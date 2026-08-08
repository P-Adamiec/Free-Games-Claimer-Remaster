"""Fab store module, claims the limited-time free assets from Epic's asset marketplace.

Only the blade's `isLimitedFreeContent` flag marks an asset as free, and sign-in rides
on the Epic browser profile.
"""

from __future__ import annotations

import json
import logging

import nodriver as uc
import pyotp

from src.core.claimer import BaseClaimer
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.fab")

URL_FREE = "https://www.fab.com/limited-time-free?lang=en"
URL_LISTING = "https://www.fab.com/listings/{uid}"
# A protected route, Fab bounces it to Epic SSO with its own client id.
URL_LOGIN = "https://www.fab.com/login"
API_BLADE = "/i/blades/free_content_blade"
API_ME = "/i/users/me"
# The account's acquired assets, the only trustworthy ownership signal.
API_LIBRARY = "/i/library/search?count=100&source=acquired"
FAB_HOST = "www.fab.com"
EPIC_LOGIN_HOST = "www.epicgames.com"

# The blade is only the free promotion when Fab says so, never assume it from a price.
FREE_BLOCK_TYPE = "listings"

# Buy now hands over to Epic's payment frame, which is where the asset is actually acquired.
CHECKOUT_PATH = "/payment/web/purchase"

CHECKOUT_STATE_JS = """
function () {
    return JSON.stringify({
        buttons: [...this.querySelectorAll('button, a[role=button]')]
            .map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 10),
        text: (this.body ? (this.body.innerText || '').replace(/\\s+/g, ' ') : '').slice(0, 260),
    });
}
"""

CHECKOUT_CLICK_JS = """
function () {
    const btn = [...this.querySelectorAll('button, a[role=button]')]
        .find(b => /add to library|place order|get now/i.test((b.innerText || '').trim()));
    if (!btn || btn.disabled) return JSON.stringify({clicked: false});
    btn.click();
    return JSON.stringify({clicked: true, label: (btn.innerText || '').trim()});
}
"""

# Add to library then asks to waive the EU right of withdrawal, the order waits on it.
CHECKOUT_CONSENT_JS = """
function () {
    const text = this.body ? (this.body.innerText || '') : '';
    if (!/right of withdrawal/i.test(text)) return JSON.stringify({present: false});
    const btn = [...this.querySelectorAll('button')]
        .find(b => /^(i accept|accept|agree)$/i.test((b.innerText || '').trim()));
    if (!btn || btn.disabled) return JSON.stringify({present: true, clicked: false});
    btn.click();
    return JSON.stringify({present: true, clicked: true, label: (btn.innerText || '').trim()});
}
"""

# Accept stays disabled until the required box is ticked; the second box is marketing.
EULA_ACCEPT_JS = r"""
(() => {
    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const modal = [...document.querySelectorAll('[class*="Modal-root" i], [role=dialog], dialog')]
        .filter(vis)
        .find(m => /end user license agreement/i.test(m.innerText || ''));
    if (!modal) return JSON.stringify({error: 'no-modal'});

    const labelFor = box => {
        const byFor = box.id ? [...modal.querySelectorAll('label')].find(l => l.htmlFor === box.id) : null;
        const wrap = box.closest('label') || box.parentElement;
        return ((byFor && byFor.innerText) || (wrap && wrap.innerText) || '').replace(/\s+/g, ' ').trim();
    };

    const boxes = [...modal.querySelectorAll('input[type=checkbox]')];
    const marketing = /news|survey|special offers|subscri/i;
    const required = boxes.find(b => /agree/i.test(labelFor(b)) && !marketing.test(labelFor(b)));
    if (!required) return JSON.stringify({error: 'no-agreement-box', labels: boxes.map(labelFor)});

    if (!required.checked) required.click();

    const accept = [...modal.querySelectorAll('button')]
        .find(b => /^(accept|agree|i agree)$/i.test((b.innerText || '').trim()));
    const marketingBoxes = boxes.filter(b => b !== required);
    const out = {
        agreed: required.checked,
        marketingUntouched: marketingBoxes.every(b => !b.checked),
        acceptDisabled: accept ? accept.disabled : null,
        accepted: false,
    };
    if (accept && !accept.disabled) {
        accept.click();
        out.accepted = true;
    }
    return JSON.stringify(out);
})()
"""


