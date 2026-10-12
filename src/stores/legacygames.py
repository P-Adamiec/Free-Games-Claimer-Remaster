"""Legacy Games: redeems the codes Prime Gaming hands out for Legacy Games into the account in LG_EMAIL / LG_PASSWORD.

Each offer has its own promo page, linked from the Prime claim page; a redeemed code shows up under My Free Games.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from src.core.claimer import BaseClaimer, filenamify, mask_account
from src.core.config import cfg
from src.core.database import async_session, ClaimedGame
from src.core.run_state import needs_you
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.legacygames")

URL_ACCOUNT = "https://legacygames.com/my-account/"
# Redeemed codes are listed here, not under Orders (checked live on 2026-10-09).
URL_FREE_GAMES = "https://legacygames.com/my-account/free-games/"
SITE_HOST = "legacygames.com"
PROMO_HOST = "promo.legacygames.com"
# The value prime.py writes into extra["external_store"] for these codes.
EXTERNAL_STORE = "legacy games"
# Prime's claim slug ends in "-legacy"; the promo page carries the same name with one of these endings.
PROMO_SUFFIXES = ("-luna", "-prime-deal")
# Codes are only worth sending while their offer can still be live; older ones are left alone.
PENDING_DAYS = 60

SESSION_JS = """
    JSON.stringify({
        href: location.href,
        logout: [...document.querySelectorAll('a[href]')].some(a => (a.getAttribute('href') || '').includes('customer-logout')),
        login: !!document.querySelector('form.woocommerce-form-login'),
    })
"""

# innerText leaves out hidden elements, and the error line sits hidden in every fresh form.
PROMO_STATE_JS = """
    JSON.stringify({
        href: location.href,
        form: !!document.querySelector('input[name="coupon_code"]'),
        text: document.body ? document.body.innerText : '',
    })
"""

# The promo page ticks the newsletter box for you, so it is always unticked.
NEWSLETTER_OFF_JS = """
    (() => {
        const box = document.querySelector('input[name="newsletter_sub"]');
        if (box && box.checked) box.click();
        return box ? !box.checked : true;
    })()
"""


def legacy_redeem_link(hrefs: list) -> str:
    """The promo page Prime links to as 'Click here', or '' when the page has none."""
    for href in hrefs or []:
        if url_has_allowed_host(str(href), PROMO_HOST):
            return str(href)
    return ""


def legacy_promo_candidates(prime_url: str, stored_url: str = "") -> list:
    """Promo pages to try for a code: the one Prime linked first, then the ones its name points to."""
    found = []
    if stored_url and url_has_allowed_host(stored_url, PROMO_HOST):
        found.append(stored_url)
    parts = [p for p in urlsplit(prime_url or "").path.split("/") if p]
    # Only an offer's own page, /claims/<game>/dp/<id>, names the game.
    at = parts.index("claims") if "claims" in parts else -1
    if at >= 0 and parts[at + 2:at + 3] == ["dp"]:
        slug = re.sub(r"-legacy$", "", parts[at + 1])
        for suffix in PROMO_SUFFIXES:
            url = f"https://{PROMO_HOST}/{slug}{suffix}/"
            if url not in found:
                found.append(url)
    return found


def legacy_listed(rows: list, title: str) -> bool:
    """True when a My Free Games row is this game; 'Rose Riddle' must not match 'Rose Riddle 2: Werewolf Shadow'."""
    norm = BaseClaimer._normalize_title
    want = norm(title)
    if not want:
        return False
    for row in rows or []:
        cells = [str(c or "") for c in row]
        product = re.split(r"\s[–-]\s", cells[0], maxsplit=1)[-1] if cells else ""
        game = cells[1] if len(cells) > 1 else ""
        # The game column adds a "1" or a CE to the Prime title ("The Da Vinci Cryptex 1"); any other number is a sequel.
        if norm(product) == want or re.fullmatch(re.escape(want) + r"(ce|collectorsedition)?1?", norm(game)):
            return True
    return False


def legacy_redeem_outcome(state: dict) -> str:
    """'redeemed', 'rejected' (a used or expired code) or 'unknown', from the page after sending the form."""
    state = state or {}
    text = str(state.get("text") or "")
    # The thank-you page replaces the form (seen live), so the form still showing means no answer yet.
    if re.search(r"thanks for redeeming", text, re.I) and not state.get("form"):
        return "redeemed"
    if re.search(r"please check your code or email", text, re.I):
        return "rejected"
    return "unknown"


def _extra(row) -> dict:
    """The row's extra JSON as a dict, empty when there is none."""
    try:
        return json.loads(row.extra or "{}") or {}
    except (TypeError, ValueError):
        return {}


