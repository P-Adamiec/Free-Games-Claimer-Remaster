"""Microsoft Store: redeems Prime Gaming codes and claims paid games that are free for a while."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
import nodriver as uc
from sqlalchemy import select

from src.core.claimer import BaseClaimer, OTP_KEY_ATTEMPTS, filenamify, mask_account
from src.core.config import cfg
from src.core.database import ClaimedGame, async_session, get_or_create
from src.core.run_state import needs_you
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.microsoft")

URL_REDEEM = "https://account.microsoft.com/billing/redeem"
PRODUCT_API = "https://storeedgefd.dsx.mp.microsoft.com/v9.0/products/{pid}"
# Xbox.com never gets its purchase window started, Microsoft's own page does (issue seen live).
PRODUCT_PAGE = "https://www.xbox.com/en-US/games/store/game/{pid}"

# The redeem form is a frame from another origin, so the page around it cannot read or fill it.
REDEEM_FRAME = "redeem-iframe"
CODE_FIELD = "tokenString"

FRAME_CLICK_JS = r"""
function() {
    const doc = this.ownerDocument || this;
    const wanted = ['next', 'confirm', 'redeem', 'get', 'place order', 'buy'];
    const buttons = [...doc.querySelectorAll('button, input[type=submit]')];
    const button = buttons.find(b => wanted.includes((b.textContent || b.value || '').trim().toLowerCase())
                                     && !b.disabled);
    if (button) {
        button.click();
        return JSON.stringify({clicked: true, label: (button.textContent || button.value || '').trim()});
    }
    return JSON.stringify({clicked: false,
                           buttons: buttons.map(b => (b.textContent || b.value || '').trim()).slice(0, 6)});
}
"""

FRAME_FILLED_JS = r"""
function() {
    const doc = this.ownerDocument || this;
    const field = doc.querySelector('input[name="tokenString"]');
    const next = [...doc.querySelectorAll('button')]
        .find(b => (b.textContent || '').trim().toLowerCase() === 'next');
    return JSON.stringify({typed: field ? (field.value || '').length : -1,
                           ready: !!(next && !next.disabled)});
}
"""

FRAME_STATE_JS = r"""
function() {
    const doc = this.ownerDocument || this;
    const text = (doc.body ? (doc.body.innerText || '') : '').replace(/\s+/g, ' ').toLowerCase();
    return JSON.stringify({
        text: text.slice(0, 300),
        // Seen live: "You're good to go" and "is ready for you", the apostrophe is not always the same one.
        done: /good to go|ready for you|redeemed|added to your account|all set|enjoy your/.test(text),
        used: /already (been )?(used|redeemed)|code has been redeemed/.test(text),
        bad: /isn't valid|is not valid|invalid|can't be used|couldn't redeem|expired|check the code|try again/.test(text),
    });
}
"""

COOKIE_JS = r"""
    JSON.stringify((() => {
        const wanted = ['accept', 'akceptuj', 'zgadzam'];
        let clicked = 0;
        [...document.querySelectorAll('button')].forEach(b => {
            const t = (b.textContent || '').trim().toLowerCase();
            if (wanted.some(w => t.startsWith(w))) { b.click(); clicked += 1; }
        });
        return {clicked: clicked};
    })())
