"""Unity Asset Store module, claims the weekly free asset from Publisher of the Week.

The asset is free only with a coupon that changes every week, so the code is read from
the page each run and ownership comes from the entitlements API, never from page text.
"""

from __future__ import annotations

import json
import logging
import re

from src.core.claimer import BaseClaimer
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.unity")

URL_SALE = "https://assetstore.unity.com/publisher-sale"
URL_BASE = "https://assetstore.unity.com"
# A protected page: signed out it bounces to Unity ID, which is the only reliable session check.
URL_ORDERS = "https://assetstore.unity.com/orders"
API_ENTITLEMENTS = "/api/users/entitlements"
STORE_HOST = "assetstore.unity.com"
LOGIN_HOST = "login.unity.com"
CHECKOUT_HOST = "pay.unity.com"

# One-time billing setup is a human job, so it gets its own, longer window than a login.
SETUP_TIMEOUT = 300

# Unity rejects the whole checkout, coupon included, until these carry a value.
REQUIRED_FIELDS = {
    "sta[country]": "Country",
    "sta[firstName]": "First name",
    "sta[lastName]": "Last name",
    "sta[email]": "Email",
    "sta[streetAddress]": "Address",
    "sta[postalCode]": "Postal code",
    "sta[locality]": "City",
}

# The checkout keeps "Pay now" enabled at full price, so the amount is read before paying.
_TOTAL_RE = re.compile(r"to pay now[^\d]{0,12}([\d]+[.,][\d]{2})", re.I)

_COUPON_RE = re.compile(r"coupon code\s+([A-Z0-9][A-Z0-9_-]{3,24})", re.I)
_NAME_RE = re.compile(r"PUBLISHER ASSET GIVEAWAY\s+(.+?)\s+Add this week", re.I)

SALE_STATE_JS = r"""
    (() => {
        const txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        return JSON.stringify({
            url: location.href,
            text: document.body ? txt(document.body).slice(0, 4000) : '',
            giftLinks: [...document.querySelectorAll('a[href*="/packages/"]')]
                .map(a => ({href: a.getAttribute('href') || '', text: txt(a).slice(0, 60)}))
                .filter(a => /free gift|free asset/i.test(a.text)),
        });
    })()
"""


# The coupon box carries no name or id, so it is found by its Apply button and tagged for
# native typing. `sta[...]` fields are the billing address and must never be touched.
COUPON_FIELD = "[data-coupon-field]"
COUPON_APPLY = "[data-coupon-apply]"

COUPON_MARK_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const apply = [...document.querySelectorAll('button')].filter(vis)
            .find(b => /^apply$/i.test((b.innerText || '').trim()));
        let field = null;
        for (let node = apply, hops = 0; node && hops < 5 && !field; node = node.parentElement, hops++) {
            field = [...node.querySelectorAll('input')]
                .find(i => vis(i) && i.type === 'text' && !(i.name || '').startsWith('sta['));
        }
        if (apply) apply.setAttribute('data-coupon-apply', '1');
        if (field) field.setAttribute('data-coupon-field', '1');
        return !!apply && !!field;
    })()
"""

# Unity starts the checkout on "exempt from consumption tax: yes", which then demands a tax
# number and refuses the whole form, coupon included, before any request leaves the browser.
CHECKOUT_STATE_JS = """
    (() => {
        const txt = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const val = name => (document.querySelector(`[name="${name}"]`) || {}).value || '';
        const yes = document.querySelector('input[name="sta[vatRegistered]"][value="true"]');
        const region = document.querySelector('[name="sta[region]"]');
        const fields = {};
        for (const name of __FIELDS__) fields[name] = val(name);
        fields['sta[region]'] = val('sta[region]');
        fields['sta[vat]'] = val('sta[vat]');
        return JSON.stringify({
            present: !!document.querySelector('input[name="sta[vatRegistered]"]'),
            exempt: !!(yes && yes.checked),
            regionVisible: !!(region && vis(region)),
            fields: fields,
            alerts: [...document.querySelectorAll('[role=alert], [class*="rror"]')].filter(vis)
                .map(e => txt(e).slice(0, 160)).filter(Boolean).slice(0, 6),
        });
    })()
