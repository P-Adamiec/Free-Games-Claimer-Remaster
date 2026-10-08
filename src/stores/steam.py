"""Steam store module – claims free-to-keep games from the Steam Store."""

from __future__ import annotations

import json
import logging
import re

from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.claimer import BaseClaimer, filenamify
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.steam")

# SteamDB page listing upcoming and current free promotions
STEAMDB_FREE_URL = "https://steamdb.info/upcoming/free/"

# Steam Store URLs used for login and navigation
URL_STORE = "https://store.steampowered.com/?l=english"
URL_LOGIN = "https://store.steampowered.com/login/"

# A game's demo has its own purchase block and is always free, so it must never pass as the giveaway (#62).
DEMO_JS = r"""
    const isDemoBlock = el => !!el && (el.matches('.demo_above_purchase') || !!el.querySelector('.demo_above_purchase, #demoGameBtn'));
    const isDemoBtn = el => !!el && (!!el.closest('.demo_above_purchase, #demoGameBtn') || /\bdemo\b|playtest/i.test(el.textContent || ''));
"""

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

    async def run(self, extra_games: list | None = None) -> None:
        """Main entry point: find free Steam games and claim them.
        
        Flow:
        1. Start browser
        2. Scrape SteamDB for free-to-keep games
        3. Claim all SteamDB games
        """
        logger.debug("Starting Steam claiming flow")

        try:
            # Step 1: Open a browser with GPU acceleration enabled.
            await self.start_browser(
                force_headful=True,
                extra_args=[
                    "--ignore-gpu-blocklist",
                    "--enable-unsafe-webgpu",
                ],
            )

            # Step 2: Scrape SteamDB for free game listings
            sdb_games = await self._fetch_steamdb_via_browser()

            # Log SteamDB results
            if sdb_games:
                links = [f"  • [bold cyan]{g['title']}[/bold cyan] 🔗 {g.get('url', '')}" for g in sdb_games]
                logger.info("🎮 [bold magenta]SteamDB: %d free game(s):[/bold magenta]\n%s",
                            len(sdb_games), "\n".join(links))

            if not sdb_games:
                logger.info("No free games found on SteamDB. Done.")

            # Step 3: Claim SteamDB's games, then whatever GamerPower found for Steam.
            for game in sdb_games:
                await self._claim_game(game)
            for game in (extra_games or []):
                logger.info("🎮 [GamerPower] '%s' → Steam", game.get("title", "Unknown"))
                await self._claim_game({**game, "url": game["final_url"], "source": "gamerpower"})

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
            await self._ensure_logged_in(URL_STORE)
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

    async def _fetch_steamdb_via_browser(self) -> list[dict]:
        """Scrape SteamDB using the already-started browser.

        SteamDB returns 403 for direct HTTP requests (Cloudflare).
        Using the real browser bypasses that.
        """
        logger.debug("Fetching free games from SteamDB via browser")
        try:
            # Warm-up navigation to build history/trust and invisibly pass Cloudflare Turnstile
            logger.debug("Warming up session by navigating to SteamDB home page...")
            await self.page.get("https://steamdb.info/")
            await self.sleep(3)

            await self.page.get(STEAMDB_FREE_URL)
            await self.sleep(10)  # Wait for page load and any invisible Turnstile checks

            # SteamDB is behind Cloudflare; if the human-check shows instead of the listing, hand off to VNC.
            if await self._human_challenge_present():
                if await self._wait_out_challenge("Steam / SteamDB"):
                    await self.sleep(3)  # let the real listing finish loading
                else:
                    logger.warning("SteamDB Cloudflare challenge not cleared in time – skipping SteamDB.")
                    return []

            html_raw = await self.page.evaluate("document.documentElement.outerHTML")
            html = html_raw if isinstance(html_raw, str) else ""
            if cfg.debug:
                try:
                    (cfg._data_dir / "steamdb_dump.html").write_text(html, encoding="utf-8")
                except Exception as e:
                    logger.debug("Failed to dump SteamDB HTML: %s", e)
            if not html:
                logger.warning("SteamDB page returned empty content")
                return []
            logger.debug("SteamDB HTML loaded (length: %d)", len(html))
            return self._parse_steamdb_html(html)
        except Exception as exc:
            logger.warning("SteamDB scrape failed: %s", exc)
            return []

    def _parse_steamdb_html(self, html: str) -> list[dict]:
        """Parse SteamDB HTML to extract 'Free to Keep' games.

        The page structure has cards with:
        - "View Store" links → https://store.steampowered.com/app/APPID/... or /sub/...
        - Game title in <b> tags
        - "Free to Keep" (green) or "Play For Free" (orange) text
        """
        games = []
        skipped: list[str] = []

        # Split HTML by the SteamDB card class to strictly isolate each game
        cards = html.split('class="span4 panel-sale')

        # Skip the first element which contains the document <head> and global meta tags
        for card_html in cards[1:]:
            if not card_html.strip():
                continue

            # Find the "View Store" link pointing to Steam store within this card
            store_match = re.search(r'href="(https://store\.steampowered\.com/(?:app|sub|bundle)/(\d+)[^"]*)"', card_html)
            if not store_match:
                continue

            store_url = store_match.group(1)
            app_id = store_match.group(2)

            # Match the real green badge class, not free "Free to Keep" text (which also
            # appears in the page's FAQ and mislabels Play-For-Free cards like ICARUS).
            if "cat-free-to-keep" not in card_html:
                skipped.append(f"{app_id}:not-free-to-keep")
                continue

            # Extract title from <b> or <h1>..<h6> tags
            title_match = re.search(r'<(?:b|strong|h[1-6])[^>]*>([^<]+)</(?:b|strong|h[1-6])>', card_html)
            title = title_match.group(1).strip() if title_match else f"Steam App {app_id}"

            # Fastly discard obvious incorrectly grabbed global links (e.g. Counter Strike 2 bug where title becomes steamdb.info)
            if "steamdb.info" in title:
                skipped.append(f"{app_id}:bad-title")
                continue

            games.append({
                "title": title,
                "url": store_url,
                "app_id": app_id,
                "source": "steamdb",
            })

        logger.debug("SteamDB: %d card(s) parsed, %d 'Free to Keep', skipped: %s",
                     len(cards) - 1, len(games), skipped or "none")
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

    async def _ensure_logged_in(self, return_url: str) -> None:
        """Check login status on current page, log in if needed, return to game url."""
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
            return

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
                return
            
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
            return

        # Verify login and get username
        await self.page.get(return_url)
        await self.sleep(3)
        if await _is_logged_in():
            self.log_signed_in()
        else:
            self.user = cfg.steam_username or "unknown"
            logger.warning("Could not verify login, continuing as %s", self.user)

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

        # Resolve the actual Steam URL and app_id if coming from GamerPower redirect
        if "store.steampowered.com/app" in current_url or "agecheck/app" in current_url:
            resolved_id = self._extract_game_id(current_url)
            if resolved_id:
                app_id = resolved_id
        elif source == "gamerpower":
            logger.info("⏭️ GamerPower redirect didn't land on Steam store: %s, skipping", current_url)
            async with async_session() as session:
                await get_or_create(
                    session, store="steam", user=self.user or "unknown",
                    game_id=game.get("giveaway_url", url), title=title,
                    url=current_url, status="not_steam",
                )
                await session.commit()
            return

        # Check login status ON the game page
        await self._ensure_logged_in(current_url)
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

        # Ensure base game is owned if this is a DLC
        has_base_game = await self._ensure_base_game(current_url)
        if not has_base_game:
            logger.warning("Skipping DLC '%s' because required base game is missing.", page_title)
            notify_game["status"] = "failed:missing_base"
            return

        # Re-check age gate after returning from base game page
        current_url = await self.page.evaluate("window.location.href")
        if "agecheck/app" in current_url:
            await self._handle_age_gate()
            await self.sleep(2)

        # Check if already owned
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
                    session, store="steam", user=self.user or "unknown",
                    game_id=app_id, title=page_title, url=current_url, status="existed",
                )
                obj.status = "existed"
                await session.commit()
            notify_game["status"] = "existed"
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
                    session, store="steam", user=self.user or "unknown",
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
                
                return { hasFreeBlock, hasPaidBlock, paidPrice };
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
            await self.take_screenshot(f"steam_paid_skip_{filenamify(page_title)}")
            return

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

        logger.info("✓ Claimed '%s'!", page_title)

        async with async_session() as session:
            obj, _ = await get_or_create(
                session, store="steam", user=self.user or "unknown",
                game_id=app_id, title=page_title, url=current_url, status="claimed",
            )
            obj.status = "claimed"
            await session.commit()
            
        notify_game["status"] = "claimed"
        await self.take_screenshot(f"steam_{filenamify(page_title)}")

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
            
        logger.info("DLC requires a base game. Checking base game: %s", base_url)
        
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
                logger.info("Base game is already in library.")
                await self.page.get(dlc_url)  # Return to DLC
                await self.sleep(3)
                return True
        except (json.JSONDecodeError, TypeError):
            pass
            
        # Try to add base game
        # For Free-to-Play games, clicking "Play Game" adds it to the library.
        # For Free-to-Keep games, clicking "Add to Account" does it.
        add_raw = await self.page.evaluate('''
            JSON.stringify((() => {''' + DEMO_JS + '''
                // Try "Add to Library" (Blue button, new Steam UI)
                const allBtns = [...document.querySelectorAll('.btn_medium')].filter(b => !isDemoBtn(b));
                const addLibBtn = allBtns.find(b => b.textContent.includes('Add to Library') || b.textContent.includes('Dodaj do biblioteki'));
                if (addLibBtn) { addLibBtn.click(); return { success: true, method: 'add_to_library' }; }

                // Try Add to Account first (Free to keep)
                const addBtn = document.querySelector('[data-action="add_to_account"]');
                if (addBtn && !isDemoBtn(addBtn)) { addBtn.click(); return { success: true, method: 'add_to_account' }; }

                // Try Play Game (Free to Play)
                const greenBtns = [...document.querySelectorAll('.btn_green_steamui.btn_medium')].filter(b => !isDemoBtn(b));
                const playBtn = greenBtns.find(b => b.textContent.includes('Play Game') || b.textContent.includes('Zagraj'));
                if (playBtn) { playBtn.click(); return { success: true, method: 'play_game' }; }
                
                return { success: false };
            })())
        ''')
        try:
            add_res = json.loads(add_raw) if isinstance(add_raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            add_res = {"success": False}
            
        if add_res.get("success"):
            logger.info("Added base game to library via %s. Wait 5s...", add_res.get("method"))
            
            bg_title_raw = await self.page.evaluate(
                "document.querySelector('#appHubAppName')?.textContent?.trim() || 'Required Base Game'"
            )
            self.notify_games.append({
                "title": bg_title_raw,
                "url": base_url,
                "status": "claimed"
            })
            
            await self.sleep(5)
            
            # Go back to the DLC page
            logger.debug("Returning to DLC page...")
            await self.page.get(dlc_url)
            await self.sleep(4)
            return True
            
        logger.warning("Base game is not free or could not be added.")
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

    @staticmethod
    def _extract_game_id(url: str) -> str | None:
        """Extract the numeric app ID from a Steam URL."""
        if "/app/" not in url:
            return None
        part = url.split("/app/")[1]
        return part.split("/")[0] if "/" in part else part


async def claim_steam(extra_games: list | None = None) -> dict:
    """Convenience entry point."""
    claimer = SteamClaimer()
    await claimer.run(extra_games)
    return {"store": "Steam", "user": claimer.user, "games": claimer.notify_games}
