"""Steam store module – claims free-to-keep games from the Steam Store."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re

from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.claimer import BaseClaimer, account_key, filenamify, track_missing_base
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.run_state import needs_you
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.steam")

# Steam's own search for items that cost money and are free right now, read after GamerPower (SteamDB asked bots to stay away, #84).
STORE_FREE_SEARCH = "https://store.steampowered.com/search/?maxprice=free&specials=1&ignore_preferences=1"
# Steam's "Games" filter, so DLC and soundtracks stay out with STEAM_CLAIM_DLC=false.
GAMES_ONLY = "&category1=998"
# A store page as (kind, number): app 10 and package 10 are different things, the age gate is the same page.
STEAM_ITEM = re.compile(r"https://store\.steampowered\.com/(?:agecheck/)?(app|sub|bundle)/(\d+)")
# The search page's rows and how many it rendered; signed in, Steam shows no "N results" line to read.
SEARCH_PAGE_JS = """JSON.stringify({
    rows: document.querySelector('#search_resultsRows')?.innerHTML || '',
    shown: document.querySelectorAll('#search_resultsRows .search_result_row').length,
})"""
# The page renders this many rows before it waits for a scroll.
SEARCH_PAGE_SIZE = 50

# Steam Store URLs used for login and navigation
URL_STORE = "https://store.steampowered.com/?l=english"
URL_LOGIN = "https://store.steampowered.com/login/"

# A game's demo has its own purchase block and is always free, so it must never pass as the giveaway (#62).
DEMO_JS = r"""
    const isDemoBlock = el => !!el && (el.matches('.demo_above_purchase') || !!el.querySelector('.demo_above_purchase, #demoGameBtn'));
    const isDemoBtn = el => !!el && (!!el.closest('.demo_above_purchase, #demoGameBtn') || /\bdemo\b|playtest/i.test(el.textContent || ''));
"""


def free_search_games(page_html: str) -> list[dict]:
    """The search rows the store itself marks as 100% off at a price of zero: paid items free for now, never free-to-play."""
    games = []
    for chunk in str(page_html or "").split('<a href="')[1:]:
        chunk = chunk.split("</a>", 1)[0]
        if "search_result_row" not in chunk:
            continue
        url = html_lib.unescape(chunk.split('"', 1)[0])
        found = STEAM_ITEM.match(url)
        # The filter in the address is not trusted on its own: a dropped "specials" would list every free game.
        if not found or 'data-discount="100"' not in chunk or 'data-price-final="0"' not in chunk:
            continue
        title = re.search(r'<span class="title">(.*?)</span>', chunk, re.S)
        games.append({
            "title": html_lib.unescape(title.group(1)).strip() if title else f"Steam {found.group(1)} {found.group(2)}",
            "url": url,
            "app_id": found.group(2),
            "source": "steam",
        })
    return games


def claim_order(gamerpower: list[dict], store: list[dict]) -> list[dict]:
    """GamerPower's Steam finds first, then what Steam's own search adds, each store page once."""
    todo = [{**g, "url": g["final_url"], "source": "gamerpower"} for g in gamerpower]
    seen = {found.groups() for found in (STEAM_ITEM.match(g["url"] or "") for g in todo) if found}
    return todo + [g for g in store if STEAM_ITEM.match(g["url"]).groups() not in seen]


def base_game_button_js(dry: bool) -> str:
    """Find the free base game's add button and click it, or only report it in a dry run."""
    return r"""
JSON.stringify(((dry) => {""" + DEMO_JS + r"""
    const take = (btn, method) => { if (!dry) btn.click(); return { success: true, method }; };
    const allBtns = [...document.querySelectorAll('.btn_medium')].filter(b => !isDemoBtn(b));
    const addLibBtn = allBtns.find(b => b.textContent.includes('Add to Library') || b.textContent.includes('Dodaj do biblioteki'));
    if (addLibBtn) return take(addLibBtn, 'add_to_library');
    const addBtn = document.querySelector('[data-action="add_to_account"]');
    if (addBtn && !isDemoBtn(addBtn)) return take(addBtn, 'add_to_account');
    const greenBtns = [...document.querySelectorAll('.btn_green_steamui.btn_medium')].filter(b => !isDemoBtn(b));
    const playBtn = greenBtns.find(b => b.textContent.includes('Play Game') || b.textContent.includes('Zagraj'));
    if (playBtn) return take(playBtn, 'play_game');
    return { success: false };
})(""" + ("true" if dry else "false") + "))"

# "Remember me" is a div with role="checkbox", ticked by default, so it is clicked only when off (#65).
REMEMBER_ME_JS = r"""
(() => {
    const form = document.querySelector('div[data-featuretarget="login"]') || document;
    const box = form.querySelector('[role="checkbox"]');
    if (!box) return "not_found";
    if (box.getAttribute('aria-checked') === 'true') return "already";
    box.click();
    return "clicked";
})()
"""
# React applies the click a moment later, so the state is read again afterwards.
REMEMBER_ME_STATE_JS = (
    "(document.querySelector('div[data-featuretarget=\"login\"] [role=\"checkbox\"]')"
    "?.getAttribute('aria-checked')) || 'missing'"
)