class LegacyGamesClaimer(BaseClaimer):
    store_name = "legacygames"

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _session_state(self) -> str:
        """'in' with a logout link, 'out' with the login form, 'unknown' on anything else (not the site, error, check)."""
        try:
            raw = await self.page.evaluate(SESSION_JS)
            state = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("Sign-in check failed: %s", e)
            return "unknown"
        if not url_has_allowed_host(str(state.get("href") or ""), SITE_HOST, allow_subdomains=True):
            return "unknown"
        if state.get("logout") and not state.get("login"):
            return "in"
        if state.get("login") and not state.get("logout"):
            return "out"
        return "unknown"

    async def _is_logged_in(self) -> bool:
        return await self._session_state() == "in"

    async def _state_after_load(self, seconds: int = 10) -> str:
        """The session state, asked again until the page shows one."""
        waited = 0
        while True:
            state = await self._session_state()
            if state != "unknown" or waited >= seconds:
                return state
            await self.sleep(2)
            waited += 2

    async def _reject_cookies(self) -> None:
        try:
            await self.page.evaluate("""(() => { const b = document.querySelector('.cky-btn-reject'); if (b) b.click(); })()""")
            await self.sleep(1)
        except Exception as e:
            logger.debug("Could not answer the cookie banner: %s", e)

    async def _type_into(self, selector: str, text: str) -> bool:
        field = await self.page.select(selector, timeout=8)
        if not field:
            logger.debug("No field '%s' on %s", selector, await self._page_brief())
            return False
        await field.click()
        await self.sleep(0.4)
        await field.clear_input()
        await field.send_keys(text)
        await self.sleep(0.5)
        return True

    async def _submit_login(self) -> None:
        """Type LG_EMAIL / LG_PASSWORD into the account page's form, keep the session, press 'Log in'."""
        if not (await self._type_into("#username", cfg.lg_email) and await self._type_into("#password", cfg.lg_password)):
            return
        await self.page.evaluate("""(() => { const r = document.querySelector('#rememberme'); if (r && !r.checked) r.click(); })()""")
        button = await self.page.select('button[name="login"]', timeout=8)
        if button:
            await button.click()
            await self.sleep(6)

    async def _ensure_logged_in(self) -> bool:
        """Signed in to Legacy Games, by itself with LG_EMAIL / LG_PASSWORD, else over VNC."""
        await self.page.get(URL_ACCOUNT)
        await self.sleep(5)
        await self._reject_cookies()
        if await self._human_challenge_present():
            if not await self._wait_out_challenge("Legacy Games", store_key=self.store_name):
                return False

        state = await self._state_after_load()
        if state == "in":
            self.log_signed_in(cfg.lg_email)
            return True
        if state == "out":
            logger.info("Signing in to Legacy Games as %s…", mask_account(cfg.lg_email))
            await self._submit_login()
            if await self._state_after_load() == "in":
                self.log_signed_in(cfg.lg_email)
                return True
            notice = await self.page.evaluate(
                "(document.querySelector('.woocommerce-error') || {}).innerText || ''")
            notice = re.sub(r"[\w.+-]+@", lambda m: m.group(0)[0] + "***@", str(notice or "")[:160])
            logger.debug("Sign-in did not go through: %s", notice or "no message")
        else:
            logger.debug("Could not read the session on %s", await self._page_brief())

        msg = self._vnc_notice("Legacy Games: login needs you",
                               "The automated sign-in did not finish. Open the browser and sign in to Legacy Games.")
        if await self._wait_for_vnc_login(self._is_logged_in, custom_msg=msg, store_key=self.store_name):
            self.log_signed_in(cfg.lg_email)
            return True
        return False

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    async def _pending_codes(self) -> list:
        """Prime's Legacy Games codes from the last PENDING_DAYS that are claimed and not redeemed yet."""
        from sqlalchemy import select

        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=PENDING_DAYS)
        async with async_session() as session:
            rows = (await session.execute(select(ClaimedGame).where(
                ClaimedGame.store == "prime",
                ClaimedGame.status == "claimed",
                ClaimedGame.code.isnot(None),
                ClaimedGame.code != "",
                ClaimedGame.created_at >= since,
            ))).scalars().all()
        return [r for r in rows if str(_extra(r).get("external_store", "")).strip().lower() == EXTERNAL_STORE]

    async def _set_status(self, row_id: int, status: str, promo_url: str = "") -> None:
        async with async_session() as session:
            obj = await session.get(ClaimedGame, row_id)
            if not obj:
                return
            if promo_url:
                extra = _extra(obj)
                extra["redeem_url"] = promo_url
                obj.extra = json.dumps(extra)
            obj.status = status
            await session.commit()

    async def _in_free_games(self, title: str) -> bool:
        """True when My Free Games lists the game; the title also sits in an ad on every page, so only the table counts."""
        await self.page.get(URL_FREE_GAMES)
        await self.sleep(4)
        try:
            rows = json.loads(await self.page.evaluate("""JSON.stringify([...document.querySelectorAll(
                '.woocommerce-MyAccount-content table tr')].map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim())))"""))
        except Exception as e:
            logger.debug("Could not read My Free Games: %s", e)
            return False
        found = legacy_listed(rows, title)
        logger.debug("'%s' %s in your Legacy Games free games (%d rows).", title, "is" if found else "is not", len(rows))
        return found

    async def _open_promo(self, candidates: list, title: str) -> str:
        """The first candidate that is a live promo page for this game, or ''."""
        for url in candidates:
            try:
                await self.page.get(url)
                await self.sleep(4)
                await self._reject_cookies()
                state = json.loads(await self.page.evaluate(PROMO_STATE_JS))
            except Exception as e:
                logger.debug("Could not read '%s': %s", url, e)
                continue
            on_promo = url_has_allowed_host(str(state.get("href") or ""), PROMO_HOST)
            names_it = self._normalize_title(title) in self._normalize_title(state.get("text") or "")
            logger.debug("Promo page %s: on promo=%s, form=%s, names the game=%s", url, on_promo, state.get("form"), names_it)
            if on_promo and state.get("form") and names_it:
                return url
        return ""

    async def _send_code(self, code: str) -> str:
        """Fill the promo form for LG_EMAIL with the newsletter unticked, send it, and read the answer."""
        if not (await self._type_into('input[name="coupon_code"]', code)
                and await self._type_into('input[name="email"]', cfg.lg_email)
                and await self._type_into('input[name="email_validate"]', cfg.lg_email)):
            return "unknown"
        # A dropped keystroke would read as "check your code" and end this code for good, so the fields are read back.
        typed = json.loads(await self.page.evaluate("""JSON.stringify(['coupon_code', 'email', 'email_validate'].map(
            n => ((document.querySelector('input[name="' + n + '"]') || {}).value || '').trim().toLowerCase()))"""))
        if typed != [code.strip().lower(), cfg.lg_email.strip().lower(), cfg.lg_email.strip().lower()]:
            logger.debug("The promo form does not hold what was typed, not sending it.")
            return "unknown"
        if not await self.page.evaluate(NEWSLETTER_OFF_JS):
            logger.warning("Could not untick the Legacy Games newsletter box, so the code is not sent.")
            return "unknown"
        button = await self.page.select("#submitbutton", timeout=8)
        if not button:
            logger.debug("No submit button on %s", await self._page_brief())
            return "unknown"
        await button.click()
        outcome = "unknown"
        for _ in range(10):
            await self.sleep(2)
            try:
                outcome = legacy_redeem_outcome(json.loads(await self.page.evaluate(PROMO_STATE_JS)))
            except Exception as e:
                logger.debug("Could not read the answer page: %s", e)
            if outcome != "unknown":
                break
        logger.debug("Legacy Games answered '%s' on %s", outcome, await self._page_brief())
        return outcome

    async def _redeem(self, row) -> None:
        title, code = row.title, row.code
        if await self._in_free_games(title):
            logger.info("'%s' is already in your Legacy Games free games.", title)
            await self._set_status(row.id, "already redeemed")
            self.notify_games.append({"title": title, "url": URL_FREE_GAMES, "status": "already redeemed (Legacy Games)"})
            return

        promo = await self._open_promo(legacy_promo_candidates(row.url, _extra(row).get("redeem_url", "")), title)
        if not promo:
            logger.warning("No Legacy Games redeem page found for '%s', redeem the code yourself.", title)
            if not cfg.dryrun:
                await self._set_status(row.id, "redeem failed")
            self.notify_games.append({"title": title, "url": row.url, "status": f"failed:no-redeem-page ({code})"})
            return

        if cfg.dryrun:
            logger.info("DRYRUN – skipped redeeming '%s' on Legacy Games.", title)
            self.notify_games.append({"title": title, "url": promo, "status": "available (dry run)"})
            return

        outcome = await self._send_code(code)
        # "Check your code" also answers a code that did go through on an earlier run.
        if outcome == "rejected" and await self._in_free_games(title):
            outcome = "redeemed"
        if outcome == "redeemed":
            if await self._in_free_games(title):
                logger.info("✓ Redeemed '%s' on Legacy Games, it is in your free games.", title)
            else:
                logger.warning("Redeemed '%s' on Legacy Games, but My Free Games does not list it yet.", title)
            await self._set_status(row.id, "claimed and redeemed", promo)
            self.notify_games.append({"title": title, "url": URL_FREE_GAMES, "status": "redeemed (Legacy Games)"})
        elif outcome == "rejected":
            logger.warning("Legacy Games turned down the code for '%s': already used or past its date.", title)
            await self._set_status(row.id, "redeem failed", promo)
            self.notify_games.append({"title": title, "url": promo, "status": f"failed:code-rejected ({code})"})
        else:
            logger.warning("Could not tell whether '%s' was redeemed on Legacy Games, trying again next run.", title)
            await self.take_screenshot(f"legacygames_{filenamify(title)}")
            self.notify_games.append({"title": title, "url": promo, "status": f"code: {code} (Legacy Games, unconfirmed)"})

    async def redeem_pending_codes(self) -> None:
        """Redeem every Legacy Games code Prime saved and nobody redeemed yet."""
        pending = await self._pending_codes()
        if not pending:
            logger.debug("No pending Legacy Games codes to redeem.")
            return
        # On by default, so the settings are asked about only once there is a code to use them for.
        if not (cfg.lg_email and cfg.lg_password):
            logger.info("%d Legacy Games code(s) from Prime Gaming are waiting: set LG_EMAIL and LG_PASSWORD to "
                        "have them redeemed, they expire within days.", len(pending))
            return
        logger.info("Found %d Legacy Games code(s) from Prime Gaming to redeem.", len(pending))
        try:
            await self.start_browser()
            if not await self._ensure_logged_in():
                logger.error("Aborting Legacy Games redemption due to login failure.")
                needs_you(self.store_name, len(pending))
                for row in pending:
                    self.notify_games.append({"title": row.title, "url": row.url,
                                              "status": f"code: {row.code} (Legacy Games, not redeemed)"})
                return
            for row in pending:
                await self._redeem(row)
        except Exception as exc:
            logger.exception("Fatal error during Legacy Games redemption")
            if cfg.notify_errors:
                await self.notify(f"Legacy Games codes failed: {exc}")
        finally:
            await self.close_browser()