# ----------------------------------------------------------------------
# Detection (pure function, no browser)
# ----------------------------------------------------------------------

def _seller_name(user) -> str:
    """Fab nests the seller in an object, older captures had it as a plain string."""
    if isinstance(user, dict):
        return str(user.get("sellerName") or "").strip()
    return str(user or "").strip()


def parse_free_listings(blade: dict) -> list[dict]:
    """Read the limited-time free assets out of Fab's free content blade."""
    if not isinstance(blade, dict):
        return []

    if blade.get("isLimitedFreeContent") is not True:
        logger.debug("Fab blade is not the limited-free promotion (isLimitedFreeContent=%r), ignoring it.",
                     blade.get("isLimitedFreeContent"))
        return []
    if blade.get("blockContentType") != FREE_BLOCK_TYPE:
        logger.debug("Fab blade holds %r, not listings, ignoring it.", blade.get("blockContentType"))
        return []

    listings, skipped = [], []
    for tile in blade.get("tiles") or []:
        listing = (tile or {}).get("listing") or {}
        uid = str(listing.get("uid") or "").strip()
        title = str(listing.get("title") or "").strip()
        if not uid:
            skipped.append(title or "<no title>")
            continue
        listings.append({
            "title": title or "Fab asset",
            "url": URL_LISTING.format(uid=uid),
            "uid": uid,
            "seller": _seller_name(listing.get("user")),
            "listing_type": str(listing.get("listingType") or "").strip(),
        })

    if skipped:
        logger.debug("Fab tiles without a listing id: %s", ", ".join(skipped))
    return listings


# ----------------------------------------------------------------------
# Claimer
# ----------------------------------------------------------------------

