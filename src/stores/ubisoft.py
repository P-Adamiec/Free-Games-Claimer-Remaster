"""Ubisoft store module, claims the free game giveaways from ubisoft.com/games/free.

Giveaways share the "gametrial" type with real trials, so the feed filter is strict and
the claim page decides: "GET YOUR FREE GAME!" versus a trial's "TRY NOW FOR FREE!".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
import nodriver as uc
import pyotp

from src.core.claimer import BaseClaimer
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.ubisoft")

URL_FREE = "https://www.ubisoft.com/en-us/games/free"
URL_ACCOUNT = "https://account.ubisoft.com/en-US/account-information"
CLAIM_HOST = "register.ubisoft.com"
LOGIN_HOST = "connect.ubisoft.com"
ACCOUNT_HOST = "account.ubisoft.com"
WWW_HOST = "www.ubisoft.com"

STATE_MARKER = "window.__PRELOADED_STATE__ = "
FEED_PLACEMENT = "freeevents"
# Giveaways carry the trial type, so type alone never decides, see the filter below.
GIVEAWAY_TYPES = ("gametrial",)
# A giveaway is never named after a trial-like offer; "free week" also covers "free weekend".
SKIP_TOKENS = ("trial", "demo", "beta", "free week", "early access", "test server", "playtest")
# Trials use a sentinel expiry decades out, a real giveaway runs for days.
MAX_WINDOW_DAYS = 90

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# Claiming runs through a Ubisoft Connect overlay served in a cross-origin iframe.
OVERLAY_PATH = "connect.ubisoft.com/login"

OVERLAY_STATE_JS = """
function () {
    return JSON.stringify({
        inputs: [...this.querySelectorAll('input')].map(i => i.id || i.type),
        buttons: [...this.querySelectorAll('button')].map(b => (b.innerText || '').trim().slice(0, 30)),
        text: (this.body ? (this.body.innerText || '').trim().replace(/\\s+/g, ' ') : '').slice(0, 300),
    });
}
"""

# "Welcome back, <user>" only needs a confirmation click; never touch "Not you?".
OVERLAY_CONTINUE_JS = """
function () {
    const buttons = [...this.querySelectorAll('button')];
    const named = buttons.find(b => /continue|kontynuuj|weiter|continuer/i.test((b.innerText || '').trim()));
    const only = buttons.length === 1 ? buttons[0] : null;
    const target = named || only;
    if (!target) return JSON.stringify(false);
    target.click();
    return JSON.stringify(true);
}
"""


# ----------------------------------------------------------------------
# Detection (pure functions, no browser)
# ----------------------------------------------------------------------

def _parse_date(value) -> datetime | None:
    """Read one of Ubisoft's naive ISO timestamps, None when unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None


def _extract_state(html: str) -> dict:
    """Pull the JSON blob the free games page embeds in its HTML."""
    if not isinstance(html, str) or STATE_MARKER not in html:
        return {}
    start = html.index(STATE_MARKER) + len(STATE_MARKER)
    end = html.find("</script>", start)
    if end == -1:
        return {}
    try:
        return json.loads(html[start:end].rstrip().rstrip(";"))
    except (ValueError, TypeError):
        logger.debug("Ubisoft page state is not valid JSON, ignoring it.")
        return {}


def _news_entries(state: dict) -> list[dict]:
    """Every feed entry from the page state, deduplicated across its personalisation blocks."""
    blocks = state.get("NewsPersonalization") or {}
    if not isinstance(blocks, dict):
        return []
    entries: dict[str, dict] = {}
    for block in blocks.values():
        news = ((block or {}).get("data") or {}).get("news") or []
        for entry in news:
            if isinstance(entry, dict):
                entries.setdefault(str(entry.get("newsId") or entry.get("title")), entry)
    return list(entries.values())


def _claim_link(entry: dict) -> str:
    """The register.ubisoft.com link of an entry, empty when it points anywhere else."""
    for link in entry.get("links") or []:
        param = (link or {}).get("param") or ""
        if url_has_allowed_host(param, CLAIM_HOST):
            return param
    return ""