URL_REGISTER_KEY = "https://store.steampowered.com/account/registerkey?l=english"

# Steam's answers on its key page; "not valid" was read live on 4.10, the rest is Steam's standard wording.
ACTIVATION_RESULTS = (
    ("too many recent activation attempts", "rate-limited"),
    ("already owns", "owned"),
    ("already been activated", "used"),
    ("not valid", "invalid"),
    ("requires ownership of another product", "needs-base"),
    ("region", "region"),
)

# Each outcome as (database status, summary status); a key Steam did not take stays in the giver's library.
KEY_OUTCOMES = {
    "activated": ("claimed and activated", "activated on Steam ✅"),
    "owned": ("existed", "existed"),
    "used": ("failed:key-invalid", "failed:key-invalid"),
    "invalid": ("failed:key-invalid", "failed:key-invalid"),
    "needs-base": ("failed:missing_base", "failed:missing_base"),
    "region": ("failed:key-region", "failed:key-region"),
}

ACTIVATION_STATE_JS = """JSON.stringify((() => {
    const receipt = document.querySelector('#receipt_form');
    return {
        error: (document.querySelector('#error_display')?.innerText || '').trim(),
        receipt: !!receipt && getComputedStyle(receipt).display !== 'none' && (receipt.innerText || '').trim().length > 0,
    };
})())"""


def activation_outcome(error_text: str, receipt_shown: bool) -> str:
    """What Steam did with a key: activated, owned, used, invalid, needs-base, region, rate-limited or unknown."""
    if receipt_shown:
        return "activated"
    text = (error_text or "").lower()
    return next((outcome for marker, outcome in ACTIVATION_RESULTS if marker in text), "unknown")


def is_waiting_steam_key(status: str | None, code: str | None, extra: str | None) -> bool:
    """A key another store left for Steam: still "claimed", with a code, tagged for Steam."""
    if status != "claimed" or not code:
        return False
    try:
        return json.loads(extra or "{}").get("external_store") == "steam"
    except (json.JSONDecodeError, AttributeError):
        return False