class FabClaimer(BaseClaimer):
    store_name = "fab"
    # Fab signs in with an Epic account, so it rides on the Epic profile's session.
    profile_name = "epic"

    PAGE_STATE_JS = r"""
        (() => {
            const txt = el => (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
            const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const buttons = [...document.querySelectorAll('button, a[role=button]')].filter(vis);
            return JSON.stringify({
                url: location.href,
                title: document.title || '',
                h1: [...document.querySelectorAll('h1')].filter(vis).map(txt),
                buttons: buttons.map(b => txt(b).slice(0, 60)).filter(Boolean),
                body: (document.body ? txt(document.body) : '').slice(0, 1200),
            });
        })()
    """

    async def run(self) -> None:
        """Main entry point for the Fab claiming flow."""
        logger.debug("Starting Fab claiming flow")
        try:
            # Checkout fronts hCaptcha, which software rendering triggers. Same flags as epic.py.
            await self.start_browser(
                force_headful=True,
                extra_args=[
                    "--ignore-gpu-blocklist",
                    "--enable-unsafe-webgpu",
                ],
            )
            await self.page.get(URL_FREE)
            await self.sleep(5)

            listings = await self._detect_free_listings()
            if not listings:
                logger.info("No free assets available right now.")
                return
            logger.info("Found %d free Fab asset(s): %s", len(listings),
                        ", ".join(item["title"] for item in listings))

            if not await self._ensure_logged_in():
                logger.error("Aborting Fab claim flow due to login failure.")
                return

            for item in listings:
                await self._claim_listing(item)

            logger.info("Fab claimer finished.")

        except Exception as exc:
            logger.exception("Fatal error during Fab flow")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Page helpers
    # ------------------------------------------------------------------

    async def _fetch_json(self, path: str) -> dict:
        """Read one of Fab's JSON endpoints from inside the page (Cloudflare 403s plain clients)."""
        raw = await self.page.evaluate(
            f"fetch({json.dumps(path)}, {{credentials: 'include', headers: {{'Accept': 'application/json'}}}})"
            ".then(r => r.text())",
            await_promise=True,
        )
        return json.loads(raw) if isinstance(raw, str) else {}

    async def _page_state(self) -> dict:
        """Title, headings, visible buttons and text in one read."""
        try:
            raw = await self.page.evaluate(self.PAGE_STATE_JS)
            return json.loads(raw) if isinstance(raw, str) else {}
        except Exception as exc:
            logger.debug("Could not read the page state: %s", exc)
            return {}

    async def _detect_free_listings(self) -> list[dict]:
        """Ask Fab which assets are free right now, [] on any failure."""
        try:
            blade = await self._fetch_json(API_BLADE)
        except Exception as exc:
            logger.warning("Could not read the Fab free content list: %s", exc)
            return []
        logger.debug("Fab blade: %r (%d tile(s))", blade.get("title"), len(blade.get("tiles") or []))
        return parse_free_listings(blade)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _is_logged_in(self) -> bool:
        """True when Fab's own endpoint recognises the session."""
        try:
            raw = await self.page.evaluate(
                f"fetch({json.dumps(API_ME)}, {{credentials: 'include'}}).then(r => String(r.status))",
                await_promise=True,
            )
            status = str(raw).strip()
        except Exception as exc:
            logger.debug("Could not check the Fab session: %s", exc)
            return False
        logger.debug("Fab %s answered %s (401 means signed out).", API_ME, status)
        return status == "200"

    async def _account_name(self) -> str:
        """The signed-in Fab account name, falling back to the Epic email."""
        try:
            me = await self._fetch_json(API_ME)
            for key in ("sellerName", "username", "displayName", "email"):
                value = me.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:60]
        except Exception as exc:
            logger.debug("Could not read the Fab account name: %s", exc)
        return cfg.eg_email or "EpicUser"

    async def _ensure_logged_in(self) -> bool:
        """Use the session inherited from Epic, log in only when Fab says we are signed out."""
        if await self._is_logged_in():
            logger.debug("Session inherited from the Epic profile, no Fab login needed.")
            self.log_signed_in(await self._account_name())
            return True

        outcome = "failed"
        if cfg.eg_email and cfg.eg_password:
            outcome = await self._do_login()
            if outcome == "ok":
                self.log_signed_in(await self._account_name())
                return True
        else:
            logger.warning("EG_EMAIL / EG_PASSWORD are not set, manual login needed.")

        if outcome == "mfa":
            logger.warning("Epic asked for a two-step code during the Fab sign-in, requesting it via VNC.")
            custom_msg = self._vnc_notice(
                "Fab: 2FA code needed",
                "Enter the code Epic sent to your email or phone (or from your authenticator app) in the browser. "
                "Set EG_OTPKEY to let the bot fill authenticator codes by itself.",
            )
        else:
            custom_msg = self._vnc_notice(
                "Fab: login needs you",
                "Automated sign-in did not complete. Open the browser and finish signing in to Fab with your Epic account.",
            )

        async def _check() -> bool:
            # Never poll while an Epic screen is open, the user may be typing a code there.
            if not url_has_allowed_host(await self._current_url(), FAB_HOST):
                return False
            return await self._is_logged_in()

        if await self._wait_for_vnc_login(_check, custom_msg=custom_msg):
            self.log_signed_in(await self._account_name())
            return True

        logger.warning("Fab login still not completed after the VNC wait, skipping.")
        return False

    async def _current_url(self) -> str:
        """The address the page is really on, page.url goes empty right after a redirect."""
        try:
            return str(await self.page.evaluate("window.location.href") or self.page.url)
        except Exception:
            return str(self.page.url)

    async def _do_login(self) -> str:
        """Sign in to Fab through Epic SSO. Returns 'ok', 'mfa' or 'failed'."""
        # Any protected route bounces to Epic SSO, steadier than hunting for a header control.
        logger.debug("Opening the Fab sign-in flow through %s.", URL_LOGIN)
        await self.page.get(URL_LOGIN)
        await self.sleep(7)

        if await self._human_challenge_present() and not await self._wait_out_challenge("Fab"):
            return "failed"

        landed = await self._current_url()
        if not url_has_allowed_host(landed, EPIC_LOGIN_HOST):
            logger.debug("Fab did not hand over to the Epic login page (at %s).", landed[:120])
            return "failed"

        # Epic often remembers the account and asks only to confirm it, with no credential form.
        if await self._confirm_account_prompt():
            return await self._await_login_outcome()

        try:
            email_input = await self.page.find("#email", timeout=15)
            if not email_input:
                logger.debug("Epic login form did not render.")
                return "failed"
            await email_input.click()
            await self.sleep(0.8)
            await email_input.send_keys(cfg.eg_email.strip())
            await self.sleep(0.5)

            # Epic serves two shapes of this form: email first, or both fields at once.
            password_input = await self.page.find("#password", timeout=5)
            if not password_input:
                continue_btn = await self.page.find("#continue", timeout=5)
                if continue_btn:
                    await continue_btn.click()
                    await self.sleep(3)
                password_input = await self.page.find("#password", timeout=10)
            if not password_input:
                logger.debug("Epic password field did not render.")
                return "failed"
            await password_input.click()
            await self.sleep(0.6)
            await password_input.send_keys(cfg.eg_password.strip())
            await self.sleep(0.5)

            if not await self._submit_epic_form():
                logger.debug("Epic sign-in button not found.")
                return "failed"
            logger.debug("Credentials entered, submitted the Epic sign-in form.")
        except Exception as exc:
            logger.debug("Epic login form interaction failed: %s", exc)
            return "failed"

        return await self._await_login_outcome()

    async def _submit_epic_form(self) -> bool:
        """Press the button that submits whichever Epic screen is showing."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const byId = document.querySelector('#sign-in, #continue');
                    if (byId && !byId.disabled) { byId.click(); return true; }
                    const btn = [...document.querySelectorAll('button')]
                        .find(b => !b.disabled && /^(sign in|continue|log in)$/i.test((b.innerText || '').trim()));
                    if (!btn) return false;
                    btn.click();
                    return true;
                })()
            """))
        except Exception as exc:
            logger.debug("Could not submit the Epic form: %s", exc)
            return False

    async def _confirm_account_prompt(self) -> bool:
        """Answer Epic's "Confirm Your Account" step, shown because Fab asks for select_account."""
        url = await self._current_url()
        if "/id/login/switch-account" not in url.lower():
            return False
        logger.debug("Epic is asking which account to continue with, confirming.")
        clicked = await self.page.evaluate("""
            (() => {
                const btn = [...document.querySelectorAll('button, a[role=button]')]
                    .find(b => /^continue$/i.test((b.innerText || '').trim()));
                if (!btn) return false;
                btn.click();
                return true;
            })()
        """)
        if clicked:
            await self.sleep(5)
        else:
            logger.debug("No Continue button on the account confirmation screen.")
        return bool(clicked)

    async def _await_login_outcome(self, timeout: int = 60) -> str:
        """Watch until Fab recognises the session, a code is asked for, or time runs out."""
        otp_tried = False
        waited = 0
        while waited < timeout:
            await self.sleep(4)
            waited += 4

            if url_has_allowed_host(await self._current_url(), FAB_HOST) and await self._is_logged_in():
                logger.debug("Login outcome: ok after %ds", waited)
                return "ok"

            # Epic asks which account to continue with before handing back to Fab.
            if await self._confirm_account_prompt():
                continue

            if await self._mfa_prompt_present():
                if cfg.eg_otpkey and not otp_tried:
                    otp_tried = True
                    await self._fill_totp()
                    continue
                if not cfg.eg_otpkey:
                    # Stay on the code screen so the user can finish it over VNC.
                    logger.debug("Login outcome: mfa, a code is due and EG_OTPKEY is not set.")
                    return "mfa"

        await self._confirm_account_prompt()
        await self.page.get(URL_FREE)
        await self.sleep(5)
        if await self._is_logged_in():
            logger.debug("Login outcome: ok (confirmed after returning to Fab)")
            return "ok"
        logger.debug("Login outcome: failed, last page %s", (await self._current_url())[:120])
        return "failed"

    async def _mfa_prompt_present(self) -> bool:
        """True on Epic's two-step code screen."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    if (document.querySelector('input[name="code-input-0"]')) return true;
                    return location.href.toLowerCase().includes('/id/login/mfa');
                })()
            """))
        except Exception:
            return False

    async def _fill_totp(self) -> bool:
        """Auto-enter the authenticator code from EG_OTPKEY, then submit."""
        if not cfg.eg_otpkey:
            return False
        try:
            field = await self.page.find('input[name="code-input-0"]', timeout=5)
            if not field:
                return False
            logger.debug("Entering the Epic two-step code from EG_OTPKEY.")
            await field.clear_input()
            await self.sleep(0.4)
            await field.send_keys(pyotp.TOTP(cfg.eg_otpkey).now())
            await self.sleep(1)
            submit = await self.page.find('button[type="submit"]', timeout=5)
            if submit:
                await submit.click()
                await self.sleep(4)
            return True
        except Exception as exc:
            logger.debug("Could not enter the Epic two-step code: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def _is_free_now(self, state: dict) -> bool:
        """True when the listing page itself shows the asset as free right now."""
        text = " ".join(state.get("buttons") or []) + " " + (state.get("body") or "")
        lowered = text.lower()
        return "free" in lowered and ("-100%" in lowered or "100% off" in lowered)

    async def _owned_uids(self) -> set:
        """The listing ids already on the account.

        Read from the library API, never from page text: a description saying "download"
        once produced false "claimed" reports.
        """
        try:
            data = await self._fetch_json(API_LIBRARY)
        except Exception as exc:
            logger.debug("Could not read the Fab library: %s", exc)
            return set()
        uids = set()
        for entry in (data or {}).get("results") or []:
            listing = entry.get("listing") if isinstance(entry, dict) else None
            for candidate in (entry, listing):
                uid = (candidate or {}).get("uid") if isinstance(candidate, dict) else None
                if uid:
                    uids.add(str(uid))
        logger.debug("Fab library holds %d acquired asset(s).", len(uids))
        return uids

    async def _claim_listing(self, item: dict) -> None:
        """Open one asset page, confirm it really is free, then claim it."""
        title, uid, url = item["title"], item["uid"], item["url"]
        logger.debug("Opening Fab listing: %s (%s by %s)", url, item.get("listing_type"), item.get("seller"))

        await self.page.get(url)
        await self.sleep(6)

        if await self._human_challenge_present() and not await self._wait_out_challenge("Fab"):
            logger.warning("A security check is blocking the Fab page for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:blocked"})
            return

        state = await self._page_state()
        logger.debug("Listing page: title=%r buttons=%r", state.get("title"), state.get("buttons"))

        if uid in await self._owned_uids():
            logger.info("'%s' already in library.", title)
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            await self._remember(uid, title, url, "existed")
            return

        # The blade says it is free, the page has the final word before anything is clicked.
        if not self._is_free_now(state):
            logger.info("'%s' is not free on its page, skipping.", title)
            self.notify_games.append({"title": title, "url": url, "status": "skipped:not-free"})
            return

        if await self._already_recorded(uid):
            logger.info("'%s' already in library.", title)
            logger.debug("Recorded by an earlier run, not claiming again.")
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            return

        if cfg.dryrun:
            logger.info("[DRYRUN] Would claim '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "available (dry run)"})
            return

        if not await self._click_buy_now():
            logger.warning("Could not start the claim for '%s', the buy button was not clickable.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed"})
            return

        # Buy now first asks for Fab's licence, then hands over to Epic's checkout frame.
        await self.sleep(6)
        if not await self._accept_eula():
            logger.warning("'%s' needs Fab's licence agreement accepted, set FAB_ACCEPT_EULA=true "
                           "or accept it once in the browser.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:eula"})
            return

        await self.sleep(6)
        if not await self._complete_checkout():
            logger.warning("Epic's checkout did not offer '%s' for the library.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:checkout"})
            return

        await self.sleep(8)
        state = await self._page_state()
        logger.debug("Fab page after the claim click: %r", (state.get("body") or "")[:400])

        if uid in await self._owned_uids():
            logger.info("✓ Claimed '%s' successfully!", title)
            status = "claimed"
        else:
            logger.warning("Claim of '%s' was not confirmed by the page, check it manually.", title)
            status = "failed:unconfirmed"

        self.notify_games.append({"title": title, "url": url, "status": status})
        if status != "failed:unconfirmed":
            await self._remember(uid, title, url, status)

    async def _accept_eula(self) -> bool:
        """Answer Fab's licence modal. True when it is gone, either accepted or never shown."""
        try:
            raw = await self.page.evaluate("""
                (() => {
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                    const modal = [...document.querySelectorAll('[class*="Modal-root" i], [role=dialog], dialog')]
                        .filter(vis)
                        .find(m => /end user license agreement|licence agreement|license agreement/i.test(m.innerText || ''));
                    if (!modal) return JSON.stringify({present: false});
                    const accept = [...modal.querySelectorAll('button')]
                        .find(b => /^(accept|agree|i agree)$/i.test((b.innerText || '').trim()));
                    return JSON.stringify({present: true, hasAccept: !!accept});
                })()
            """)
            state = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as exc:
            logger.debug("Could not inspect the Fab licence modal: %s", exc)
            return True

        if not state.get("present"):
            return True

        if not cfg.fab_accept_eula:
            logger.debug("Fab licence agreement is open and FAB_ACCEPT_EULA is off, not accepting it.")
            return False

        logger.debug("Accepting Fab's End User License Agreement (FAB_ACCEPT_EULA is on).")
        raw = await self.page.evaluate(EULA_ACCEPT_JS)
        result = json.loads(raw) if isinstance(raw, str) else {}
        logger.debug("Licence modal: agreement ticked=%s, marketing left alone=%s, Accept clicked=%s",
                     result.get("agreed"), result.get("marketingUntouched"), result.get("accepted"))
        if not result.get("accepted"):
            logger.debug("Could not accept the licence modal: %r", result)
            return False
        await self.sleep(7)
        return True

    # ------------------------------------------------------------------
    # Epic checkout (a cross-origin iframe over the listing page)
    # ------------------------------------------------------------------

    def _walk_nodes(self, node):
        """Every node of a pierced DOM tree, iframe documents included."""
        yield node
        for child in (node.children or []):
            yield from self._walk_nodes(child)
        content = getattr(node, "content_document", None)
        if content is not None:
            yield from self._walk_nodes(content)

    @staticmethod
    def _node_attrs(node) -> dict:
        raw = node.attributes or []
        return {raw[i]: raw[i + 1] for i in range(0, len(raw) - 1, 2)}

    async def _checkout_document(self):
        """The checkout frame's document, the parent page cannot reach into it."""
        try:
            doc = await self.page.send(uc.cdp.dom.get_document(depth=-1, pierce=True))
        except Exception as exc:
            logger.debug("Could not read the pierced DOM: %s", exc)
            return None
        for node in self._walk_nodes(doc):
            if node.node_name == "IFRAME" and CHECKOUT_PATH in self._node_attrs(node).get("src", ""):
                return getattr(node, "content_document", None)
        return None

    async def _checkout_eval(self, document, function_declaration: str):
        """Run JS inside the checkout frame and parse what it returns."""
        try:
            handle = await self.page.send(uc.cdp.dom.resolve_node(node_id=document.node_id))
            result = await self.page.send(uc.cdp.runtime.call_function_on(
                function_declaration=function_declaration,
                object_id=handle.object_id,
                return_by_value=True,
            ))
            if isinstance(result, tuple):
                result = result[0]
            raw = getattr(result, "value", None)
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.debug("Could not evaluate inside the checkout frame: %s", exc)
            return None

    async def _complete_checkout(self) -> bool:
        """Press "Add to library" in Epic's checkout frame."""
        document = await self._checkout_document()
        if document is None:
            logger.debug("No Epic checkout frame on the page.")
            return False

        state = await self._checkout_eval(document, CHECKOUT_STATE_JS) or {}
        logger.debug("Checkout frame: %r buttons=%r", (state.get("text") or "")[:120], state.get("buttons"))

        result = await self._checkout_eval(document, CHECKOUT_CLICK_JS) or {}
        if not result.get("clicked"):
            logger.debug("No add-to-library button in the checkout frame.")
            return False
        logger.debug("Clicked %r in the checkout frame.", result.get("label"))
        await self.sleep(6)

        # The order then waits on the EU right-of-withdrawal waiver.
        document = await self._checkout_document()
        if document is None:
            return True
        consent = await self._checkout_eval(document, CHECKOUT_CONSENT_JS) or {}
        if not consent.get("present"):
            await self.sleep(6)
            return True
        if not cfg.fab_accept_eula:
            logger.warning("Epic asks to waive the right of withdrawal and FAB_ACCEPT_EULA is off.")
            return False
        if not consent.get("clicked"):
            logger.debug("Right-of-withdrawal dialog is open but has no usable accept button: %r", consent)
            return False
        logger.debug("Accepted the right-of-withdrawal waiver (%r).", consent.get("label"))
        await self.sleep(8)
        return True

    async def _click_buy_now(self) -> bool:
        """Click the button that starts the free checkout."""
        try:
            raw = await self.page.evaluate("""
                (() => {
                    const buttons = [...document.querySelectorAll('button, a[role=button]')];
                    const target = buttons.find(b => /^(buy now|get|add to my library)$/i.test((b.innerText || '').trim()));
                    if (!target) return JSON.stringify({clicked: false, seen: buttons.map(b => (b.innerText||'').trim()).slice(0, 12)});
                    target.scrollIntoView({block: 'center'});
                    target.click();
                    return JSON.stringify({clicked: true, picked: (target.innerText || '').trim()});
                })()
            """)
            result = json.loads(raw) if isinstance(raw, str) else {}
            if result.get("clicked"):
                logger.debug("Clicked %r on the listing page.", result.get("picked"))
                return True
            logger.debug("No buy button found, page offered: %r", result.get("seen"))
            return False
        except Exception as exc:
            logger.debug("Clicking the Fab buy button failed: %s", exc)
            return False

    async def _already_recorded(self, uid: str) -> bool:
        """True when an earlier run already stored this asset for this account."""
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame).where(
                    ClaimedGame.store == self.store_name,
                    ClaimedGame.user == (self.user or "unknown"),
                    ClaimedGame.game_id == uid,
                )
            )
            return result.scalar_one_or_none() is not None

    async def _remember(self, uid: str, title: str, url: str, status: str) -> None:
        """Record the asset so later runs can tell it apart from a new one."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session,
                store=self.store_name,
                user=self.user or "unknown",
                game_id=uid,
                title=title,
                url=url,
                status=status,
            )
            if not created and obj.status != status:
                obj.status = status
            await session.commit()
            logger.debug("DB %s '%s' (status=%s).", "stored" if created else "already had", uid, obj.status)


async def claim_fab() -> dict:
    """Convenience entry point."""
    claimer = FabClaimer()
    await claimer.run()
    return {"store": "Fab", "user": claimer.user, "games": claimer.notify_games}