def _slug(url: str) -> str:
    """Stable per-offer identifier, e.g. 'ghost-recon-future-soldier'."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[0] if parts else ""


def _giveaway_reason(entry: dict, now: datetime) -> str:
    """Empty when the entry is a claimable giveaway, otherwise why it was rejected."""
    if entry.get("placement") != FEED_PLACEMENT:
        return f"placement={entry.get('placement')}"
    if entry.get("type") not in GIVEAWAY_TYPES:
        return f"type={entry.get('type')}"

    url = _claim_link(entry)
    if not url:
        return "no register.ubisoft.com link"

    haystack = f"{entry.get('title') or ''} {_slug(url)}".lower()
    for token in SKIP_TOKENS:
        if token in haystack:
            return f"named like a {token}"

    ends = _parse_date(entry.get("expirationDate"))
    if not ends:
        return "no end date"
    if ends <= now:
        return f"ended {entry.get('expirationDate')}"
    if ends - now > timedelta(days=MAX_WINDOW_DAYS):
        return f"end date {entry.get('expirationDate')} is not a real promo window"

    starts = _parse_date(entry.get("publicationDate"))
    if starts and starts > now:
        return f"starts {entry.get('publicationDate')}"
    return ""


def parse_free_games(html: str, now: datetime | None = None) -> list[dict]:
    """Find the running giveaways in the free games page HTML."""
    now = now or datetime.now()
    entries = _news_entries(_extract_state(html))
    logger.debug("Ubisoft feed carries %d entries.", len(entries))

    games, rejected = [], []
    for entry in entries:
        reason = _giveaway_reason(entry, now)
        if reason:
            rejected.append(f"{entry.get('title')!r} ({reason})")
            continue
        url = _claim_link(entry)
        games.append({
            "title": str(entry.get("title") or "Ubisoft giveaway").strip(),
            "url": url,
            "slug": _slug(url),
            "ends": entry.get("expirationDate"),
        })

    if rejected:
        logger.debug("Ubisoft entries skipped: %s", ", ".join(rejected))
    return games


async def fetch_free_games() -> list[dict]:
    """Download the free games page and return the running giveaways, [] on any failure."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     headers={"User-Agent": BROWSER_UA}) as client:
            response = await client.get(URL_FREE)
            response.raise_for_status()
            logger.debug("Fetched %s (%d bytes).", URL_FREE, len(response.text))
            return parse_free_games(response.text)
    except Exception as exc:
        logger.warning("Could not read the Ubisoft free games page: %s", exc)
        return []


# ----------------------------------------------------------------------
# Claimer
# ----------------------------------------------------------------------