class SteamClaimer(BaseClaimer):
    store_name = "steam"

    # Set when a sign-in nobody finished stops the run.
    _signed_out: bool = False

    async def run(self, extra_games: list | None = None) -> None:
        """Main entry point: find free Steam games and claim them.
        
        Flow:
        1. Start browser
        2. Read Steam's own search for paid games that are free right now (STEAM_SEARCH)
        3. Claim GamerPower's Steam games first, then what the search adds
        """
        logger.debug("Starting Steam claiming flow")
        if not cfg.steam_search and not extra_games:
            logger.info("Nothing from GamerPower for Steam, and Steam's own search is off (STEAM_SEARCH=false).")
            return

        try:
            # Step 1: Open a browser with GPU acceleration enabled.
            await self.start_browser(
                force_headful=True,
                extra_args=[
                    "--ignore-gpu-blocklist",
                    "--enable-unsafe-webgpu",
                ],
            )

            # Step 2: Steam's own search for paid games that are free right now
            store_games = await self._fetch_free_from_store() if cfg.steam_search else []
            if store_games:
                links = [f"  • [bold cyan]{g['title']}[/bold cyan] 🔗 {g.get('url', '')}" for g in store_games]
                logger.info("🎮 [bold magenta]Steam store: %d free game(s):[/bold magenta]\n%s",
                            len(store_games), "\n".join(links))
            elif cfg.steam_search:
                logger.info("No free games found in the Steam store.")

            # Step 3: GamerPower's Steam games first, the search adds what GamerPower missed.
            todo = claim_order(extra_games or [], store_games)
            logger.debug("Steam: %d from GamerPower, %d more from the store search", len(extra_games or []),
                         len(todo) - len(extra_games or []))
            for index, game in enumerate(todo):
                if game.get("source") == "gamerpower":
                    logger.info("🎮 [GamerPower] '%s' → Steam", game.get("title", "Unknown"))
                await self._claim_game(game)
                # A sign-in nobody finished ends the run instead of failing every game after it (TODO 2).
                if self._signed_out:
                    left = len(todo) - index
                    logger.error("Aborting Steam flow due to login failure, %d game(s) left for the next run.", left)
                    needs_you(self.store_name, left)
                    if cfg.notify_errors:
                        await self.notify("steam: could not sign in, the rest is left for the next run.")
                    break

        except Exception as exc:
            logger.exception("Fatal error")
            if cfg.notify_errors:
                await self.notify(f"steam failed: {exc}")
        finally:
            # Notifications are sent from main.py.
            await self.close_browser()

    # ------------------------------------------------------------------
    # Keys other stores handed out (Fanatical giveaways)
    # ------------------------------------------------------------------

    async def redeem_pending_keys(self) -> None:
        """Activate the Steam keys another store left in the database."""
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            rows = (await session.execute(
                select(ClaimedGame).where(ClaimedGame.status == "claimed", ClaimedGame.code.isnot(None))
            )).scalars().all()
        waiting = [(r.id, r.title, r.code) for r in rows if is_waiting_steam_key(r.status, r.code, r.extra)]
        if not waiting:
            logger.debug("No Steam keys waiting to be activated.")
            return

        logger.info("Found %d Steam key(s) waiting to be activated. Starting browser...", len(waiting))
        try:
            await self.start_browser(force_headful=True, extra_args=["--ignore-gpu-blocklist", "--enable-unsafe-webgpu"])
            await self.page.get(URL_STORE)
            await self.sleep(3)
            if not await self._ensure_logged_in(URL_STORE):
                logger.error("Not signed in to Steam, the %d key(s) stay queued for the next run.", len(waiting))
                needs_you(self.store_name, len(waiting))
                return
            for row_id, title, key in waiting:
                if not await self._activate_key(row_id, title, key):
                    break
        except Exception:
            logger.exception("Fatal error during Steam key activation")
        finally:
            await self.close_browser()

    async def _activate_key(self, row_id: int, title: str, key: str) -> bool:
        """Enter one key on Steam's activation page. False when the rest should wait for the next run."""
        from src.core.database import ClaimedGame

        label = f"{title} (Fanatical key)"
        if cfg.dryrun:
            logger.info("DRYRUN – would activate the Steam key for '%s'.", title)
            self.notify_games.append({"title": label, "url": URL_REGISTER_KEY, "status": "available (dry run)"})
            return True

        await self.page.get(URL_REGISTER_KEY)
        await self.sleep(4)
        field = await self.page.select("#product_key", timeout=10)
        if not field:
            logger.warning("Steam's key page showed no key field, '%s' stays queued for the next run.", title)
            return False
        await field.click()
        await self.sleep(0.4)
        await field.send_keys(key)
        await self.sleep(0.5)
        await self.page.evaluate("(() => { const b = document.querySelector('#accept_ssa'); if (b && !b.checked) b.click(); })()")
        await self.sleep(0.5)
        button = await self.page.select("#register_btn", timeout=5)
        if not button:
            logger.warning("Steam's key page showed no Continue button, '%s' stays queued.", title)
            return False
        await button.click()
        await self.sleep(6)

        try:
            state = json.loads(await self.page.evaluate(ACTIVATION_STATE_JS))
        except (json.JSONDecodeError, TypeError):
            state = {}
        outcome = activation_outcome(state.get("error", ""), bool(state.get("receipt")))
        logger.debug("Steam key %s… for '%s': %s (%s)", key[:5], title, outcome, str(state.get("error", ""))[:120])

        if outcome == "rate-limited":
            logger.warning("Steam asks to wait before more key activations, '%s' stays queued for the next run.", title)
            return False
        if outcome == "unknown":
            logger.warning("Could not read Steam's answer for '%s', the key stays queued for the next run.", title)
            await self.take_screenshot(f"steam_key_{filenamify(title)}")
            return False

        db_status, shown = KEY_OUTCOMES[outcome]
        if outcome == "activated":
            logger.info("✓ Activated the Steam key for '%s'.", title)
        elif outcome == "owned":
            logger.info("'%s' is already on your Steam account, the key stays unused in your Fanatical library.", title)
        else:
            logger.warning("Steam did not take the key for '%s' (%s), it stays in your Fanatical library.", title, outcome)
        async with async_session() as session:
            row = await session.get(ClaimedGame, row_id)
            if row:
                row.status = db_status
                await session.commit()
        self.notify_games.append({"title": label, "url": URL_REGISTER_KEY, "status": shown})
        return True

    # ------------------------------------------------------------------
    # Finding free games
    # ------------------------------------------------------------------

    async def _fetch_free_from_store(self) -> list[dict]:
        """Read Steam's search for paid items that are free right now, the way a person browses the store."""
        try:
            await self.page.get(STORE_FREE_SEARCH + ("" if cfg.steam_claim_dlc else GAMES_ONLY))
            await self.sleep(3)
            page = json.loads(await self.page.evaluate(SEARCH_PAGE_JS))
        except Exception as exc:
            logger.warning("Could not read Steam's store search: %s", exc)
            return []
        games = free_search_games(page.get("rows", ""))
        shown = page.get("shown", 0)
        logger.debug("Steam search: %d row(s) shown, %d free to keep", shown, len(games))
        # shortcut: only the first page is read, scroll for the rest if Steam ever gives that many away at once.
        if shown >= SEARCH_PAGE_SIZE:
            logger.warning("Steam's search shows %d free items or more, only the first %d were read this run.", shown, shown)
        if shown and not games:
            logger.warning("Steam's search shows %d item(s) but none read as 100%% off, the page may have changed.", shown)
        return games

    # ------------------------------------------------------------------
    # Cookie banner
    # ------------------------------------------------------------------

    async def _dismiss_cookie_banner(self) -> None:
        """Dismiss Steam's cookie consent popup if present by clicking 'Reject All'.
        
        The banner loads lazily (typically 1-3s after page load), so we poll
        for up to 5 seconds before giving up.
        NOTE: Steam uses <div> elements for cookie buttons, NOT <button> tags!
        """
        for _ in range(10):  # 10 × 0.5s = 5s max wait
            try:
                dismissed = await self.page.evaluate('''
                    (() => {
                        const allEls = [...document.querySelectorAll('div, button, span')];
                        const reject = allEls.find(el => {
                            const t = (el.textContent || '').trim();
                            return t === 'Reject All' || t === 'Odrzuć wszystkie';
                        });
                        if (reject) { reject.click(); return true; }
                        return false;
                    })()
                ''')
                if dismissed:
                    logger.debug("Rejected Steam cookie consent (privacy)")
                    await self.sleep(1)
                    return
            except Exception:
                pass
            await self.sleep(0.5)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _ensure_logged_in(self, return_url: str) -> bool:
        """Check login status on current page, log in if needed, return to game url; False only when nobody finished it."""
        async def _is_logged_in() -> bool:
            result = await self.page.evaluate(
                """
                JSON.stringify((() => {
                    const el = document.querySelector('#account_pulldown');
                    if (el) {
                        const text = (el.textContent || '').trim();
                        if (text.length > 0) return { loggedIn: true, user: text };
                    }
                    const links = document.querySelectorAll('a.global_action_link');
                    for (const a of links) {
                        if (a.getAttribute('href')?.includes('/login/')) return { loggedIn: false, user: '' };
                    }
                    return { loggedIn: true, user: '' };
                })())
                """
            )
            try:
                data = json.loads(result) if isinstance(result, str) else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            if data.get("loggedIn"):
                self.user = data.get("user", "") or cfg.steam_username or "unknown"
                return True
            return False

        if await _is_logged_in():
            if not self.user or self.user == "unknown":
                self.log_signed_in()
            return True

        # Not logged in → navigate directly to login page
        logger.warning("Not signed in – redirecting to login page…")
        await self.page.get(URL_LOGIN)
        await self.sleep(2)
        await self._dismiss_cookie_banner()

        username, password = cfg.steam_username, cfg.steam_password
        if username and password:
            await self._do_login()
            
            # Verify auto-login worked
            await self.page.get(return_url)
            await self.sleep(3)
            if await _is_logged_in():
                self.log_signed_in()
                return True

            # Auto-login failed (CAPTCHA, wrong creds, etc.) → fall back to VNC
            logger.warning("Auto-login failed. Falling back to VNC manual login…")
            await self.page.get(URL_LOGIN)
            await self.sleep(2)
        else:
            logger.warning("STEAM_USERNAME / STEAM_PASSWORD not set.")

        # VNC fallback: let user log in manually
        logged_in = await self._wait_for_vnc_login(_is_logged_in)
        if not logged_in:
            logger.warning("VNC login timed out – skipping.")
            return False

        # Verify login and get username
        await self.page.get(return_url)
        await self.sleep(3)
        if await _is_logged_in():
            self.log_signed_in()
        else:
            # The VNC check saw you signed in, so an unclear page here is not a failed sign-in.
            self.user = cfg.steam_username or "unknown"
            logger.warning("Could not verify login, continuing as %s", self.user)
        return True

    async def _do_login(self) -> None:
        """Perform Steam login with stored credentials.

        Assumes browser is already on the /login/ page.
        """
        username = cfg.steam_username
        password = cfg.steam_password

        logger.debug("Using stored credentials")

        # --- Username ---
        user_input = await self.page.find('div[data-featuretarget="login"] input[type="text"]', timeout=10)
        if user_input:
            await user_input.click()
            await self.sleep(0.5)
            await user_input.clear_input()
            await self.sleep(0.3)
            await user_input.send_keys(username)
            await self.sleep(0.5)
        else:
            logger.warning("Could not find username input")
            return

        # --- Password ---
        pw_input = await self.page.find('input[type="password"]', timeout=5)
        if pw_input:
            await pw_input.click()
            await self.sleep(0.5)
            await pw_input.clear_input()
            await self.sleep(0.3)
            await pw_input.send_keys(password)
            await self.sleep(0.5)
        else:
            logger.warning("Could not find password input")
            return

        # --- Remember Me (keep session alive across restarts) ---
        try:
            remember = await self.page.evaluate(REMEMBER_ME_JS)
            await self.sleep(0.5)
            logger.debug("Remember me: %s, ticked now: %s", remember, await self.page.evaluate(REMEMBER_ME_STATE_JS))
        except Exception as e:
            logger.debug("Remember me could not be read: %s", e)

        # --- Submit ---
        submit = await self.page.find('div[data-featuretarget="login"] button[type="submit"]', timeout=5)
        if submit:
            await submit.click()
            logger.debug("Clicked Sign In, waiting for response...")
            await self.sleep(3)

        # --- Steam Guard / 2FA ---
        await self._handle_steam_guard()

    async def _handle_steam_guard(self) -> None:
        """Wait for Steam Guard code entry if required.
        
        Steam Guard shows a code input after successful credential submission.
        We notify via Discord and wait for the user to enter the code via VNC.
        """
        for attempt in range(60):  # Wait up to 60 seconds
            current_url = await self.page.evaluate("window.location.href")
            
            # Login is complete when we reach the store domain
            # (checking "/login" not in url was fragile, CAPTCHA/challenge redirects broke the loop)
            if url_has_allowed_host(current_url, "store.steampowered.com") and "/login" not in current_url:
                logger.debug("Login redirect detected, Steam Guard not needed or already passed")
                return
            
            # Check for Steam Guard code input
            has_guard = await self.page.evaluate('''
                (() => {
                    const inputs = document.querySelectorAll('input[type="text"]');
                    for (const inp of inputs) {
                        if (inp.maxLength <= 6 || inp.closest('[class*="guard"], [class*="twofactor"], [class*="auth"]')) {
                            return true;
                        }
                    }
                    // Also check for any "Enter the code" type text
                    const body = document.body?.innerText || '';
                    if (body.includes('Steam Guard') || body.includes('access code') || body.includes('two-factor')) {
                        return true;
                    }
                    return false;
                })()
            ''')
            
            if has_guard:
                logger.warning("⚠ Steam Guard detected, waiting for you to finish it.")

                async def _guard_done() -> bool:
                    url = await self.page.evaluate("window.location.href")
                    return (isinstance(url, str)
                            and url_has_allowed_host(url, "store.steampowered.com")
                            and "/login" not in url)

                # The shared wait sends the notification and honours VNC_LOGIN_TIMEOUT.
                if await self._wait_for_vnc_login(_guard_done, custom_msg=self._vnc_notice(
                        "Steam: Steam Guard code needed",
                        "Enter the Steam Guard code in the browser, or approve the login on your Steam mobile app.")):
                    logger.info("Steam Guard passed successfully!")
                else:
                    logger.warning("Steam Guard wait timed out")
                return
            
            await self.sleep(1)

    # ------------------------------------------------------------------
    # Claim a game
    # ------------------------------------------------------------------

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15), reraise=True)
    async def _claim_game(self, game: dict) -> None:
        """Claim a single free game on Steam."""
        url = game.get("url", "")
        title = game.get("title", "Unknown")
        app_id = game.get("app_id", "")
        source = game.get("source", "unknown")

        try:
            await self.page.get(url)
            await self.sleep(4)
            await self._dismiss_cookie_banner()
        except Exception as exc:
            logger.warning("Failed to navigate to %s: %s", url, exc)
            return

        current_url = await self.page.evaluate("window.location.href")

        # Where the link really landed decides the id, a package (/sub/) included.
        landed = STEAM_ITEM.match(current_url)
        if landed:
            app_id = landed.group(2)
        elif source == "gamerpower":
            logger.info("⏭️ GamerPower redirect didn't land on Steam store: %s, skipping", current_url)
            async with async_session() as session:
                await get_or_create(
                    session, store="steam", user=account_key(cfg.steam_username, self.user),
                    game_id=game.get("giveaway_url", url), title=title,
                    url=current_url, status="not_steam",
                )
                await session.commit()
            return

        # Check login status ON the game page
        if not await self._ensure_logged_in(current_url):
            self._signed_out = True
            return
        current_url = await self.page.evaluate("window.location.href")

        # Handle age gate
        if "agecheck/app" in current_url:
            await self._handle_age_gate()

        # Get actual game title from page
        page_title_raw = await self.page.evaluate(
            """JSON.stringify({ title: document.querySelector('#appHubAppName')?.textContent?.trim() || '' })"""
        )
        try:
            pt = json.loads(page_title_raw) if isinstance(page_title_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            pt = {}
        page_title = pt.get("title", "") or title

        logger.debug("Game: %s (app_id: %s)", page_title, app_id)

        notify_game = {"title": page_title, "url": current_url, "status": "failed"}
        self.notify_games.append(notify_game)

        # Owned first: the base-game check leaves this page and could misread a DLC you have (#78).
        owned_raw = await self.page.evaluate(
            """JSON.stringify({ owned: document.querySelector('.game_area_already_owned') !== null })"""
        )
        try:
            owned = json.loads(owned_raw) if isinstance(owned_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            owned = {}

        if owned.get("owned"):
            logger.info("'%s' already in library.", page_title)
            async with async_session() as session:
                obj, _ = await get_or_create(
                    session, store="steam", user=account_key(cfg.steam_username, self.user),
                    game_id=app_id, title=page_title, url=current_url, status="existed",
                )
                obj.status = "existed"
                await session.commit()
            notify_game["status"] = "existed"
            return

        # GamerPower sometimes files a DLC as a game, so the page has the last word.
        if not cfg.steam_claim_dlc and await self.page.evaluate("!!document.querySelector('.game_area_dlc_bubble')") is True:
            logger.info("Skipping DLC '%s' (STEAM_CLAIM_DLC=false).", page_title)
            notify_game["status"] = "skipped:dlc"
            return

        # Check if this is a pure Free-to-Play game with nothing to claim.
        # IMPORTANT: DLC for F2P games (e.g. World of Warships DLC) can be
        # temporarily free to keep, those WILL have an "Add to Account" button.
        # Only skip if there's NO claim button at all.
        is_unclaimed_f2p = await self.page.evaluate('''
            (() => {''' + DEMO_JS + '''
                // If there's any claim button or form, this IS claimable, don't skip
                const freeBtn = document.querySelector('#freeGameBtn');
                if (freeBtn && !isDemoBtn(freeBtn)) return false;
                const addBtn = document.querySelector('[data-action="add_to_account"]');
                if (addBtn && !isDemoBtn(addBtn)) return false;
                // Check for DLC/sublicense forms (language-agnostic)
                const forms = document.querySelectorAll('.game_area_purchase_game form');
                for (const form of forms) {
                    if (isDemoBlock(form.closest('.game_area_purchase_game'))) continue;
                    if (form.querySelector('input[name="subid"]')) return false;
                }
                // Check for ANY green purchase button
                const greenBtn = [...document.querySelectorAll('.game_area_purchase_game .btn_green_steamui')]
                    .find(b => !isDemoBtn(b));
                if (greenBtn) return false;
                // Check for any button with "Free" text that looks claimable
                const allBtns = document.querySelectorAll('.game_area_purchase_game .btn_medium, .game_area_purchase_game a.btn_green_steamui');
                for (const btn of allBtns) {
                    if (isDemoBtn(btn)) continue;
                    const t = (btn.textContent || '').toLowerCase();
                    if (t.includes('free') || t.includes('install') || t.includes('add')) return false;
                }

                // No claim button found, check if it's just a F2P title
                const purchaseArea = [...document.querySelectorAll('.game_area_purchase_game')]
                    .find(b => !isDemoBlock(b));
                if (purchaseArea) {
                    const text = purchaseArea.textContent || '';
                    if (text.includes('Play Game')) return true;
                    if (text.includes('Free To Play') || text.includes('Free to Play')) return true;
                }
                return false;
            })()
        ''')
        if is_unclaimed_f2p:
            logger.info("'%s' is Free-to-Play with no claim button. Skipping.", page_title)
            async with async_session() as session:
                obj, _ = await get_or_create(
                    session, store="steam", user=account_key(cfg.steam_username, self.user),
                    game_id=app_id, title=page_title, url=current_url, status="free_to_play",
                )
                obj.status = "free_to_play"
                await session.commit()
            notify_game["status"] = "skipped:f2p"
            return

        # ── SAFETY: Verify the item is genuinely FREE before attempting to claim ──
        # Some games have multiple purchase blocks (e.g. DLC with paid + free editions).
        # We MUST verify that the specific item we're about to claim costs 0.
        price_check_raw = await self.page.evaluate(r'''
            JSON.stringify((() => {''' + DEMO_JS + r'''
                // Check for any price indicator on the page
                const purchaseBlocks = document.querySelectorAll('.game_area_purchase_game_wrapper, .game_area_purchase_game');
                let hasFreeBlock = false;
                let hasPaidBlock = false;
                let paidPrice = '';

                for (const block of purchaseBlocks) {
                    if (isDemoBlock(block)) continue;
                    const text = block.textContent || '';
                    // Check if this block is free (-100%, 0.00, 0,00, Free, Complimentary)
                    const isFree = text.includes('-100%') || 
                                   /\b0[.,]00\b/.test(text) ||
                                   text.includes('Free to Keep') ||
                                   text.includes('Free To Play') ||
                                   text.includes('Play for free') ||
                                   text.includes('Complimentary') ||
                                   (text.includes('Free') && !text.includes('Free Weekend'));
                    
                    // Check if an "Add to Account" or form exists in this block
                    const hasClaimBtn = block.querySelector('[data-action="add_to_account"]') ||
                                       block.querySelector('form input[name="subid"]') ||
                                       block.querySelector('#freeGameBtn');
                    
                    if (isFree && hasClaimBtn) hasFreeBlock = true;
                    // Even without a claim button, a "Free" DLC purchase block counts
                    if (isFree && !hasClaimBtn) {
                        const greenBtn = block.querySelector('.btn_green_steamui, .btn_medium');
                        if (greenBtn) hasFreeBlock = true;
                    }
                    
                    // Check for paid blocks (discount but not -100%)
                    const discountEl = block.querySelector('.discount_pct, .discount_prices');
                    const priceEl = block.querySelector('.discount_final_price, .game_purchase_price');
                    if (priceEl && !isFree) {
                        hasPaidBlock = true;
                        paidPrice = priceEl.textContent.trim();
                    }
                }
                
                // Also check the global add_to_account button (outside purchase blocks)
                const globalAdd = document.querySelector('[data-action="add_to_account"]');
                if (globalAdd && !hasFreeBlock && !isDemoBtn(globalAdd)) {
                    // Standalone add_to_account button = free item (DLC page pattern)
                    hasFreeBlock = true;
                }
                
                // A giveaway is a 100% discount; a block that only says "Free" is free for good.
                const giveaway = [...purchaseBlocks].some(b => !isDemoBlock(b) && (b.textContent || '').includes('-100%'));
                return { hasFreeBlock, hasPaidBlock, paidPrice, giveaway };
            })())
        ''')
        try:
            price_info = json.loads(price_check_raw) if isinstance(price_check_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            price_info = {}

        if not price_info.get("hasFreeBlock") and price_info.get("hasPaidBlock"):
            paid_price = price_info.get("paidPrice", "unknown")
            logger.warning("⚠️ SKIPPING '%s': item is NOT free (price: %s). "
                           "Will not purchase paid content.", page_title, paid_price)
            notify_game["status"] = "skipped:paid"
            await self.take_screenshot(f"steam_paid_skip_{filenamify(page_title)}", failed=False)
            return

        if price_info and not price_info.get("giveaway"):
            logger.info("Skipping '%s': its page shows no 100%% discount, so it is not a giveaway.", page_title)
            notify_game["status"] = "skipped:not-a-giveaway"
            return

        # After the price checks, so a DLC that is free for good never pulls its base game into your library.
        has_base_game = await self._ensure_base_game(current_url)
        if not has_base_game:
            # Kept in the database so the summary tells you once, not every run (#78).
            async with async_session() as session:
                obj, _ = await get_or_create(
                    session, store="steam", user=account_key(cfg.steam_username, self.user),
                    game_id=app_id, title=page_title, url=current_url, status="failed:missing_base",
                )
                notify_game["status"] = track_missing_base(obj, remember=not cfg.dryrun)
                await session.commit()
            if notify_game["status"] == "failed:missing_base":
                logger.warning("Skipping DLC '%s' because required base game is missing.", page_title)
            else:
                logger.info("Skipping DLC '%s', its base game is still missing (already reported).", page_title)
            return

        # Re-check age gate after returning from base game page
        current_url = await self.page.evaluate("window.location.href")
        if "agecheck/app" in current_url:
            await self._handle_age_gate()
            await self.sleep(2)

        if cfg.dryrun:
            logger.info("DRYRUN – skipped '%s'.", page_title)
            notify_game["status"] = "available (dry run)"
            return

        # Try to claim – look for various claim buttons (language-agnostic)
        # IMPORTANT: When a game has BOTH a "Play Game" (temporary F2P) section
        # AND a "Get Game" (free-to-keep, -100%) section, #freeGameBtn is often
        # the "Play Game" button which does NOT claim the game.
        # We MUST check add_to_account and form-based claims BEFORE freeGameBtn.
        # SAFETY: Only claim from blocks verified to be free (-100% or 0.00 price).
        claimed_raw = await self.page.evaluate(
            r"""
            JSON.stringify((() => {""" + DEMO_JS + r"""
                // Helper: check if a purchase block is genuinely free
                function isFreeBlock(block) {
                    if (!block || isDemoBlock(block)) return false;
                    const text = block.textContent || '';
                    return text.includes('-100%') || 
                           /\b0[.,]00\b/.test(text) ||
                           text.includes('Free to Keep') ||
                           text.includes('Complimentary') ||
                           (text.includes('Free') && !text.includes('Free Weekend'));
                }

                // 1. Try data-action="add_to_account", but only in a free context
                const addBtn = document.querySelector('[data-action="add_to_account"]');
                if (addBtn && !isDemoBtn(addBtn)) {
                    // Walk up to find the purchase block and verify it's free
                    let parent = addBtn.closest('.game_area_purchase_game_wrapper') ||
                                 addBtn.closest('.game_area_purchase_game');
                    if (parent && isFreeBlock(parent)) {
                        addBtn.click();
                        return { method: 'add_to_account' };
                    }
                    // Standalone add_to_account (common on free DLC pages), safe to click
                    if (!parent) {
                        addBtn.click();
                        return { method: 'add_to_account_standalone' };
                    }
                }

                // 2. Try submitting the free-to-keep form specifically
                const purchaseBlocks = document.querySelectorAll('.game_area_purchase_game_wrapper, .game_area_purchase_game');
                for (const block of purchaseBlocks) {
                    if (!isFreeBlock(block)) continue;
                    
                    const form = block.querySelector('form');
                    if (form) {
                        const subInput = form.querySelector('input[name="subid"]');
                        if (subInput) {
                            form.submit();
                            return { method: 'form_submit', subid: subInput.value };
                        }
                    }
                }

                // 3. Try #freeGameBtn ONLY if it's in a free context
                const freeBtn = document.querySelector('#freeGameBtn');
                if (freeBtn && !isDemoBtn(freeBtn)) {
                    let parent = freeBtn.closest('.game_area_purchase_game_wrapper') ||
                                 freeBtn.closest('.game_area_purchase_game');
                    if (!parent || isFreeBlock(parent)) {
                        freeBtn.click();
                        return { method: 'freeGameBtn' };
                    }
                }

                return { method: null };

            })())
            """
        )
        try:
            claimed = json.loads(claimed_raw) if isinstance(claimed_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            claimed = {}

        method = claimed.get("method")
        if not method:
            logger.warning("No claim button found for '%s'.", page_title)
            notify_game["status"] = "failed"
            return

        logger.debug("Clicked claim button (%s) for '%s', verifying...", method, page_title)
        await self.sleep(5)

        # Check if Steam shows an error after the claim attempt
        claim_error = await self.page.evaluate('''
            (() => {
                const body = document.body?.innerText || '';
                if (body.includes('problem adding this product') ||
                    body.includes('Oops, sorry') ||
                    body.includes('error was encountered')) {
                    return true;
                }
                return false;
            })()
        ''')
        if claim_error:
            logger.warning("Claim failed for '%s': Steam returned an error page.", page_title)
            notify_game["status"] = "failed"
            await self.take_screenshot(f"steam_fail_{filenamify(page_title)}")
            return

        if not await self._in_library(current_url):
            logger.warning("'%s' is not in your library after the claim (%s).", page_title, method)
            notify_game["status"] = "failed:unconfirmed"
            await self.take_screenshot(f"steam_fail_{filenamify(page_title)}")
            return

        logger.info("✓ Claimed '%s'!", page_title)

        async with async_session() as session:
            obj, _ = await get_or_create(
                session, store="steam", user=account_key(cfg.steam_username, self.user),
                game_id=app_id, title=page_title, url=current_url, status="claimed",
            )
            obj.status = "claimed"
            await session.commit()
            
        notify_game["status"] = "claimed"
        await self.take_screenshot(f"steam_{filenamify(page_title)}", failed=False)

    # ------------------------------------------------------------------
    # Base game check (DLC)
    # ------------------------------------------------------------------

    async def _ensure_base_game(self, dlc_url: str) -> bool:
        """Check if DLC requires a base game, and try to add it if it's free.
        
        Returns:
            True if no base game required OR base game is now owned/added.
            False if base game is paid and not owned.
        """
        # Look for the .game_area_dlc_bubble which contains the base game requirement link
        base_cmd_raw = await self.page.evaluate('''
            JSON.stringify((() => {
                const bubble = document.querySelector('.game_area_dlc_bubble');
                if (!bubble) return { required: false };
                
                const link = bubble.querySelector('a');
                if (!link) return { required: true, url: null }; // Required but cant find link
                
                return { required: true, url: link.href };
            })())
        ''')
        try:
            base_info = json.loads(base_cmd_raw) if isinstance(base_cmd_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            base_info = {"required": False}
            
        if not base_info.get("required"):
            return True  # Not a DLC or no requirement banner
            
        base_url = base_info.get("url")
        if not base_url:
            logger.warning("DLC requires a base game, but couldn't find the store link.")
            return False
            
        logger.debug("DLC requires a base game. Checking base game: %s", base_url)
        
        # Navigate to base game
        await self.page.get(base_url)
        await self.sleep(4)
        
        # Check if already owned
        owned_raw = await self.page.evaluate(
            "JSON.stringify({ owned: document.querySelector('.game_area_already_owned') !== null })"
        )
        try:
            owned = json.loads(owned_raw) if isinstance(owned_raw, str) else {}
            if owned.get("owned"):
                logger.debug("Base game is already in library.")
                await self.page.get(dlc_url)  # Return to DLC
                await self.sleep(3)
                return True
        except (json.JSONDecodeError, TypeError):
            pass
            
        bg_title = await self.page.evaluate(
            "document.querySelector('#appHubAppName')?.textContent?.trim() || 'Required Base Game'"
        )
        add_raw = await self.page.evaluate(base_game_button_js(cfg.dryrun))
        try:
            add_res = json.loads(add_raw) if isinstance(add_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            add_res = {"success": False}

        if add_res.get("success") and cfg.dryrun:
            logger.info("DRYRUN – would add the free base game '%s' first.", bg_title)
            self.notify_games.append({"title": bg_title, "url": base_url, "status": "available (dry run)"})
            await self.page.get(dlc_url)
            await self.sleep(4)
            return True

        if add_res.get("success"):
            await self.sleep(5)
            if not await self._in_library(base_url):
                logger.warning("The free base game '%s' did not reach your library (%s).", bg_title, add_res.get("method"))
                self.notify_games.append({"title": bg_title, "url": base_url, "status": "failed:unconfirmed"})
                await self.take_screenshot(f"steam_fail_{filenamify(bg_title)}")
                return False

            logger.info("✓ Added the free base game '%s' to your library.", bg_title)
            self.notify_games.append({"title": bg_title, "url": base_url, "status": "claimed"})
            logger.debug("Returning to DLC page...")
            await self.page.get(dlc_url)
            await self.sleep(4)
            return True

        logger.debug("Base game is not free or could not be added.")
        return False

    async def _in_library(self, url: str, tries: int = 3) -> bool:
        """Reload the store page until Steam marks it as in your library, the only proof a claim went through."""
        for attempt in range(1, tries + 1):
            await self.page.get(url)
            await self.sleep(4)
            here = await self.page.evaluate("window.location.href")
            if isinstance(here, str) and "agecheck/app" in here:
                await self._handle_age_gate()
                await self.sleep(2)
            if await self.page.evaluate("document.querySelector('.game_area_already_owned') !== null") is True:
                return True
            logger.debug("Not in the library yet (read %d of %d) on %s", attempt, tries, await self._page_brief())
        return False

    async def _handle_age_gate(self) -> None:
        """Handle Steam's age verification gate."""
        await self.sleep(1)
        await self.page.evaluate(
            """
            (() => {
                const day = document.querySelector('#ageDay');
                if (day) day.value = '21';
                const month = document.querySelector('#ageMonth');
                if (month) month.value = 'January';
                const year = document.querySelector('#ageYear');
                if (year) year.value = '1990';
                const btn = document.querySelector('#view_product_page_btn');
                if (btn) btn.click();
            })()
            """
        )
        await self.sleep(3)


async def claim_steam(extra_games: list | None = None) -> dict:
    """Convenience entry point."""
    claimer = SteamClaimer()
    await claimer.run(extra_games)
    return {"store": "Steam", "user": claimer.user, "games": claimer.notify_games}