"""

TAX_NOT_EXEMPT_JS = """
    (() => {
        const no = document.querySelector('input[name="sta[vatRegistered]"][value="false"]');
        if (!no) return false;
        no.click();
        return !!no.checked;
    })()
"""

# Only the `term` boxes, the unnamed one next to them is a marketing opt-in.
TERMS_JS = """
    (() => {
        [...document.querySelectorAll('input[name="term"]')].filter(b => !b.checked).forEach(b => b.click());
        return document.querySelectorAll('input[name="term"]:checked').length;
    })()
"""


# ----------------------------------------------------------------------
# Detection (pure function, no browser)
# ----------------------------------------------------------------------

def _slug(href: str) -> str:
    """Stable per-asset identifier, the last path segment carries Unity's package id."""
    return (href or "").rstrip("/").rsplit("/", 1)[-1]


def _package_id(href: str) -> str:
    """The number the entitlements API calls `productId`, tucked at the end of the asset URL."""
    match = re.search(r"-(\d+)$", _slug(href))
    return match.group(1) if match else ""


def parse_free_asset(state: dict) -> dict | None:
    """The week's free asset, or None when the page does not offer one."""
    if not isinstance(state, dict):
        return None

    text = str(state.get("text") or "")
    coupon_match = _COUPON_RE.search(text)
    name_match = _NAME_RE.search(text)
    links = [link for link in state.get("giftLinks") or [] if (link or {}).get("href")]

    # All three must line up: no coupon means no free asset, whatever the page shows.
    if not coupon_match or not name_match or not links:
        logger.debug("No Unity giveaway found (coupon=%s, name=%s, links=%d).",
                     bool(coupon_match), bool(name_match), len(links))
        return None

    href = links[0]["href"]
    return {
        "title": name_match.group(1).strip(),
        "url": href if href.startswith("http") else URL_BASE + href,
        "slug": _slug(href),
        "package_id": _package_id(href),
        "coupon": coupon_match.group(1).strip(),
    }


def parse_total(text: str) -> float:
    """The amount the checkout still wants, -1.0 when the page does not say. Never guesses zero."""
    match = _TOTAL_RE.search(str(text or ""))
    if not match:
        return -1.0
    return float(match.group(1).replace(",", "."))


def checkout_blockers(state: dict) -> list[str]:
    """What the checkout still needs from a human, empty when the bot can carry on."""
    fields = (state or {}).get("fields") or {}
    missing = [label for name, label in REQUIRED_FIELDS.items() if not str(fields.get(name) or "").strip()]
    if (state or {}).get("regionVisible") and not str(fields.get("sta[region]") or "").strip():
        missing.append("State/Province")
    if (state or {}).get("exempt") and not str(fields.get("sta[vat]") or "").strip():
        missing.append("Tax number")
    return missing


# ----------------------------------------------------------------------
# Claimer
# ----------------------------------------------------------------------