class UbisoftClaimer(BaseClaimer):
    store_name = "ubisoft"

    PAGE_STATE_JS = r"""
        (() => {
            const txt = el => (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
            const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            return JSON.stringify({
                url: location.href,
                title: document.title || '',
                headings: [...document.querySelectorAll('h1,h2,h3')].filter(vis).map(h => txt(h)),
                body: (document.body ? txt(document.body) : '').slice(0, 2500),
                platforms: [...document.querySelectorAll('.sitegen-platform-button')].map(b => txt(b)),
                loginFrame: (document.querySelector('iframe[src*="connect.ubisoft.com/login"]') || {}).src || '',
            });
        })()
    """

    async def run(self) -> None:
        """Main entry point for the Ubisoft claiming flow."""
        logger.debug("Starting Ubisoft claiming flow")
        try:
            games = await fetch_free_games()
            if not games:
                logger.info("No free giveaway currently available.")
                return
            logger.info("Found %d Ubisoft giveaway(s): %s", len(games), ", ".join(g["title"] for g in games))

            await self.start_browser()
            await self.page.get(URL_FREE)
            await self.sleep(3)
            await self._dismiss_cookie_banner()

            if not await self._ensure_logged_in():
                logger.error("Aborting Ubisoft claim flow due to login failure.")
                return

            for game in games:
                await self._claim_game(game)

            logger.info("Ubisoft claimer finished.")

        except Exception as exc:
            logger.exception("Fatal error during Ubisoft flow")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Page helpers
    # ------------------------------------------------------------------

    async def _page_state(self) -> dict:
        """Read title, headings, visible text and platform buttons in one go."""
        try:
            raw = await self.page.evaluate(self.PAGE_STATE_JS)
            return json.loads(raw) if isinstance(raw, str) else {}
        except Exception as exc:
            logger.debug("Could not read the page state: %s", exc)
            return {}

    async def _dismiss_cookie_banner(self) -> None:
        """Close the cookie modal that otherwise covers the claim button."""
        try:
            clicked = await self.page.evaluate("""
                (() => {
                    const b = document.querySelector('#privacy__modal__accept, #onetrust-accept-btn-handler, #accept-recommended-btn-handler');
                    if (b) { b.click(); return true; }
                    return false;
                })()
            """)
            if clicked:
                logger.debug("Dismissed the Ubisoft cookie banner.")
                await self.sleep(1.5)
        except Exception as exc:
            logger.debug("Cookie banner dismissal failed (harmless): %s", exc)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _current_url(self) -> str:
        """The address the page is really on, page.url goes empty right after a redirect."""
        try:
            return str(await self.page.evaluate("window.location.href") or self.page.url)
        except Exception:
            return str(self.page.url)

    async def _is_logged_in(self) -> bool:
        """True when the account portal shows the account instead of its login page."""
        url = await self._current_url()
        # account.ubisoft.com hands the portal over to www.ubisoft.com, so both hosts count.
        path = urlsplit(url).path.lower().rstrip("/")
        signed_in = (
            (url_has_allowed_host(url, ACCOUNT_HOST) or url_has_allowed_host(url, WWW_HOST))
            and "/account" in path
            and not path.endswith("/account/login")
        )
        logger.debug("Account portal landed on %s, signed in: %s", url, signed_in)
        return signed_in

    async def _account_name(self) -> str:
        """The signed-in username as shown by the account portal."""
        try:
            name = await self.page.evaluate("""
                (() => {
                    const label = [...document.querySelectorAll('label')]
                        .find(l => /^username$/i.test((l.innerText || '').trim()));
                    const field = label && label.htmlFor ? document.getElementById(label.htmlFor) : null;
                    return field && field.value ? field.value.trim() : '';
                })()
            """)
            if isinstance(name, str) and name.strip():
                return name.strip()[:60]
        except Exception as exc:
            logger.debug("Could not read the account name: %s", exc)
        return cfg.ubi_email or "UbisoftUser"

    async def _ensure_logged_in(self) -> bool:
        """Confirm the Ubisoft session, log in automatically or hand over via VNC."""
        await self.page.get(URL_ACCOUNT)
        await self.sleep(4)
        await self._dismiss_cookie_banner()

        if await self._is_logged_in():
            self.log_signed_in(await self._account_name())
            return True

        outcome = "failed"
        if cfg.ubi_email and cfg.ubi_password:
            outcome = await self._do_login()
            if outcome == "ok":
                self.log_signed_in(await self._account_name())
                return True
        else:
            logger.warning("UBI_EMAIL / UBI_PASSWORD are not set, manual login needed.")

        if outcome == "mfa":
            logger.warning("Ubisoft asked for a two-step verification code, requesting it via VNC.")
            custom_msg = self._vnc_notice(
                "Ubisoft: 2FA code needed",
                "Enter the code Ubisoft sent to your email (or from your authenticator app) in the browser. "
                "Set UBI_OTPKEY to let the bot fill authenticator codes by itself.",
            )
        else:
            custom_msg = self._vnc_notice(
                "Ubisoft: login needs you",
                "Automated sign-in did not complete. Open the browser and finish logging in to Ubisoft Connect.",
            )

        async def _check() -> bool:
            url = await self._current_url()
            # Never navigate while a sign-in screen is open, it would wipe what the user typed.
            on_login_screen = url_has_allowed_host(url, LOGIN_HOST) or (
                url_has_allowed_host(url, WWW_HOST) and "/account/login" in urlsplit(url).path.lower()
            )
            if on_login_screen:
                return False
            await self.page.get(URL_ACCOUNT)
            await self.sleep(3)
            return await self._is_logged_in()

        if await self._wait_for_vnc_login(_check, custom_msg=custom_msg):
            self.log_signed_in(await self._account_name())
            return True

        logger.warning("Ubisoft login still not completed after the VNC wait, skipping.")
        return False

    async def _mfa_prompt_present(self) -> bool:
        """True on Ubisoft's two-step verification code screen."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    if (document.querySelector('#AuthCode, input[name*="code" i], input[id*="otp" i], input[type="tel"]')) return true;
                    const t = (document.body ? document.body.innerText : '').toLowerCase();
                    return t.includes('two-factor') || t.includes('two step') || t.includes('verification code')
                        || t.includes('authentication code') || t.includes('security code');
                })()
            """))
        except Exception:
            return False

    async def _fill_totp(self) -> bool:
        """Auto-enter the authenticator code from UBI_OTPKEY, then submit."""
        if not cfg.ubi_otpkey:
            return False
        try:
            field = await self.page.find('#AuthCode, input[name*="code" i], input[type="tel"]', timeout=5)
            if not field:
                return False
            logger.debug("Entering the Ubisoft two-step code from UBI_OTPKEY.")
            await field.clear_input()
            await self.sleep(0.4)
            await field.send_keys(pyotp.TOTP(cfg.ubi_otpkey).now())
            await self.sleep(0.8)
            await self.page.evaluate(
                "(() => { const b = document.querySelector('button.btn-primary'); if (b) b.click(); })()"
            )
            await self.sleep(4)
            return True
        except Exception as exc:
            logger.debug("Could not enter the Ubisoft two-step code: %s", exc)
            return False

    async def _do_login(self) -> str:
        """Fill the Ubisoft Connect login form. Returns 'ok', 'mfa' or 'failed'."""
        state = await self._page_state()
        login_url = state.get("loginFrame") or ""
        # The login form lives in a cross-origin iframe, so open its own URL as a normal page.
        if not url_has_allowed_host(login_url, LOGIN_HOST):
            logger.debug("No Ubisoft Connect login frame on the page (src=%r).", login_url)
            return "failed"

        logger.debug("Opening the Ubisoft Connect login page directly.")
        await self.page.get(login_url)
        await self.sleep(4)

        if await self._human_challenge_present() and not await self._wait_out_challenge("Ubisoft"):
            return "failed"

        try:
            email_input = await self.page.find("#AuthEmail", timeout=15)
            if not email_input:
                logger.debug("Ubisoft login form did not render.")
                return "failed"
            await email_input.click()
            await self.sleep(0.8)
            await email_input.send_keys(cfg.ubi_email.strip())
            await self.sleep(0.5)

            password_input = await self.page.find("#AuthPassword", timeout=10)
            if not password_input:
                logger.debug("Ubisoft password field did not render.")
                return "failed"
            await password_input.click()
            await self.sleep(0.6)
            await password_input.send_keys(cfg.ubi_password.strip())
            await self.sleep(0.5)

            # Only tick "Keep me logged" when it is off, clicking a ticked box signs us out next run.
            if not await self.page.evaluate('!!document.querySelector("#RememberMe")?.checked'):
                remember = await self.page.find("#RememberMe", timeout=3)
                if remember:
                    await remember.click()
                    await self.sleep(0.5)

            # Only a DOM click reaches this Angular handler, and the buttons carry no type attribute.
            if not await self.page.evaluate(
                "(() => { const b = document.querySelector('button.btn-primary'); if (!b) return false; b.click(); return true; })()"
            ):
                logger.debug("Ubisoft LOG IN button (button.btn-primary) not found.")
                return "failed"
            logger.debug("Credentials entered, submitted the form with a DOM click.")
        except Exception as exc:
            logger.debug("Ubisoft login form interaction failed: %s", exc)
            return "failed"

        return await self._await_login_outcome()

    async def _await_login_outcome(self, timeout: int = 45) -> str:
        """Watch the login page until the session lands, a code is asked for, or time runs out."""
        otp_tried = False
        waited = 0
        while waited < timeout:
            await self.sleep(3)
            waited += 3

            # Left connect.ubisoft.com means the sign-in went through and redirected onward.
            current = await self._current_url()
            if current and not url_has_allowed_host(current, LOGIN_HOST):
                break

            if await self._mfa_prompt_present():
                if cfg.ubi_otpkey and not otp_tried:
                    otp_tried = True
                    await self._fill_totp()
                    continue
                if not cfg.ubi_otpkey:
                    # Stay on the code screen so the user can finish it over VNC.
                    logger.debug("Login outcome: mfa, a two-step code is due and UBI_OTPKEY is not set.")
                    return "mfa"

            if await self._human_challenge_present() and not await self._wait_out_challenge("Ubisoft"):
                return "failed"

        logger.debug("Login watch ended after %ds at %s", waited, (await self._current_url())[:120])
        await self.page.get(URL_ACCOUNT)
        await self.sleep(4)
        if await self._is_logged_in():
            logger.debug("Login outcome: ok")
            return "ok"

        state = await self._page_state()
        logger.debug("Login outcome: failed, page says: %r", (state.get("body") or "")[:400])
        return "failed"

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def _claim_url(self, url: str) -> str:
        """Pin the claim page to English so its wording can be matched."""
        return url.rstrip("/") + "/en-US"

    def _looks_like_giveaway(self, state: dict) -> bool:
        """True only for a real giveaway page, trials and demos word it differently."""
        title = (state.get("title") or "").strip().lower()
        headings = " ".join(state.get("headings") or []).lower()
        return title == "giveaway" or "get your free game" in headings

    def _already_owned(self, state: dict) -> bool:
        """True when the page says the account already has the game."""
        text = (state.get("body") or "").lower()
        return any(m in text for m in ("already own", "already in your", "already have this", "you own this"))

    def _claim_confirmed(self, state: dict) -> bool:
        """True when the page confirms the game landed on the account.

        A claim reads "Have fun!" over "The game has been successfully added ... library".
        """
        text = (state.get("body") or "").lower()
        headings = " ".join(state.get("headings") or []).lower()
        if "have fun" in headings:
            return True
        return any(m in text for m in (
            "has been successfully added", "has been added to your", "added to your ubisoft",
            "is now yours", "you now own",
        ))

    async def _claim_game(self, game: dict) -> None:
        """Open one giveaway page, confirm what it is, then claim it."""
        title, slug = game["title"], game["slug"]
        url = self._claim_url(game["url"])
        logger.debug("Opening Ubisoft giveaway page: %s (ends %s)", url, game.get("ends"))

        await self.page.get(url)
        await self.sleep(5)

        # Ubisoft fronts these pages with DataDome, so tell a challenge apart from a wrong page.
        if await self._human_challenge_present() and not await self._wait_out_challenge("Ubisoft"):
            logger.warning("A security check is blocking the giveaway page for '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:blocked"})
            return

        await self._dismiss_cookie_banner()
        state = await self._page_state()
        logger.debug("Claim page: title=%r headings=%r platforms=%r",
                     state.get("title"), state.get("headings"), state.get("platforms"))

        if not self._looks_like_giveaway(state):
            logger.info("'%s' is not a giveaway page, skipping.", title)
            self.notify_games.append({"title": title, "url": url, "status": "skipped:not-a-giveaway"})
            return

        if self._already_owned(state):
            logger.info("'%s' already in library.", title)
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            await self._remember(slug, title, url, "existed")
            return

        # The page never shows ownership, so an earlier claim in the DB is the only signal.
        if await self._already_recorded(slug or title):
            logger.info("'%s' already in library.", title)
            logger.debug("Recorded by an earlier run, not clicking the claim again.")
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            return

        if cfg.dryrun:
            logger.info("[DRYRUN] Would claim '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "available (dry run)"})
            return

        if not await self._click_pc_platform():
            logger.warning("Could not start the claim for '%s', the PC button was not clickable.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed"})
            return

        # Clicking a platform opens the Ubisoft Connect overlay, which wants the account confirmed.
        await self.sleep(8)
        if await self._confirm_account_overlay() == "login":
            logger.warning("Ubisoft wants a fresh sign-in for '%s' before it hands the game over.", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed:login"})
            return

        await self.sleep(10)
        state = await self._page_state()
        logger.debug("Ubisoft page after the claim click: %r", (state.get("body") or "")[:600])

        if self._already_owned(state):
            logger.info("'%s' already in library.", title)
            status = "existed"
        elif self._claim_confirmed(state):
            logger.info("✓ Claimed '%s' successfully!", title)
            status = "claimed"
        else:
            logger.warning("Claim of '%s' was not confirmed by the page, check it manually.", title)
            status = "failed:unconfirmed"

        self.notify_games.append({"title": title, "url": url, "status": status})
        if status != "failed:unconfirmed":
            await self._remember(slug, title, url, status)

    # ------------------------------------------------------------------
    # Ubisoft Connect overlay (a cross-origin iframe)
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

    async def _overlay_document(self):
        """The overlay's document, reachable only by piercing the cross-origin iframe."""
        try:
            doc = await self.page.send(uc.cdp.dom.get_document(depth=-1, pierce=True))
        except Exception as exc:
            logger.debug("Could not read the pierced DOM: %s", exc)
            return None
        for node in self._walk_nodes(doc):
            if node.node_name == "IFRAME" and OVERLAY_PATH in self._node_attrs(node).get("src", ""):
                return getattr(node, "content_document", None)
        return None

    async def _overlay_eval(self, document, function_declaration: str):
        """Run JS inside the overlay iframe, the parent document cannot reach into it."""
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
            logger.debug("Could not evaluate inside the Ubisoft Connect overlay: %s", exc)
            return None

    async def _confirm_account_overlay(self) -> str:
        """Answer the 'Welcome back, continue?' overlay. Returns 'none', 'confirmed' or 'login'."""
        document = await self._overlay_document()
        if document is None:
            return "none"

        state = await self._overlay_eval(document, OVERLAY_STATE_JS) or {}
        logger.debug("Ubisoft Connect overlay: %r", (state.get("text") or "")[:200])

        if state.get("inputs"):
            logger.debug("The overlay is asking for credentials, the site session did not carry over.")
            return "login"

        if await self._overlay_eval(document, OVERLAY_CONTINUE_JS):
            logger.debug("Confirmed the account in the Ubisoft Connect overlay.")
            return "confirmed"

        logger.debug("Overlay had no button to confirm: %r", state)
        return "login"

    async def _click_pc_platform(self) -> bool:
        """Click the 'Ubisoft Connect PC' tile that starts the claim."""
        try:
            raw = await self.page.evaluate("""
                (() => {
                    const tiles = [...document.querySelectorAll('.sitegen-platform-button')];
                    const pc = document.querySelector('.sitegen-platform-button.sitegen-icon-uplay') || tiles[0];
                    if (!pc) return JSON.stringify({clicked: false, tiles: tiles.length, picked: null});
                    pc.scrollIntoView({block: 'center'});
                    pc.click();
                    return JSON.stringify({
                        clicked: true,
                        tiles: tiles.length,
                        picked: (pc.innerText || '').trim().slice(0, 40),
                        byUplayClass: pc.classList.contains('sitegen-icon-uplay'),
                    });
                })()
            """)
            result = json.loads(raw) if isinstance(raw, str) else {}
            logger.debug("Platform tiles: %d, clicked %r (uplay class: %s)",
                         result.get("tiles", 0), result.get("picked"), result.get("byUplayClass"))
            return bool(result.get("clicked"))
        except Exception as exc:
            logger.debug("Clicking the Ubisoft platform button failed: %s", exc)
            return False

    async def _already_recorded(self, game_id: str) -> bool:
        """True when an earlier run already stored this giveaway for this account."""
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame).where(
                    ClaimedGame.store == self.store_name,
                    ClaimedGame.user == (self.user or "unknown"),
                    ClaimedGame.game_id == game_id,
                )
            )
            return result.scalar_one_or_none() is not None

    async def _remember(self, slug: str, title: str, url: str, status: str) -> None:
        """Record the offer so later runs can tell it apart from a new one."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session,
                store=self.store_name,
                user=self.user or "unknown",
                game_id=slug or title,
                title=title,
                url=url,
                status=status,
            )
            if not created and obj.status != status:
                obj.status = status
            await session.commit()
            logger.debug("DB %s '%s' (status=%s).", "stored" if created else "already had", slug or title, obj.status)


async def claim_ubisoft() -> dict:
    """Convenience entry point."""
    claimer = UbisoftClaimer()
    await claimer.run()
    return {"store": "Ubisoft", "user": claimer.user, "games": claimer.notify_games}