"""

PAGE_ACTION_JS = r"""
    JSON.stringify((() => {
        const seen = el => !!el.getClientRects().length;
        const label = el => (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const buttons = [...document.querySelectorAll('button, a[role="button"]')].filter(seen);
        const text = (document.body ? (document.body.innerText || '') : '').toLowerCase();
        // Only the page's own Get button is ever pressed, never Buy, and never the Game Pass
        // offer sitting next to it, which is a subscription and a link rather than a button.
        const get = buttons.find(b => b.tagName === 'BUTTON' && label(b).startsWith('get')
                                      && !label(b).includes('game pass'));
        const owned = /you own this|in your library/.test(text)
            || (!get && buttons.some(b => ['install', 'play'].some(w => label(b).startsWith(w))));
        if (get) get.click();
        return {clicked: !!get, owned: owned, buttons: buttons.map(label).filter(Boolean).slice(0, 10)};
    })())
"""

PAGE_OWNED_JS = r"""
    JSON.stringify((() => {
        const seen = el => !!el.getClientRects().length;
        const label = el => (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const buttons = [...document.querySelectorAll('button')].filter(seen);
        const text = (document.body ? (document.body.innerText || '') : '').toLowerCase();
        const get = buttons.find(b => label(b).startsWith('get') && !label(b).includes('game pass'));
        return {
            owned: /you own this|in your library/.test(text)
                   || (!get && buttons.some(b => ['install', 'play'].some(w => label(b).startsWith(w)))),
            buttons: buttons.map(label).filter(Boolean).slice(0, 10),
        };
    })())
"""

WINDOW_STATE_JS = r"""
    JSON.stringify((() => {
        const seen = el => !!el.getClientRects().length;
        const text = (document.body ? (document.body.innerText || '') : '').replace(/\s+/g, ' ').trim();
        const low = text.toLowerCase();
        const amounts = text.match(/\d+[.,]\d{2}/g) || [];
        const zero = a => parseFloat(a.replace(',', '.')) === 0;
        return {
            text: text.slice(0, 200),
            free: amounts.length ? amounts.every(zero) : /\bfree\b|0[.,]00/.test(low),
            owned: /already own|you own|in your library/.test(low),
            error: /might be temporarily down|page not found|can.t be reached|no internet/.test(low),
            done: /thank you|all set|good to go|ready for you|order confirmed|installed|start playing/.test(low),
            buttons: [...document.querySelectorAll('button')].filter(seen)
                .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 6),
        };
    })())
"""

WINDOW_CLICK_JS = r"""
    JSON.stringify((() => {
        const wanted = ['get', 'confirm', 'place order', 'install'];
        const buttons = [...document.querySelectorAll('button')].filter(b => !!b.getClientRects().length);
        const button = buttons.find(b => wanted.includes((b.textContent || '').trim().toLowerCase()) && !b.disabled);
        if (button) {
            button.click();
            return {clicked: true, label: (button.textContent || '').trim()};
        }
        return {clicked: false, buttons: buttons.map(b => (b.textContent || '').trim()).slice(0, 6)};
    })())
"""

# The two names prime.py writes for a Microsoft code, nothing else counts as one.
CODE_STORES = ("microsoft store", "xbox")
LOGIN_HOSTS = ("login.live.com", "login.microsoftonline.com", "login.microsoft.com")


# ----- Reading the store, all pure -----

def is_microsoft_code(extra: str | None) -> bool:
    """True when Prime Gaming tagged this code as a Microsoft Store or Xbox one."""
    try:
        store = json.loads(extra or "{}").get("external_store", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False
    return str(store).strip().lower() in CODE_STORES


def product_id_from_url(url: str) -> str:
    """The store id out of an Xbox or Microsoft product address, empty when there is none."""
    path = str(url or "").split("?", 1)[0].split("#", 1)[0]
    for part in reversed(path.strip("/").split("/")):
        # Store ids are twelve characters, digits and capitals, e.g. 9NKX70BBCDRN.
        if len(part) == 12 and part.isalnum() and part.upper() == part:
            return part
    return ""


def read_product(payload: dict) -> dict:
    """Pull the price facts out of the store's answer about one product."""
    body = (payload or {}).get("Payload") or {}
    sku = ((body.get("SkusSummary") or [{}])[0]) or {}
    msrp = body.get("MSRP")
    return {
        "product_id": body.get("ProductId") or "",
        "title": body.get("Title") or "",
        "price": body.get("Price"),
        "msrp": msrp if msrp is not None else sku.get("MSRP"),
        "display_price": body.get("DisplayPrice") or "",
    }


def _as_price(value) -> float:
    """A price as a number, with anything unreadable counting as no price at all."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def free_to_keep(product: dict) -> bool:
    """True only for a game that normally costs money and costs nothing right now."""
    # "Included" is Game Pass and a zero list price is free to play, neither is a giveaway.
    if str((product or {}).get("display_price") or "").strip().lower() == "included":
        return False
    return _as_price((product or {}).get("msrp")) > 0 and _as_price((product or {}).get("price")) == 0


async def fetch_product(product_id: str, market: str = "US") -> dict:
    """Ask the store what one product costs. A failure returns nothing, never raises."""
    if not product_id:
        return {}
    url = PRODUCT_API.format(pid=product_id)
    params = {"market": market, "locale": "en-us", "deviceFamily": "Windows.Xbox"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            product = read_product(resp.json())
    except Exception as exc:
        logger.debug("Could not read product %s: %s", product_id, exc)
        return {}
    logger.debug("Product %s: price=%s msrp=%s display='%s'", product_id, product.get("price"),
                 product.get("msrp"), product.get("display_price"))
    return product


class MicrosoftClaimer(BaseClaimer):
    store_name = "microsoft"
    # Prices differ per country, and the account's own country is read from the store window below.
    market = "US"

    async def start_browser(self, *, force_headful: bool = True, extra_args: list[str] | None = None) -> uc.Browser:
        """Microsoft requires headful mode to avoid ERR_HTTP2_PROTOCOL_ERROR on account pages."""
        return await super().start_browser(force_headful=force_headful, extra_args=extra_args)

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def run(self, extra_games: list | None = None) -> None:
        """Claim the limited-time free games GamerPower found on the Microsoft Store."""
        if not extra_games:
            logger.info("No Microsoft giveaway to claim in this run.")
            return
        try:
            await self.start_browser()
            self.log_browser_ready()
            if not await self._ensure_logged_in():
                logger.error("Aborting Microsoft claim flow due to login failure.")
                return
            for game in extra_games:
                await self._claim_game(game)
            logger.info("Microsoft claimer finished.")
        except Exception as exc:
            logger.exception("Fatal error during Microsoft flow")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    async def redeem_pending_codes(self) -> None:
        """Redeem the Microsoft and Xbox codes Prime Gaming handed out but nobody used yet."""
        pending = await self._pending_codes()
        if not pending:
            logger.debug("No pending Microsoft codes in the database.")
            return

        logger.info("Found %d Microsoft code(s) waiting to be redeemed. Starting browser...", len(pending))
        try:
            await self.start_browser()
            self.log_browser_ready()
            if not await self._ensure_logged_in():
                logger.error("Aborting Microsoft code redemption due to login failure.")
                return
            for row in pending:
                await self._redeem_code(row.code, row.title, row.url)
        except Exception as exc:
            logger.exception("Fatal error during Microsoft code redemption")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    async def _pending_codes(self) -> list:
        """The database rows holding a Microsoft code that has not been redeemed yet."""
        async with async_session() as session:
            query = select(ClaimedGame).where(
                ClaimedGame.code.isnot(None),
                ClaimedGame.code != "",
                ClaimedGame.status != "redeemed",
                ClaimedGame.status != "already redeemed",
            )
            if cfg.ms_force_redeem:
                # Force mode walks the last 60 days again, except what is already on the account.
                query = query.where(ClaimedGame.created_at >= datetime.now(timezone.utc) - timedelta(days=60))
            else:
                query = query.where(ClaimedGame.status == "claimed")
            rows = (await session.execute(query)).scalars().all()

        pending = [row for row in rows if is_microsoft_code(row.extra)]
        logger.debug("Pending code check: %d row(s) with a code, %d of them Microsoft.", len(rows), len(pending))
        return pending

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    @staticmethod
    def _short_url(url: str) -> str:
        """Address without its query, which is where Microsoft carries the account name."""
        return str(url or "").split("?", 1)[0]

    async def _current_url(self) -> str:
        url = await self.page.evaluate("window.location.href")
        return url if isinstance(url, str) else ""

    async def _is_logged_in(self) -> bool:
        """Signed in when the account pages keep us instead of handing us to a sign-in host."""
        # Decided by where Microsoft sends us, never by what the page says (issue #38 on GOG).
        url = await self._current_url()
        if any(url_has_allowed_host(url, host) for host in LOGIN_HOSTS):
            return False
        return url_has_allowed_host(url, "account.microsoft.com", allow_subdomains=True)

    async def _ensure_logged_in(self) -> bool:
        """Open the redeem page and make sure the session behind it is a signed-in one."""
        await self._open(URL_REDEEM)
        if await self._settled_login_state():
            await self._signed_in()
            return True

        logger.warning("Not signed in to Microsoft, attempting login...")
        if cfg.ms_email and cfg.ms_password:
            await self._do_login(cfg.ms_email, cfg.ms_password)
            await self.sleep(5)
            if await self._is_logged_in():
                await self._signed_in()
                return True
        else:
            logger.warning("MS_EMAIL / MS_PASSWORD not set.")

        # An e-mail code goes to your inbox and a passkey needs your device, so this ends with you.
        logged_in = await self._wait_for_vnc_login(
            self._is_logged_in,
            custom_msg=self._vnc_notice(
                "Microsoft: sign-in needed",
                "Microsoft wants you to finish signing in. Open the browser, and if it asks for a "
                "passkey choose another way and use the e-mail code.",
            ),
            store_key=self.store_name,
        )
        if logged_in:
            await self._signed_in()
        else:
            logger.warning("VNC login timed out, skipping Microsoft.")
        return logged_in

    async def _settled_login_state(self, wait: int = 25) -> bool:
        """Judge the session only after Microsoft stops bouncing between its sign-in hosts."""
        # Deciding too early made the bot retype the password on every single run.
        for _ in range(max(1, wait // 5)):
            if await self._is_logged_in():
                return True
            if await self._first_field(self.EMAIL_FIELDS + self.PASSWORD_FIELDS, timeout=2):
                return False
            await self.sleep(5)
        return await self._is_logged_in()

    async def _signed_in(self) -> None:
        """Note who we are and which country this account buys in, once the session is sure."""
        self.user = mask_account(cfg.ms_email) or "Microsoft User"
        self.log_signed_in(self.user)
        await self._detect_market()

    # Microsoft serves two sign-in forms, the classic one and the newer live.com layout.
    EMAIL_FIELDS = ("input[name='loginfmt']", "#usernameEntry", "input[type='email']")
    PASSWORD_FIELDS = ("input[name='passwd']", "#passwordEntry", "input[type='password']")
    CODE_FIELDS = ("input[name='otc']", "#codeEntry", "input[inputmode='numeric']")

    async def _detect_market(self) -> None:
        """Read the country this account buys in, so prices are judged where the purchase happens."""
        # The window takes a few seconds to arrive, and it is the only place that names the country.
        for _ in range(5):
            frame = await self._redeem_frame()
            found = re.search(r"market=([A-Za-z]{2})", str(getattr(frame, "document_url", "") or "")) if frame else None
            if found:
                self.market = found.group(1).upper()
                break
            await self.sleep(3)
        logger.debug("Store market for this account: %s", self.market)

    async def _first_field(self, selectors: tuple, timeout: int = 10):
        """The first of these fields the page actually has, noting which one it was."""
        for selector in selectors:
            field = await self.page.select(selector, timeout=timeout)
            if field:
                logger.debug("Sign-in form: using %s", selector)
                return field
        logger.debug("Sign-in form: none of %s is on this page.", ", ".join(selectors))
        return None

    async def _do_login(self, email: str, password: str) -> None:
        """Type the account and password into Microsoft's sign-in form."""
        logger.debug("Sign-in starts at %s", self._short_url(await self._current_url()))
        email_input = await self._first_field(self.EMAIL_FIELDS)
        if email_input:
            await email_input.send_keys(email)
            await self.sleep(1)
            await email_input.send_keys("\r")
            await self.sleep(5)
            logger.debug("After the account step: %s", self._short_url(await self._current_url()))

        password_input = await self._first_field(self.PASSWORD_FIELDS)
        if not password_input:
            # An account with a passkey is offered that first, and the password is one click away.
            await self._switch_to_password()
            password_input = await self._first_field(self.PASSWORD_FIELDS, timeout=6)
        if password_input:
            await password_input.send_keys(password)
            await self.sleep(1)
            await password_input.send_keys("\r")
            await self.sleep(6)
            logger.debug("After the password step: %s", self._short_url(await self._current_url()))
            await self._log_page_state("after the password step")

        if not await self._two_factor_present() and not await self._is_logged_in():
            await self._switch_to_code()
        if await self._two_factor_present() and not await self._fill_totp():
            logger.debug("Microsoft asked for a code the bot could not produce, leaving it to you.")
        await self._stay_signed_in()

    async def _switch_to_password(self) -> None:
        """Leave Microsoft's passkey offer and ask for the password screen instead."""
        await self._switch_method(("Use your password", "Password"), self.PASSWORD_FIELDS)

    async def _switch_to_code(self) -> None:
        """Leave the approve-in-the-app offer and ask for a code the bot can type."""
        await self._switch_method(("Use a verification code", "Use a code", "verification code"),
                                  self.CODE_FIELDS)

    async def _switch_method(self, wanted: tuple, fields: tuple) -> None:
        """Click through Microsoft's other ways to sign in until the field we need shows up."""
        await self._log_page_state("before switching the sign-in method")
        for label in ("Other ways to sign in", "Sign-in options", "Sign in another way") + wanted:
            option = await self.page.find(label, timeout=4)
            if option:
                logger.debug("Sign-in form: clicking '%s'", label)
                await option.click()
                await self.sleep(4)
                if await self._first_field(fields, timeout=4):
                    return
        await self._log_page_state("after trying to switch the sign-in method")

    async def _log_page_state(self, moment: str) -> None:
        """Write what the sign-in page is showing, so a stuck login can be read from the log."""
        raw = await self.page.evaluate(
            r"""
            JSON.stringify((() => {
                const seen = el => !!el.getClientRects().length;
                const txt = el => (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40);
                return {
                    headings: [...document.querySelectorAll('h1,h2,h3')].filter(seen).map(txt).slice(0, 4),
                    buttons: [...document.querySelectorAll('button, a, input[type=submit]')]
                        .filter(seen).map(el => txt(el) || el.id).filter(Boolean).slice(0, 10),
                };
            })())
            """
        )
        logger.debug("Sign-in page %s: %s", moment, raw)

    async def _two_factor_present(self) -> bool:
        """True when Microsoft is asking for a one-time code."""
        return bool(await self._first_field(self.CODE_FIELDS, timeout=3))

    async def _fill_totp(self) -> bool:
        """Type a code from the authenticator secret, twice at most, then it is over to you."""
        if not cfg.ms_otp_key:
            return False
        for _ in range(OTP_KEY_ATTEMPTS):
            code_input = await self._first_field(self.CODE_FIELDS, timeout=6)
            if not code_input:
                return False
            self._last_totp = await self._fresh_totp(cfg.ms_otp_key, self._last_totp)
            await code_input.send_keys(self._last_totp)
            await self._remember_this_browser()
            await code_input.send_keys("\r")
            await self.sleep(8)
            if not await self._two_factor_present():
                return True
        return False

    async def _stay_signed_in(self) -> None:
        """Answer "Stay signed in?" with yes, so the profile keeps the session."""
        button = await self.page.select("#idSIButton9", timeout=5)
        if not button:
            button = await self.page.find("Yes", timeout=4)
        if button:
            logger.debug("Sign-in form: staying signed in.")
            await button.click()
            await self.sleep(5)

    # ------------------------------------------------------------------
    # Codes
    # ------------------------------------------------------------------

    async def _redeem_code(self, code: str, title: str, url: str) -> None:
        """Type one code into the redeem form and record what Microsoft answered."""
        logger.info("Redeeming Microsoft code for '%s'.", title)
        try:
            await self._open_redeem_page()
            field = await self._code_field_node()
            if field is None:
                logger.error("Could not find the code field on the redeem page.")
                await self.take_screenshot(f"microsoft_redeem_{filenamify(title)}")
                self.notify_games.append({"title": title, "url": URL_REDEEM, "status": "failed:no-code-field"})
                return

            filled = await self._fill_code(field, code)
            if filled != "ready":
                # The form checks the code while you type and says so, e.g. a code already used.
                refusal = await self._redeem_outcome() if filled == "not accepted" else "unclear"
                if refusal == "already redeemed":
                    await self._record_outcome(code, title, url, refusal)
                    return
                status = "failed:code-refused" if filled == "not accepted" else "failed:not-typed"
                logger.warning("Microsoft's redeem form did not accept the code for '%s'.", title)
                await self.take_screenshot(f"microsoft_redeem_{filenamify(title)}")
                self.notify_games.append({"title": title, "url": URL_REDEEM, "status": status})
                return

            if cfg.dryrun:
                logger.info("[DRYRUN] Would redeem '%s' now.", title)
                self.notify_games.append({"title": title, "url": URL_REDEEM, "status": "available (dry run)"})
                return

            if not await self._press_frame_button():
                logger.error("The redeem form would not submit the code for '%s'.", title)
                await self.take_screenshot(f"microsoft_redeem_{filenamify(title)}")
                self.notify_games.append({"title": title, "url": URL_REDEEM, "status": "failed:not-submitted"})
                return
            await self.sleep(8)
            # A real code then shows what it is for and asks you to confirm, a refused one does not.
            if await self._press_frame_button():
                await self.sleep(8)

            outcome = await self._redeem_outcome()
            logger.debug("Redeem outcome for '%s': %s", title, outcome)
            await self._record_outcome(code, title, url, outcome)
        except Exception:
            logger.exception("Failed to redeem Microsoft code for '%s'", title)
            self.notify_games.append({"title": title, "url": URL_REDEEM, "status": "failed"})

    async def _open_redeem_page(self) -> None:
        """Open the redeem page in English and get the cookie banner out of the way."""
        # Without the language the form speaks your country's language and the buttons change name.
        await self._open(f"{URL_REDEEM}?lang=en-US")
        await self.sleep(6)
        await self._dismiss_cookies()
        await self.sleep(6)

    async def _redeem_frame(self):
        """The document of Microsoft's redeem frame, which the page around it cannot read."""
        return await self._frame_document(
            lambda attrs: attrs.get("id") == REDEEM_FRAME or "redeem" in (attrs.get("title") or "").lower()
        )

    async def _code_field_node(self):
        """The code box inside that frame, found by piercing into it."""
        doc = await self._pierced_document()
        if doc is None:
            return None
        for node in self._walk_nodes(doc):
            if node.node_name == "INPUT" and self._node_attrs(node).get("name") == CODE_FIELD:
                return node
        logger.debug("No %s field on the page, the frame may not have loaded.", CODE_FIELD)
        return None

    async def _fill_code(self, field, code: str) -> str:
        """Type the code in and say what the form made of it: ready, not accepted or empty."""
        # The box counts 25 characters and writes the dashes itself, so anything else is refused.
        code = "".join(ch for ch in str(code) if ch.isalnum())
        await self._type_into_node(field.node_id, code)
        await self.sleep(2)
        state = await self._frame_eval(await self._redeem_frame(), FRAME_FILLED_JS) or {}
        logger.debug("Redeem box after typing: %s", state)
        if state.get("ready"):
            return "ready"
        if state.get("typed"):
            return "not accepted"

        # Some of Microsoft's boxes ignore inserted text and only react to real key presses.
        logger.debug("The box did not take the text, typing it key by key instead.")
        try:
            await self.page.send(uc.cdp.dom.focus(node_id=field.node_id))
            for char in code:
                await self.page.send(uc.cdp.input_.dispatch_key_event(type_="char", text=char))
        except Exception as exc:
            logger.debug("Typing key by key did not work either: %s", exc)
            return "empty"
        await self.sleep(2)
        state = await self._frame_eval(await self._redeem_frame(), FRAME_FILLED_JS) or {}
        logger.debug("Redeem box after typing key by key: %s", state)
        if state.get("ready"):
            return "ready"
        return "not accepted" if state.get("typed") else "empty"

    async def _press_frame_button(self, frame=None) -> bool:
        """Press the window's own button, whichever step it is showing."""
        document = await (frame or self._redeem_frame)()
        result = (await self._frame_eval(document, FRAME_CLICK_JS) if document else None) or {}
        logger.debug("Redeem form: %s", result)
        return bool(result.get("clicked"))

    async def _redeem_outcome(self) -> str:
        """What the form says happened: redeemed, already used, refused or unclear."""
        document = await self._redeem_frame()
        state = (await self._frame_eval(document, FRAME_STATE_JS) if document else None) or {}
        logger.debug("Redeem form says: %r", (state.get("text") or "")[:160])
        if state.get("used"):
            return "already redeemed"
        if state.get("bad"):
            return "refused"
        if state.get("done"):
            return "redeemed"
        return "unclear"


    async def _record_outcome(self, code: str, title: str, url: str, outcome: str) -> None:
        """Write the result to the database and the run summary."""
        if outcome in ("redeemed", "already redeemed"):
            async with async_session() as session:
                rows = (await session.execute(select(ClaimedGame).where(ClaimedGame.code == code))).scalars().all()
                for row in rows:
                    row.status = outcome
                await session.commit()
            logger.info("'%s' %s on Microsoft.", title, outcome)
            self.notify_games.append({"title": title, "url": url or URL_REDEEM, "status": "claimed" if outcome == "redeemed" else "existed"})
            return

        await self.take_screenshot(f"microsoft_redeem_{filenamify(title)}")
        status = "failed:code-refused" if outcome == "refused" else "failed:unclear"
        logger.warning("Microsoft code for '%s': %s.", title, status)
        self.notify_games.append({"title": title, "url": url or URL_REDEEM, "status": status})

    # ------------------------------------------------------------------
    # Giveaways
    # ------------------------------------------------------------------

    async def _claim_game(self, game: dict) -> None:
        """Claim one GamerPower find, but only when the store says it is a giveaway."""
        title = game.get("title") or "Unknown"
        url = game.get("url") or game.get("giveaway_url") or ""
        product_id = product_id_from_url(url)
        product = await fetch_product(product_id, self.market)

        if not product:
            logger.warning("Could not read '%s' in the store, skipping it.", title)
            self.notify_games.append({"title": title, "url": url, "status": "skipped:no-store-data"})
            return
        if not free_to_keep(product):
            logger.info("'%s' is not free to keep (%s), skipping.", title, product.get("display_price") or "no price")
            self.notify_games.append({"title": title, "url": url, "status": "skipped:not-a-giveaway"})
            return
        if await self._already_recorded(product_id):
            logger.info("'%s' already claimed in an earlier run.", title)
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            return

        if cfg.dryrun:
            logger.info("[DRYRUN] '%s' is free to keep, would claim it now.", title)
            self.notify_games.append({"title": title, "url": url, "status": "available (dry run)"})
            return

        await self._open(PRODUCT_PAGE.format(pid=product_id))
        await self.sleep(10)
        await self._dismiss_cookies()
        outcome = await self._press_get(PRODUCT_PAGE.format(pid=product_id))
        await self._record_claim(product_id, product.get("title") or title, title, url, outcome)

    async def _record_claim(self, product_id: str, store_title: str, title: str, url: str, outcome: str) -> None:
        """Write down how the claim ended, and never let a live giveaway pass unnoticed."""
        if outcome == "claimed":
            logger.info("'%s': claimed.", title)
            await self._remember(product_id, store_title, url, "claimed")
            self.notify_games.append({"title": title, "url": url, "status": "claimed"})
            return
        if outcome == "owned":
            logger.info("'%s' is already on the account.", title)
            await self._remember(product_id, store_title, url, "existed")
            self.notify_games.append({"title": title, "url": url, "status": "existed"})
            return

        # A giveaway runs for days, so this wording carries none of the words the summary filter drops.
        await self.take_screenshot(f"microsoft_{filenamify(title)}")
        needs_you(self.store_name)
        logger.warning("'%s' is free to keep, but the bot could not finish it. Open it yourself.", title)
        self.notify_games.append({"title": title, "url": url, "status": "notified"})

    async def _press_get(self, url: str) -> str:
        """Take the game: Get on the page, then confirm in the window Microsoft opens for it."""
        page = await self._page_action()
        logger.debug("Product page: %s", page)
        if page.get("owned"):
            return "owned"
        if not page.get("clicked"):
            return "failed"

        window, till = await self._open_purchase_window()
        if window is None:
            # Microsoft serves a broken window now and then, and only a fresh page brings a new one.
            logger.debug("Reloading the product page for one more go at the purchase window.")
            await self._open(url)
            await self.sleep(8)
            await self._dismiss_cookies()
            if not (await self._page_action()).get("clicked"):
                return "failed"
            window, till = await self._open_purchase_window()
        if window is None:
            logger.warning("Microsoft did not open a working purchase window, so nothing was taken.")
            return "failed"

        if till.get("owned"):
            return "owned"
        if not till.get("free"):
            # The catalogue said free and the till disagrees, so this stops rather than buying.
            logger.warning("The purchase window is not free: %r", (till.get("text") or "")[:120])
            return "failed"

        pressed = await self._window_eval(window, WINDOW_CLICK_JS)
        logger.debug("Purchase window button: %s", pressed)
        if not pressed.get("clicked"):
            return "failed"
        await self.sleep(12)

        done = await self._window_state(await self._purchase_window(wait=8))
        logger.debug("Purchase window after confirming: %s", done)
        if done.get("done") or done.get("owned"):
            return "claimed"
        # The window closes itself once the order goes through, so the page has the last word.
        return "claimed" if await self._owned_now(url) else "failed"

    async def _owned_now(self, url: str) -> bool:
        """Reload the product page and read whether the game sits on the account now."""
        await self._open(url)
        await self.sleep(8)
        state = await self._json_eval(self.page, PAGE_OWNED_JS)
        logger.debug("Product page after the order: %s", state)
        return bool(state.get("owned"))

    async def _open(self, url: str, timeout: int = 60) -> None:
        """Open a page, and carry on when it never stops loading instead of hanging the run."""
        try:
            await asyncio.wait_for(self.page.get(url), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug("%s kept loading for %ss, carrying on with what is there.", url, timeout)

    async def _open_purchase_window(self):
        """Wait for the purchase window and read it. Pressing Get again only re-reads the same one."""
        window = await self._purchase_window(wait=28)
        till = await self._window_state(window) if window is not None else {}
        logger.debug("Purchase window: %s", till)
        if window is None or not till:
            return None, {}
        if till.get("error"):
            # Seen repeatedly: Microsoft answers the window with its own error page, and no
            # amount of clicking changes that, so the giveaway goes to you with a link instead.
            logger.warning("Microsoft's purchase window did not load: %r", (till.get("text") or "")[:100])
            return None, {}
        return window, till

    async def _page_action(self) -> dict:
        """Press the page's own Get button, and say when the game is on the account already."""
        return await self._json_eval(self.page, PAGE_ACTION_JS)

    async def _purchase_window(self, wait: int = 0):
        """The window Microsoft opens to finish a purchase, which runs in its own process."""
        return await self._target_by_url("buynowui", wait=wait)

    async def _window_state(self, window) -> dict:
        """What the purchase window is showing: the total, whether it is free, whether it is done."""
        return await self._window_eval(window, WINDOW_STATE_JS)

    async def _window_eval(self, window, script: str) -> dict:
        """Run JS inside the purchase window and parse what it hands back."""
        return await self._json_eval(window, script)

    @staticmethod
    async def _json_eval(target, script: str, timeout: int = 45) -> dict:
        """Evaluate JS that returns JSON, anywhere, and never raise or hang over a bad answer."""
        try:
            raw = await asyncio.wait_for(target.evaluate(script), timeout=timeout)
            return json.loads(raw) if isinstance(raw, str) else {}
        except asyncio.TimeoutError:
            logger.debug("A page took longer than %ss to answer, moving on.", timeout)
            return {}
        except Exception as exc:
            logger.debug("Could not read a page or window: %s", exc)
            return {}

    async def _dismiss_cookies(self) -> None:
        """Click away the cookie banner, which otherwise sits over the buttons."""
        await self._json_eval(self.page, COOKIE_JS, timeout=30)
        await self.sleep(3)


    # Database
    # ------------------------------------------------------------------

    async def _already_recorded(self, product_id: str) -> bool:
        """True when an earlier run already claimed this product for this account."""
        if not product_id:
            return False
        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame).where(
                    ClaimedGame.store == self.store_name,
                    ClaimedGame.user == (self.user or "unknown"),
                    ClaimedGame.game_id == product_id,
                    ClaimedGame.status == "claimed",
                )
            )
            return result.scalar_one_or_none() is not None

    async def _remember(self, product_id: str, title: str, url: str, status: str) -> None:
        """Record the claim so the next run can tell it apart from a new offer."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session,
                store=self.store_name,
                user=self.user or "unknown",
                game_id=product_id or title,
                title=title,
                url=url,
                status=status,
            )
            if not created and obj.status != status:
                obj.status = status
            await session.commit()
            logger.debug("DB %s '%s' (status=%s).", "stored" if created else "already had", title, obj.status)


async def claim_microsoft(extra_games: list | None = None) -> dict:
    """Convenience entry point."""
    claimer = MicrosoftClaimer()
    await claimer.run(extra_games)
    return {"store": "Microsoft", "user": claimer.user, "games": claimer.notify_games}