class UnityClaimer(BaseClaimer):
    store_name = "unity"

    PAGE_STATE_JS = r"""
        (() => {
            const txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
            const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            return JSON.stringify({
                url: location.href,
                title: document.title || '',
                buttons: [...document.querySelectorAll('button, a[role=button]')].filter(vis)
                    .map(b => txt(b).slice(0, 50)).filter(Boolean).slice(0, 20),
                body: document.body ? txt(document.body).slice(0, 1200) : '',
            });
        })()
    """

    async def run(self) -> None:
        """Main entry point for the Unity claiming flow."""
        logger.debug("Starting Unity claiming flow")
        try:
            await self.start_browser()
            await self.page.get(URL_SALE)
            await self.sleep(8)

            asset = await self._detect_free_asset()
            if not asset:
                logger.info("No free asset available right now.")
                return
            logger.info("Found free Unity asset: %s", asset["title"])

            if not await self._ensure_logged_in():
                logger.error("Aborting Unity claim flow due to login failure.")
                return

            await self._claim_asset(asset)
            logger.info("Unity claimer finished.")

        except Exception as exc:
            logger.exception("Fatal error during Unity flow")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Page helpers
    # ------------------------------------------------------------------

    async def _evaluate_json(self, script: str) -> dict:
        """Run a script that returns JSON and parse it, {} when anything goes wrong."""
        try:
            raw = await self.page.evaluate(script)
            return json.loads(raw) if isinstance(raw, str) else {}
        except Exception as exc:
            logger.debug("Could not read the page state: %s", exc)
            return {}

    async def _fetch_json(self, path: str) -> dict:
        """Read one of Unity's JSON endpoints from inside the page."""
        raw = await self.page.evaluate(
            f"fetch({json.dumps(path)}, {{credentials: 'include', headers: {{'Accept': 'application/json'}}}})"
            ".then(r => r.text())",
            await_promise=True,
        )
        return json.loads(raw) if isinstance(raw, str) else {}

    async def _detect_free_asset(self) -> dict | None:
        """Read this week's giveaway off the publisher sale page."""
        state = await self._evaluate_json(SALE_STATE_JS)
        asset = parse_free_asset(state)
        if asset:
            logger.debug("Unity giveaway: %r coupon=%r url=%s", asset["title"], asset["coupon"], asset["url"])
        return asset

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _owned_ids(self) -> set:
        """Package ids already on the account, `productId` in the entitlements API."""
        try:
            data = await self._fetch_json(API_ENTITLEMENTS)
        except Exception as exc:
            logger.debug("Could not read Unity entitlements: %s", exc)
            return set()
        owned = set()
        for entry in (data or {}).get("entitlements") or []:
            if isinstance(entry, dict):
                for key in ("id", "packageId", "productId", "slug"):
                    if entry.get(key):
                        owned.add(str(entry[key]))
            elif entry:
                owned.add(str(entry))
        logger.debug("Unity entitlements holds %d id(s): %s", len(owned), sorted(owned))
        return owned

    async def _current_url(self) -> str:
        """The address the page is really on, page.url goes empty right after a redirect."""
        try:
            return str(await self.page.evaluate("window.location.href") or self.page.url)
        except Exception:
            return str(self.page.url)

    async def _is_logged_in(self) -> bool:
        """True when the orders page loads instead of bouncing to Unity ID. Navigates."""
        await self.page.get(URL_ORDERS)
        await self.sleep(6)
        url = await self._current_url()
        signed_in = url_has_allowed_host(url, STORE_HOST)
        logger.debug("Orders page landed on %s, signed in: %s", url[:110], signed_in)
        return signed_in

    async def _dismiss_cookie_banner(self) -> None:
        """Close the consent banner that otherwise covers the login form."""
        try:
            clicked = await self.page.evaluate("""
                (() => {
                    const b = document.querySelector('#onetrust-accept-btn-handler, #onetrust-reject-all-handler');
                    if (b) { b.click(); return true; }
                    return false;
                })()
            """)
            if clicked:
                logger.debug("Dismissed the Unity cookie banner.")
                await self.sleep(1.5)
        except Exception as exc:
            logger.debug("Cookie banner dismissal failed (harmless): %s", exc)

    async def _do_login(self) -> bool:
        """Sign in through Unity ID, the form asks for the email first and the password after."""
        if not url_has_allowed_host(await self._current_url(), LOGIN_HOST):
            await self.page.get(URL_ORDERS)
            await self.sleep(6)
        await self._dismiss_cookie_banner()

        if await self._human_challenge_present() and not await self._wait_out_challenge("Unity"):
            return False

        try:
            email_input = await self.page.find("#email", timeout=15)
            if not email_input:
                logger.debug("Unity ID email field did not render.")
                return False
            await email_input.click()
            await self.sleep(0.7)
            await email_input.send_keys(cfg.unity_email.strip())
            await self.sleep(0.5)
            if not await self._click_continue():
                return False
            await self.sleep(5)

            password_input = await self.page.find("input[type=password]", timeout=12)
            if not password_input:
                logger.debug("Unity ID password field did not render.")
                return False
            await password_input.click()
            await self.sleep(0.6)
            await password_input.send_keys(cfg.unity_password.strip())
            await self.sleep(0.5)
            if not await self._click_continue():
                return False
            logger.debug("Credentials entered, submitted the Unity ID form.")
        except Exception as exc:
            logger.debug("Unity ID form interaction failed: %s", exc)
            return False

        await self.sleep(10)
        return await self._is_logged_in()

    async def _click_continue(self) -> bool:
        """Press the primary submit button of the Unity ID form."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                    const btn = [...document.querySelectorAll('button[type=submit]')].filter(vis)
                        .find(b => /^(continue|sign in|log in)$/i.test((b.innerText || '').trim()));
                    if (!btn || btn.disabled) return false;
                    btn.click();
                    return true;
                })()
            """))
        except Exception as exc:
            logger.debug("Could not submit the Unity ID form: %s", exc)
            return False

    async def _ensure_logged_in(self) -> bool:
        """Confirm the Unity session, log in automatically or hand over via VNC."""
        if await self._is_logged_in():
            self.log_signed_in(cfg.unity_email or "UnityUser")
            return True

        if cfg.unity_email and cfg.unity_password:
            if await self._do_login():
                self.log_signed_in(cfg.unity_email)
                return True
        else:
            logger.warning("UNITY_EMAIL / UNITY_PASSWORD are not set, manual login needed.")

        custom_msg = self._vnc_notice(
            "Unity: login needs you",
            "Open the browser and sign in to your Unity account so the weekly free asset can be claimed.",
        )
        if await self._wait_for_vnc_login(self._is_logged_in, custom_msg=custom_msg):
            self.log_signed_in(cfg.unity_email or "UnityUser")
            return True

        logger.warning("Unity login still not completed after the VNC wait, skipping.")
        return False

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    async def _claim_asset(self, asset: dict) -> None:
        """Open the asset page, confirm it is the giveaway, then claim it with the coupon."""
        title, slug, url = asset["title"], asset["slug"], asset["url"]
        package_id = asset.get("package_id") or ""
        logger.debug("Opening Unity asset page: %s", url)

        await self.page.get(url)
        await self.sleep(6)

        if await self._human_challenge_present() and not await self._wait_out_challenge("Unity"):
            logger.warning("A security check is blocking the Unity page for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:blocked"})
            return

        state = await self._evaluate_json(self.PAGE_STATE_JS)
        logger.debug("Asset page: title=%r buttons=%r", state.get("title"), state.get("buttons"))

        if package_id and package_id in await self._owned_ids():
            logger.info("'%s' already in library.", title)
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            await self._remember(slug, title, url, "existed")
            return

        if await self._already_recorded(slug):
            logger.info("'%s' already in library.", title)
            logger.debug("Recorded by an earlier run, not claiming again.")
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            return

        if cfg.dryrun:
            logger.info("[DRYRUN] Would claim '%s' with coupon %s.", title, asset["coupon"])
            self.notify_games.append({"title": title, "url": url, "status": "available (dry run)"})
            return

        if not await self._start_checkout():
            logger.warning("Could not open the checkout for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:checkout"})
            return

        if not await self._prepare_checkout():
            logger.warning("The checkout form is still incomplete, cannot claim '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "skipped:setup"})
            return

        if not await self._apply_coupon(asset["coupon"]):
            logger.warning("Coupon %s was not accepted for '%s'.", asset["coupon"], title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:coupon"})
            return

        # Pay now stays enabled at full price, so the amount decides, never the button.
        due = await self._amount_due()
        if due != 0:
            logger.warning("'%s' still costs %s after the coupon, refusing to pay.", title, due)
            self.notify_games.append({"title": title, "url": url, "status": "failed:not-free"})
            return

        if not await self._accept_terms():
            logger.warning("Unity's asset terms were not accepted, not paying for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:terms"})
            return

        if not await self._pay_now(title, asset["coupon"]):
            logger.warning("Could not confirm the free order for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:checkout"})
            return

        await self.sleep(12)
        if package_id and package_id in await self._owned_ids():
            logger.info("✓ Claimed '%s' successfully!", title)
            status = "claimed"
        else:
            logger.warning("Claim of '%s' was not confirmed by the entitlements API, check it manually.", title)
            status = "failed:unconfirmed"

        self.notify_games.append({"title": title, "url": url, "status": status})
        if status != "failed:unconfirmed":
            await self._remember(slug, title, url, status)

    async def _start_checkout(self) -> bool:
        """Press Buy Now and answer the Terms of Service modal it opens."""
        clicked = await self.page.evaluate("""
            (() => {
                const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const btn = [...document.querySelectorAll('button, a[role=button]')].filter(vis)
                    .find(b => /buy now|add to cart/i.test((b.innerText || '').trim()));
                if (!btn || btn.disabled) return false;
                btn.click();
                return true;
            })()
        """)
        if not clicked:
            logger.debug("No Buy Now button on the asset page.")
            return False
        await self.sleep(6)

        if await self._tos_prompt_present():
            if not cfg.unity_accept_tos:
                logger.warning("Unity asks to accept its Terms of Service and UNITY_ACCEPT_TOS is off.")
                return False
            logger.debug("Accepting Unity's Terms of Service (UNITY_ACCEPT_TOS is on).")
            await self.page.evaluate("""
                (() => {
                    const b = [...document.querySelectorAll('button')]
                        .find(x => /^accept$/i.test((x.innerText || '').trim()));
                    if (b) b.click();
                })()
            """)
            await self.sleep(14)

        url = await self._current_url()
        on_checkout = url_has_allowed_host(url, CHECKOUT_HOST)
        logger.debug("Checkout landed on %s, on the payment host: %s", url[:100], on_checkout)
        return on_checkout

    async def _tos_prompt_present(self) -> bool:
        """True while the Terms of Service modal is waiting for an answer."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                    return [...document.querySelectorAll('button')].filter(vis)
                        .some(b => /^accept$/i.test((b.innerText || '').trim()));
                })()
            """))
        except Exception:
            return False

    async def _checkout_state(self) -> dict:
        """Read the checkout's billing form: what is filled in, the tax answer, visible errors."""
        script = CHECKOUT_STATE_JS.replace("__FIELDS__", json.dumps(list(REQUIRED_FIELDS)))
        return await self._evaluate_json(script)

    async def _prepare_checkout(self) -> bool:
        """Make Unity accept its own form: answer the tax question, ask for help if data is missing."""
        state = {}
        waited = 0
        while waited < 60:
            state = await self._checkout_state()
            if state.get("present"):
                break
            await self.sleep(4)
            waited += 4
        if not state.get("present"):
            logger.debug("The checkout billing form never rendered.")
            return False

        # An empty tax number with "exempt: yes" is the state Unity itself rejects.
        if state.get("exempt") and not str(state.get("fields", {}).get("sta[vat]") or "").strip():
            answered = await self.page.evaluate(TAX_NOT_EXEMPT_JS)
            logger.info("Answered Unity's consumption-tax question with No (no tax number on the account).")
            logger.debug("Tax question answered: %s", answered)
            await self.sleep(2)
            state = await self._checkout_state()

        blockers = checkout_blockers(state)
        if not blockers:
            logger.debug("Checkout form is complete, alerts=%r", state.get("alerts"))
            return True
        return await self._wait_for_manual_setup(blockers)

    async def _wait_for_manual_setup(self, blockers: list[str]) -> bool:
        """Unity needs billing details only a human can give, and it keeps them for later runs."""
        logger.warning("Unity's checkout is missing: %s", ", ".join(blockers))
        msg = self._vnc_notice(
            "Unity: checkout needs your details once",
            f"Unity will not accept the free coupon until its checkout form is complete.\n"
            f"Missing: {', '.join(blockers)}.\n"
            "Open the browser, fill those in, and answer 'Are you exempt from paying consumption "
            "tax?' with No unless you really have a tax number. Unity saves this on your account, "
            "so it is a one-time job.",
            timeout=SETUP_TIMEOUT,
        )

        async def _ready() -> bool:
            return not checkout_blockers(await self._checkout_state())

        if await self._wait_for_vnc_login(_ready, timeout=SETUP_TIMEOUT, custom_msg=msg):
            logger.info("Checkout details completed, carrying on with the claim.")
            return True
        return False

    async def _apply_coupon(self, coupon: str) -> bool:
        """Type the weekly code into the coupon box and press Apply, then wait for the total to move."""
        if not await self._wait_for_coupon_box():
            logger.debug("Coupon box and Apply button never appeared on the checkout.")
            return False
        before = await self._amount_due()
        try:
            field = await self.page.select(COUPON_FIELD, timeout=10)
            button = await self.page.select(COUPON_APPLY, timeout=10)
            if not field or not button:
                logger.debug("Coupon box or Apply button vanished before typing.")
                return False
            await field.click()
            await self.sleep(0.6)
            await field.send_keys(coupon)
            await self.sleep(1)
            await button.click()
        except Exception as exc:
            logger.debug("Could not submit the coupon: %s", exc)
            return False

        waited = 0
        while waited < 24:
            await self.sleep(4)
            waited += 4
            due = await self._amount_due()
            if due != before:
                logger.debug("Coupon %s moved the total from %s to %s.", coupon, before, due)
                return True
        state = await self._checkout_state()
        logger.debug("Coupon %s left the total at %s, page says: %r", coupon, before, state.get("alerts"))
        return False

    async def _wait_for_coupon_box(self, timeout: int = 40) -> bool:
        """The checkout renders in stages, so wait for both the box and its Apply button."""
        waited = 0
        while waited < timeout:
            try:
                ready = await self.page.evaluate(COUPON_MARK_JS)
            except Exception:
                ready = False
            if ready:
                logger.debug("Coupon box ready after %ds.", waited)
                return True
            await self.sleep(4)
            waited += 4
        return False

    async def _accept_terms(self) -> bool:
        """Tick Unity's asset EULA and the EU withdrawal waiver, both named `term`."""
        if not cfg.unity_accept_tos:
            logger.warning("Unity's asset terms need accepting and UNITY_ACCEPT_TOS is off.")
            return False
        try:
            ticked = await self.page.evaluate(TERMS_JS)
        except Exception as exc:
            logger.debug("Could not tick the terms boxes: %s", exc)
            return False
        logger.debug("Terms boxes ticked: %s", ticked)
        await self.sleep(1)
        return bool(ticked)

    async def _amount_due(self) -> float:
        """What the checkout still wants. Anything but zero, including an unreadable total, blocks paying."""
        try:
            text = await self.page.evaluate(
                "(document.body ? document.body.innerText : '').replace(/\\s+/g, ' ')")
        except Exception as exc:
            logger.debug("Could not read the checkout total: %s", exc)
            return -1.0
        amount = parse_total(text)
        if amount < 0:
            logger.debug("No 'to pay now' amount on the page, treating it as not free.")
        else:
            logger.debug("Checkout total: %.2f", amount)
        return amount

    async def _pay_now(self, title: str = "", coupon: str = "") -> bool:
        """Confirm an order that costs nothing, after reading the amount one last time."""
        due = await self._amount_due()
        if due != 0:
            logger.warning("The checkout wants %s just before paying, not clicking Pay now.", due)
            return False
        logger.info("Confirming '%s' with coupon %s, total %.2f.", title, coupon, due)
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                    const btn = [...document.querySelectorAll('button')].filter(vis)
                        .find(b => /^pay now$/i.test((b.innerText || '').trim()));
                    if (!btn || btn.disabled) return false;
                    btn.click();
                    return true;
                })()
            """))
        except Exception as exc:
            logger.debug("Could not press Pay now: %s", exc)
            return False

    async def _already_recorded(self, slug: str) -> bool:
        """True when an earlier run already stored this asset for this account."""
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame).where(
                    ClaimedGame.store == self.store_name,
                    ClaimedGame.user == (self.user or "unknown"),
                    ClaimedGame.game_id == slug,
                )
            )
            return result.scalar_one_or_none() is not None

    async def _remember(self, slug: str, title: str, url: str, status: str) -> None:
        """Record the asset so later runs can tell it apart from a new one."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session,
                store=self.store_name,
                user=self.user or "unknown",
                game_id=slug,
                title=title,
                url=url,
                status=status,
            )
            if not created and obj.status != status:
                obj.status = status
            await session.commit()
            logger.debug("DB %s '%s' (status=%s).", "stored" if created else "already had", slug, obj.status)


async def claim_unity() -> dict:
    """Convenience entry point."""
    claimer = UnityClaimer()
    await claimer.run()
    return {"store": "Unity", "user": claimer.user, "games": claimer.notify_games}
