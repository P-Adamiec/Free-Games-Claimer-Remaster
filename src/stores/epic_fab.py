"""Fab store module, claims the limited-time free assets from Epic's asset marketplace.

Only the blade's `isLimitedFreeContent` flag marks an asset as free, and sign-in rides
on the Epic browser profile.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import timezone

import nodriver as uc

from src.core.claimer import (BaseClaimer, CAPTCHA_ANSWER_JS, CHALLENGE_JS, OTP_KEY_ATTEMPTS, account_key, answer_filled,
                              captcha_answer_note, challenge_needs_you, giveaway_end, mark_page_checked, read_lately)
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.display import vnc_viewers
from src.core.run_state import (CAPTCHA_ENDS, CAPTCHA_UNSOLVED, book, booking_until, ends_before_next_run, is_booked,
                                lost_before_next_run, mark_answered, mark_unanswered, needs_you, next_run, unbook_title,
                                utc_text, waits_for_nobody)
from src.core.url_security import url_has_allowed_host
from src.stores.epic import FRAME_HIT_JS, FRAME_ORIGIN_JS, SHOWN, checkout_total_is_zero, saved_account_finder

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
        text: this.body ? (this.body.innerText || '').replace(/\\s+/g, ' ') : '',
    });
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


# Buttons a person clicks with the mouse; the scripts above stay as the fallback when a mouse click does not take.
BUY_NOW_FINDER = r"""() => [...document.querySelectorAll('button, a[role=button]')]
    .find(b => /^(buy now|get|add to my library)$/i.test((b.innerText || '').trim()))"""
# What Buy now changes: Epic's checkout frame and the dialogs on screen (the licence); compared before and after the click.
BUY_NOW_STATE_JS = r"""(document.querySelector('iframe[src*="/payment/web/purchase"]') ? 'frame' : 'page') + ':' + [...document.querySelectorAll(
    '[class*="Modal-root" i], [role=dialog], dialog')].filter(m => m.getBoundingClientRect().width > 0).length"""
EULA_MODAL = r"""[...document.querySelectorAll('[class*="Modal-root" i], [role=dialog], dialog')]
    .filter(m => { const r = m.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .find(m => /end user license agreement/i.test(m.innerText || ''))"""
# The licence's required box, found as EULA_ACCEPT_JS finds it; the marketing boxes are never it.
EULA_BOX_FINDER = r"""() => {
    const modal = __MODAL__;
    if (!modal) return null;
    const labelFor = box => {
        const byFor = box.id ? [...modal.querySelectorAll('label')].find(l => l.htmlFor === box.id) : null;
        const wrap = box.closest('label') || box.parentElement;
        return ((byFor && byFor.innerText) || (wrap && wrap.innerText) || '').replace(/\s+/g, ' ').trim();
    };
    const required = [...modal.querySelectorAll('input[type=checkbox]')]
        .find(b => /agree/i.test(labelFor(b)) && !/news|survey|special offers|subscri/i.test(labelFor(b)));
    return required && !required.checked ? required : null;
}""".replace("__MODAL__", EULA_MODAL)
EULA_ACCEPT_FINDER = r"""() => { const modal = __MODAL__;
    return modal ? [...modal.querySelectorAll('button')].find(b => !b.disabled && /^(accept|agree|i agree)$/i.test((b.innerText || '').trim())) : null; }
""".replace("__MODAL__", EULA_MODAL)
EULA_OPEN_JS = "!!(" + EULA_MODAL + ")"
# Epic's own buttons on its sign-in pages, which Fab hands over to.
EPIC_SUBMIT_FINDER = r"""() => { const byId = document.querySelector('#sign-in, #continue');
    if (byId && !byId.disabled) return byId;
    return [...document.querySelectorAll('button')].find(b => !b.disabled && /^(sign in|continue|log in)$/i.test((b.innerText || '').trim())); }"""
EPIC_CONTINUE_FINDER = r"""() => [...document.querySelectorAll('button, a[role=button]')].find(b => /^continue$/i.test((b.innerText || '').trim()))"""
# Inside the checkout frame: the button for the order and the one for the EU waiver, only ones a person can see.
CHECKOUT_ADD_FINDER = r"""() => [...document.querySelectorAll('button, a[role=button]')].filter(__SHOWN__)
    .find(b => /add to library|place order|get now/i.test((b.innerText || '').trim()))""".replace("__SHOWN__", SHOWN)
CHECKOUT_ACCEPT_FINDER = r"""() => [...document.querySelectorAll('button')].filter(__SHOWN__)
    .find(b => /^(i accept|accept|agree)$/i.test((b.innerText || '').trim()))""".replace("__SHOWN__", SHOWN)
CHECKOUT_CONSENT_OPEN_JS = r"""function () { return /right of withdrawal/i.test(this.body ? (this.body.innerText || '') : ''); }"""


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
        licenses = [lic for lic in listing.get("licenses") or [] if isinstance(lic, dict)]
        listings.append({
            "title": title or "Fab asset",
            "url": URL_LISTING.format(uid=uid),
            "uid": uid,
            "seller": _seller_name(listing.get("user")),
            "listing_type": str(listing.get("listingType") or "").strip(),
            # When the free price ends, for a captcha on its last run; empty when the blade does not say.
            "ends": next((str((lic.get("priceTier") or {}).get("discountEndDate") or "") for lic in licenses
                          if (lic.get("priceTier") or {}).get("discountEndDate")), ""),
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
    # Set once a puzzle stopped Epic's checkout in this run, so the rest waits for the next run.
    _checkout_puzzle = False
    # When the asset being claimed stops being free, for a puzzle on its last run.
    _ends_now = None
    # Set when a puzzle for this asset went to you and vanished with no answer, so an asset that did not land is a captcha.
    _puzzle_asked = False
    # The asset being claimed, kept for a meeting in noVNC when its puzzle finds nobody.
    _claiming = None
    # A meeting in noVNC claims only the kept assets; with you there every puzzle is yours at once.
    _meeting = False
    _with_you = False
    # Who watched noVNC when you were last asked, so only a later connection counts as you coming.
    _seen_at_prompt = None

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

    async def run(self, booked_items: list | None = None, with_you: bool = False) -> None:
        """Main entry point for the Fab claiming flow, or only the assets kept for a meeting in noVNC."""
        logger.debug("Starting Fab claiming flow")
        if booked_items is not None:
            self._meeting, self._with_you = True, with_you
            # You connected, so the prompt nobody answered earlier in this run does not hold this one back.
            if with_you:
                mark_answered(self.store_name)
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

            listings = [kept["data"] for kept in booked_items] if booked_items is not None else await self._detect_free_listings()
            if not listings:
                logger.info("No free assets available right now.")
                return
            logger.info("Found %d free Fab asset(s): %s", len(listings),
                        ", ".join(item["title"] for item in listings))

            # An asset you have is opened again only every few days, and with nothing to open there is no sign-in either.
            if booked_items is None:
                due = []
                for item in listings:
                    if await self._read_lately(item["uid"]):
                        logger.debug("'%s' is on record and its page was read lately, not opened this run.", item["title"])
                        self.notify_games.append({"title": item["title"], "url": item["url"], "status": "existed"})
                    else:
                        due.append(item)
                listings = due
                if not listings:
                    logger.info("Fab claimer finished.")
                    return

            if not await self._ensure_logged_in():
                logger.error("Aborting Fab claim flow due to login failure.")
                return

            if booked_items is not None:
                held = 0
                for kept in booked_items:
                    # A puzzle nobody answered at this meeting either: only an asset kept on its last run is still opened.
                    if waits_for_nobody(self.store_name) and not kept["last_chance"]:
                        held += 1
                        self.notify_games.append({"title": kept["title"], "url": kept["data"].get("url", ""),
                                                  "status": "failed:captcha"})
                        continue
                    await self._claim_listing(kept["data"])
                self._captcha_held(held, "asset")
                return

            held = 0
            for item in listings:
                ends = giveaway_end(item.get("ends"))
                # On its last run an asset is still opened: with nobody there a puzzle ends it at once.
                if self._captcha_holds(ends):
                    # With EG_CAPTCHA_ASK an asset you lack is yours anyway, so it joins your noVNC meeting unopened.
                    if await self._already_recorded(item["uid"]):
                        continue
                    if not (cfg.eg_captcha_ask and self._book(item["title"], item, ends, False)):
                        held += 1
                    continue
                # Each further checkout could draw another puzzle, so after one an asset you lack waits for the next run.
                if self._checkout_puzzle and not cfg.eg_captcha_ask and not ends_before_next_run(ends):
                    if not await self._already_recorded(item["uid"]):
                        logger.info("'%s' waits for the next run after a captcha in Epic's checkout.", item["title"])
                        needs_you(self.store_name, 1, "asset", why=CAPTCHA_UNSOLVED)
                    continue
                await self._claim_listing(item)
            self._captcha_held(held, "asset")

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
                "Set EG_OTP_KEY to let the bot fill authenticator codes by itself.",
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
            email_input = await self.page.select("#email", timeout=15)
            if not email_input:
                logger.debug("Epic login form did not render.")
                return "failed"
            await email_input.click()
            await self.sleep(0.8)
            await email_input.send_keys(cfg.eg_email.strip())
            await self.sleep(0.5)

            # Epic serves two shapes of this form: email first, or both fields at once.
            password_input = await self.page.select("#password", timeout=5)
            if not password_input:
                continue_btn = await self.page.select("#continue", timeout=5)
                if continue_btn:
                    await continue_btn.click()
                    await self.sleep(3)
                password_input = await self.page.select("#password", timeout=10)
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
        """Press the button that submits whichever Epic screen is showing, with the mouse first."""
        if await self._click_like_a_hand(EPIC_SUBMIT_FINDER, "Epic sign-in button"):
            return True
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
        # Continue, or on a list of saved accounts the tile of EG_EMAIL, as Epic itself picks it.
        clicked = (await self._click_like_a_hand(EPIC_CONTINUE_FINDER, "continue as this account")
                   or await self._click_like_a_hand(saved_account_finder(cfg.eg_email), "saved account")
                   or await self.page.evaluate("""
            (() => {
                const btn = [...document.querySelectorAll('button, a[role=button]')]
                    .find(b => /^continue$/i.test((b.innerText || '').trim()));
                if (btn) { btn.click(); return true; }
                // The store shows the same screen as a list of accounts with no Continue button.
                const tile = document.querySelector('[id^="account-"]');
                if (tile) { tile.click(); return true; }
                return false;
            })()
        """))
        if clicked:
            await self.sleep(5)
        else:
            logger.debug("No Continue button on the account confirmation screen.")
        return bool(clicked)

    async def _await_login_outcome(self, timeout: int = 60) -> str:
        """Watch until Fab recognises the session, a code is asked for, or time runs out."""
        otp_tried = 0
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
                if cfg.eg_otp_key and otp_tried < OTP_KEY_ATTEMPTS:
                    otp_tried += 1
                    await self._fill_totp()
                    continue
                if not cfg.eg_otp_key:
                    # Stay on the code screen so the user can finish it over VNC.
                    logger.debug("Login outcome: mfa, a code is due and EG_OTP_KEY is not set.")
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
        """Auto-enter the authenticator code from EG_OTP_KEY, then submit."""
        if not cfg.eg_otp_key:
            return False
        try:
            field = await self.page.select('input[name="code-input-0"]', timeout=5)
            if not field:
                return False
            logger.debug("Entering the Epic two-step code from EG_OTP_KEY.")
            await field.clear_input()
            await self.sleep(0.4)
            self._last_totp = await self._fresh_totp(cfg.eg_otp_key, self._last_totp)
            await field.send_keys(self._last_totp)
            await self.sleep(1)
            submit = await self.page.select('button[type="submit"]', timeout=5)
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
        self._ends_now = giveaway_end(item.get("ends"))
        self._puzzle_asked = False
        self._claiming = item
        # Each asset's checkout gets its own two mouse clicks per button; live 3.11 the third asset found them spent.
        self._click_counts = {}
        # Signed in by hand there is no EG_EMAIL, so the record under the account's name is found only after sign-in.
        if await self._read_lately(uid):
            logger.debug("'%s' is on record and its page was read lately, not opened this run.", title)
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            return
        logger.debug("Opening Fab listing: %s (%s by %s)", url, item.get("listing_type"), item.get("seller"))

        await self.page.get(url)
        await self.sleep(6)

        if await self._human_challenge_present() and not await self._wait_out_challenge("Fab"):
            logger.warning("A security check is blocking the Fab page for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:blocked"})
            lost = lost_before_next_run(self._ends_now)
            needs_you(self.store_name, 1, "asset", why=CAPTCHA_ENDS if lost else CAPTCHA_UNSOLVED)
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
            await self._remember(uid, title, url, "existed")
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
        checkout = await self._complete_checkout(title)
        if checkout == "not-free":
            self.notify_games.append({"title": title, "url": url, "status": "skipped:not-free"})
            return
        if checkout == "captcha":
            self._report_captcha(title, url)
            return
        if checkout == "eula":
            self.notify_games.append({"title": title, "url": url, "status": "failed:eula"})
            return
        if not checkout:
            logger.warning("Epic's checkout did not offer '%s' for the library.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:checkout"})
            return

        await self.sleep(8)
        state = await self._page_state()
        logger.debug("Fab page after the claim click: %r", (state.get("body") or "")[:400])

        if uid in await self._owned_uids():
            logger.info("✓ Claimed '%s' successfully!", title)
            status = "claimed"
            unbook_title(self.store_name, title)
        elif self._puzzle_asked:
            # A puzzle handed to you that vanished with no asset behind it expired unsolved.
            self._checkout_puzzle = True
            mark_unanswered(self.store_name)
            self._book(title, item, self._ends_now, ends_before_next_run(self._ends_now))
            self._report_captcha(title, url)
            return
        else:
            logger.warning("Claim of '%s' was not confirmed by the page, trying again next run.", title)
            status = "failed:unconfirmed"

        self.notify_games.append({"title": title, "url": url, "status": status})
        if status == "claimed":
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
        # The required box, then Accept, with the mouse; EULA_ACCEPT_JS finishes whatever the mouse did not.
        if await self._click_like_a_hand(EULA_BOX_FINDER, "licence agreement box"):
            await self.sleep(1)
        if await self._click_like_a_hand(EULA_ACCEPT_FINDER, "accept the licence"):
            for _ in range(5):
                await self.sleep(1)
                if await self.page.evaluate(EULA_OPEN_JS) is not True:
                    logger.debug("Licence modal accepted with the mouse.")
                    await self.sleep(6)
                    return True
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
                # The <iframe> itself, kept to know where the frame sits on the page for a mouse click.
                self._checkout_iframe = node
                return getattr(node, "content_document", None)
        return None

    async def _checkout_eval(self, document, function_declaration: str, *args):
        """Run JS on a node of the pierced page (the checkout frame's document, or the <iframe>) and parse what it returns."""
        try:
            handle = await self.page.send(uc.cdp.dom.resolve_node(node_id=document.node_id))
            result = await self.page.send(uc.cdp.runtime.call_function_on(
                function_declaration=function_declaration,
                object_id=handle.object_id,
                return_by_value=True,
                arguments=[uc.cdp.runtime.CallArgument(value=a) for a in args] or None,
            ))
            if isinstance(result, tuple):
                result = result[0]
            raw = getattr(result, "value", None)
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.debug("Could not evaluate inside the checkout frame: %s", exc)
            return None

    async def _click_in_checkout(self, document, finder: str, label: str) -> bool:
        """Click a button in Epic's checkout frame with Epic's own mouse loop: its spot in the frame plus where the frame sits."""
        async def run_js(expression):
            # Stringified, so the frame's answer comes back exactly as the page's own evaluate gives it.
            return await self._checkout_eval(document, "function () { return JSON.stringify(" + expression + "); }")

        async def read():
            return await self._read_box(run_js, finder)

        async def frame():
            iframe = getattr(self, "_checkout_iframe", None)
            return await self._checkout_eval(iframe, FRAME_ORIGIN_JS) if iframe is not None else None

        async def wheel_point():
            f = await frame()
            if not f or f["bottom"] - f["top"] < 40:
                return None
            return (f["x"] + f["w"] * random.uniform(0.3, 0.7), f["top"] + (f["bottom"] - f["top"]) * random.uniform(0.3, 0.7))

        async def to_page(box):
            f = await frame()
            if not f:
                return None
            px, py = round(f["x"] + box["x"]), round(f["y"] + box["y"])
            if await self._checkout_eval(self._checkout_iframe, FRAME_HIT_JS, px, py) is True:
                return px, py
            logger.debug("Point (%d, %d) is not on the checkout frame at %s.", px, py, f)
            return None

        return await self._click_until_taken(read, run_js, wheel_point, to_page, finder, f"{label} (checkout frame)",
                                             tries=2, allow_script=True)

    async def _checkout_challenge_reason(self) -> str:
        """What reads as a human check on the page, or inside Epic's checkout frame where the page check cannot look."""
        reason = await self._human_challenge_reason()
        if reason:
            return reason
        document = await self._checkout_document()
        found = await self._checkout_eval(document, f"function () {{ return JSON.stringify({CHALLENGE_JS}); }}") if document else ""
        return f"checkout frame, {found}" if found else ""

    def _report_captcha(self, title: str, url: str) -> None:
        """Log and list an asset a puzzle stopped; one kept for a noVNC meeting is told apart in the summary."""
        kept = is_booked(self.store_name, title)
        logger.warning("A captcha in Epic's checkout stopped '%s', %s.", title,
                       "it waits for you in noVNC" if kept else "trying again next run")
        # A kept asset is listed apart in the summary, and its meeting reports how it ended.
        if not kept:
            self.notify_games.append({"title": title, "url": url, "status": "failed:captcha"})
            lost = lost_before_next_run(self._ends_now)
            needs_you(self.store_name, 1, "asset", why=CAPTCHA_ENDS if lost else CAPTCHA_UNSOLVED)

    def _book(self, title: str, item, ends, last_chance: bool) -> bool:
        """Keep an asset whose puzzle nobody solved for a meeting with you in noVNC; False when there is no time for one."""
        viewers = vnc_viewers()
        if self._meeting or not item or viewers is None:
            # Outside the container nobody can be seen opening noVNC, so there is no meeting to wait for.
            if viewers is None:
                logger.debug("No noVNC to watch here, so '%s' is not kept for a meeting.", title)
            return False
        until = booking_until(ends, last_chance)
        if until is None:
            logger.debug("No noVNC meeting for '%s': %s.", title,
                         "a single run does not wait" if next_run() is None else f"too little time is left (ends {ends})")
            return False
        seen = self._seen_at_prompt if self._seen_at_prompt is not None else viewers
        if book(self.store_name, {"title": title, "data": item, "ends": ends, "last_chance": last_chance,
                                  "until": until, "seen": set(seen)}):
            logger.info("'%s' waits for you in noVNC until %s: once you connect, the bot opens its checkout again.",
                        title, utc_text(until))
        return True

    async def _clear_checkout_challenge(self, title: str) -> bool:
        """Like Epic: a check that passes by itself is waited out, a puzzle waits for the next run or for you with EG_CAPTCHA_ASK."""
        reason = await self._checkout_challenge_reason()
        if not reason:
            return True
        logger.debug("Human check on Epic's checkout for '%s': %s", title, reason)
        person = challenge_needs_you(reason)

        async def present() -> bool:
            return bool(await self._checkout_challenge_reason())

        last_chance = ends_before_next_run(self._ends_now)
        ask = cfg.eg_captcha_ask or not person or last_chance
        if person and self._meeting:
            # At a meeting you are there; the bot's own last try asks nobody.
            ask = self._with_you
        if person and ask:
            self._seen_at_prompt = vnc_viewers() or set()
        if person and ask and last_chance and not cfg.eg_captcha_ask and not waits_for_nobody(self.store_name):
            logger.info("'%s' is free only until %s, %s, so you are asked to solve its captcha.",
                        title, self._ends_now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "before the next run" if next_run() else "and this single run does not wait")
        cleared = await self._wait_out_challenge(f"Fab ('{title}')", settle=12 if person else 180, present_fn=present,
                                                 reason_fn=self._checkout_challenge_reason, ask=ask)
        if not cleared and person:
            self._checkout_puzzle = True
            # A puzzle that was yours and stayed unsolved waits for you in noVNC.
            if cfg.eg_captcha_ask or last_chance:
                self._book(title, self._claiming, self._ends_now, last_chance)
        elif person and ask and self._handed_over:
            # Solved or expired (after about two minutes) look alike here; a filled answer is a solve, else the asset decides.
            lengths = await self._captcha_answer_lengths()
            logger.debug("The puzzle for '%s' is gone: %s.", title, captcha_answer_note(lengths))
            self._puzzle_asked = not answer_filled(lengths)
        return cleared

    async def _captcha_answer_lengths(self) -> list:
        """How long the captcha answer is on the page and in Epic's checkout frame, for the log."""
        lengths = []
        try:
            lengths.append(await self.page.evaluate(CAPTCHA_ANSWER_JS))
            document = await self._checkout_document()
            if document:
                # In parentheses: a return followed by a line break returns nothing.
                lengths.append(await self._checkout_eval(document, f"function () {{ return ({CAPTCHA_ANSWER_JS}); }}"))
        except Exception as exc:
            logger.debug("Could not read the captcha answer field: %s", exc)
        return lengths

    async def _complete_checkout(self, title: str = "") -> bool | str:
        """Press "Add to library" in Epic's checkout frame; "not-free" at a total above zero, "captcha" after a puzzle, "eula" without FAB_ACCEPT_EULA."""
        # The same frame sells assets, so only a total of zero is ever ordered; an unreadable one is not.
        document, state, free = None, {}, None
        for attempt in range(5):
            if attempt:
                await self.sleep(2)
            document = await self._checkout_document()
            if document is None:
                continue
            state = await self._checkout_eval(document, CHECKOUT_STATE_JS) or {}
            free = checkout_total_is_zero(state.get("text"), title)
            if free is not None:
                break
        if document is None:
            logger.debug("No Epic checkout frame on the page.")
            return False
        logger.debug("Checkout frame: %r buttons=%r free=%s", (state.get("text") or "")[:120], state.get("buttons"), free)
        if free is False:
            logger.warning("Epic's checkout wants money for this asset, nothing was ordered.")
            return "not-free"
        if free is None:
            logger.warning("Could not read the total in Epic's checkout, nothing was ordered.")
            return False

        if not await self._click_in_checkout(document, CHECKOUT_ADD_FINDER, "add to library"):
            return False
        await self.sleep(6)
        if not await self._clear_checkout_challenge(title):
            return "captcha"

        # The order then waits on the EU right-of-withdrawal waiver.
        document = await self._checkout_document()
        if document is None:
            return True
        present = bool(await self._checkout_eval(document, CHECKOUT_CONSENT_OPEN_JS))
        if not present:
            await self.sleep(6)
            # The order can still draw a puzzle once it is placed.
            return True if await self._clear_checkout_challenge(title) else "captcha"
        if not cfg.fab_accept_eula:
            logger.warning("Epic asks to waive the right of withdrawal and FAB_ACCEPT_EULA is off.")
            return "eula"
        if not await self._click_in_checkout(document, CHECKOUT_ACCEPT_FINDER, "accept the waiver"):
            logger.debug("Right-of-withdrawal dialog is open but its accept button could not be pressed.")
            return False
        logger.debug("Accepted the right-of-withdrawal waiver.")
        await self.sleep(8)
        return True if await self._clear_checkout_challenge(title) else "captcha"

    async def _click_buy_now(self) -> bool:
        """Click the button that starts the free checkout with the mouse, by script when that did not take."""
        before = await self.page.evaluate(BUY_NOW_STATE_JS)
        if await self._click_like_a_hand(BUY_NOW_FINDER, "buy now"):
            for _ in range(12):
                await self.sleep(1)
                if await self.page.evaluate(BUY_NOW_STATE_JS) != before:
                    return True
            logger.debug("Nothing opened after the mouse click on the buy button, pressing it by script.")
        elif (await self._read_box(self.page.evaluate, BUY_NOW_FINDER)).get("reason") == "covered":
            # Something lies over the button, a person could not press it either.
            logger.debug("The buy button is covered, not pressing it by script.")
            return False
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

    async def _record(self, uid: str):
        """This asset's row for this account, None when no earlier run stored it."""
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame).where(
                    ClaimedGame.store == self.store_name,
                    ClaimedGame.user == account_key(cfg.eg_email, self.user),
                    ClaimedGame.game_id == uid,
                )
            )
            return result.scalar_one_or_none()

    async def _read_lately(self, uid: str) -> bool:
        """True for an asset on record whose page was read in the last few days."""
        return read_lately(await self._record(uid), ("claimed", "existed"))

    async def _already_recorded(self, uid: str) -> bool:
        """True when an earlier run already stored this asset for this account."""
        return await self._record(uid) is not None

    async def _remember(self, uid: str, title: str, url: str, status: str) -> None:
        """Record the asset so later runs can tell it apart from a new one."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session,
                store=self.store_name,
                user=account_key(cfg.eg_email, self.user),
                game_id=uid,
                title=title,
                url=url,
                status=status,
            )
            # A claim the bot made stays a claim when a later run finds the asset owned.
            if not created and obj.status != status and not (obj.status == "claimed" and status == "existed"):
                obj.status = status
            mark_page_checked(obj)
            await session.commit()
            logger.debug("DB %s '%s' (status=%s).", "stored" if created else "already had", uid, obj.status)


async def claim_fab() -> dict:
    """Convenience entry point."""
    claimer = FabClaimer()
    await claimer.run()
    return {"store": "Fab", "user": claimer.user, "games": claimer.notify_games}


async def claim_fab_booked(items: list, with_you: bool) -> dict:
    """A meeting in noVNC for the assets whose puzzle nobody solved: with you there, or the bot's own last try."""
    claimer = FabClaimer()
    await claimer.run(booked_items=items, with_you=with_you)
    return {"store": "Fab", "user": claimer.user, "games": claimer.notify_games}
