"""GOG store module – claims free giveaways and redeems Prime Gaming codes on GOG.com."""

from __future__ import annotations

import json
import logging

import nodriver as uc
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.claimer import BaseClaimer, now_str
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.gog")

URL_CLAIM = "https://www.gog.com/en"


class GOGClaimer(BaseClaimer):
    store_name = "gog"

    async def run(self) -> None:
        """Main entry point for the GOG claiming flow."""
        logger.debug("Starting GOG claiming flow")
        try:
            # Step 1: Open a Chrome browser with stealth patches
            await self.start_browser()

            # Step 2: Navigate to the GOG homepage
            await self.page.get(URL_CLAIM)
            await self.sleep(3)

            # Step 3: Make sure we are logged in (or wait for VNC manual login)
            if not await self._ensure_logged_in():
                logger.error("Aborting GOG claim flow due to login failure.")
                return

            # Step 4: Look for a free game giveaway and claim it
            await self._claim_giveaway()

        except Exception as exc:
            logger.exception("Fatal error")
            # Send a notification about the crash if notifications are enabled
            if cfg.notify_errors:
                await self.notify(f"gog failed: {exc}")
        finally:
            # Always close the browser, even if there was an error
            await self.close_browser()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _ensure_logged_in(self) -> bool:
        """Check if we are logged in to GOG. If not, try automatic login or wait for VNC."""
        import json
        await self.sleep(3)  # Give GOG time to fully render the page
        
        # GOG shows a cookie consent popup (CookieBot) that blocks the page.
        # We click "Allow All" to dismiss it, then remove the popup element from the page entirely.
        await self.page.evaluate("""
            (() => {
                const accept = document.querySelector('#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll');
                if (accept) { accept.click(); }
                else {
                    // Fallback: search for any button with common accept/reject text
                    const btns = [...document.querySelectorAll('button, a.button, a')];
                    const acceptBtn = btns.find(b => {
                        const t = (b.textContent || '').trim().toLowerCase();
                        return t === 'allow all' || t === 'zaakceptuj' || t === 'accept all' || t === 'akceptuj' || t === 'reject all' || t === 'odrzuć wszystko';
                    });
                    if (acceptBtn) acceptBtn.click();
                }
                // Remove the cookie popup and its dark overlay from the page
                const cookiebot = document.querySelector('#CybotCookiebotDialog');
                if (cookiebot) cookiebot.remove();
                const backdrop = document.querySelector('#CybotCookiebotDialogBodyUnderlay');
                if (backdrop) backdrop.remove();
                // Re-enable scrolling (cookie popup disables it)
                document.body.style.overflow = 'auto';
            })()
        """)
        await self.sleep(1)

        async def _is_logged_in() -> bool:
            """Check if the user is logged in by looking at the page content.
            
            We try multiple methods because GOG's layout changes frequently:
            1. Look for the account menu button (shows username when logged in)
            2. Look for a username displayed anywhere on the page
            3. Check if a "Sign in" link exists (means NOT logged in)
            4. Check if an avatar image exists (means logged in)
            """
            result_raw = await self.page.evaluate(
                """
                JSON.stringify((() => {
                    // Strategy 1: menuAccountButton with textContent
                    const menuBtn = document.querySelector('[hook-test="menuAccountButton"]');
                    const menuText = menuBtn ? (menuBtn.textContent || '').trim() : '';

                    // Strategy 2: look for GOG username in various known selectors
                    const usernameEl = document.querySelector('.menu-username')
                        || document.querySelector('.menu-user-name')
                        || document.querySelector('[class*="username"]')
                        || document.querySelector('[class*="user-name"]')
                        || document.querySelector('[class*="account"] [class*="name"]');
                    const usernameText = usernameEl ? (usernameEl.textContent || '').trim() : '';

                    // Strategy 3: check if "Sign in" link exists
                    let hasSignIn = false;
                    document.querySelectorAll('a').forEach(a => {
                        const t = (a.textContent || '').trim().toLowerCase();
                        if (t === 'sign in' || t === 'log in') hasSignIn = true;
                    });

                    // Strategy 4: check for avatar/logged-in indicator
                    const avatar = document.querySelector('.menu-avatar, .menu-user-avatar, [class*="avatar"]');

                    // Determine login state
                    const user = menuText || usernameText || '';
                    const loggedIn = (user.length > 0) || (!hasSignIn && !!menuBtn);

                    return {
                        loggedIn,
                        user,
                        debug: {
                            menuBtnExists: !!menuBtn,
                            menuText,
                            usernameText,
                            hasSignIn,
                            hasAvatar: !!avatar,
                        },
                    };
                })())
                """
            )
            try:
                result = json.loads(result_raw) if isinstance(result_raw, str) else {}
            except (json.JSONDecodeError, TypeError):
                result = {}

            debug = result.get("debug", {})
            logger.debug("Login check: menuBtn=%s, menuText='%s', usernameText='%s', hasSignIn=%s, hasAvatar=%s",
                         debug.get("menuBtnExists"), debug.get("menuText"),
                         debug.get("usernameText"), debug.get("hasSignIn"), debug.get("hasAvatar"))

            if result.get("loggedIn"):
                self.user = result.get("user", "") or "GOG User"
                return True
            return False

        # First check: are we already logged in from a previous session?
        if await _is_logged_in():
            self.log_signed_in()
            return True

        # Not logged in — try to find the "Sign in" button on the page
        sign_in = await self.page.find("Sign in", timeout=5)
        if not sign_in:
            logger.error("Could not find Sign in button or username.")
            return False

        logger.warning("Not signed in – attempting login…")
        await sign_in.click()
        await self.sleep(3)

        # Check if the user provided GOG credentials in the .env file
        email, password = cfg.gog_email, cfg.gog_password
        if not email or not password:
            # No credentials available — ask the user to log in manually via VNC
            logger.warning("GOG_EMAIL / GOG_PASSWORD not set.")
            logged_in = await self._wait_for_vnc_login(_is_logged_in)
            if not logged_in:
                logger.warning("VNC login timed out – skipping.")
            return logged_in

        # GOG's login form is inside an iframe from a different domain (login.gog.com).
        # The browser security model prevents us from interacting with cross-domain iframes.
        # Workaround: navigate directly to the login page URL so the form loads as the main page.
        # After successful login, GOG automatically redirects us back to gog.com.
        LOGIN_URL = "https://login.gog.com/auth?client_id=46755278331571209&redirect_uri=https%3A%2F%2Fwww.gog.com%2Fon_login_success%3FreturnTo%3D%2Fen%2F&response_type=code&layout=default&locale=en-US"
        logger.debug("Navigating directly to login.gog.com...")
        await self.page.get(LOGIN_URL)
        await self.page.evaluate('if(document.documentElement) document.documentElement.setAttribute("translate", "no");')
        await self.sleep(3)

        try:
            # Wait for the react form to fully render before injecting
            await self.page.find("#login_username", timeout=15)
            await self.page.find("#login_login", timeout=5)
            
            import json
            js_email = json.dumps(email)
            js_password = json.dumps(password)

            # Fill the inputs instantly using JS to prevent Google Translate popups from stealing focus
            completed = await self.page.evaluate(f'''
                (() => {{
                    const u = document.querySelector("#login_username");
                    const p = document.querySelector("#login_password");
                    const b = document.querySelector("#login_login");
                    if (u && p && b) {{
                        let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        if(setter) {{
                            setter.call(u, {js_email});
                            u.dispatchEvent(new Event("input", {{bubbles: true}}));
                            setter.call(p, {js_password});
                            p.dispatchEvent(new Event("input", {{bubbles: true}}));
                        }}
                        b.click();
                        return true;
                    }}
                    return false;
                }})()
            ''')
            if completed:
                logger.debug("Injected credentials and clicked login automatically.")
            else:
                logger.error("Could not find GOG login inputs to inject.")
                
            await self.sleep(5)
        except Exception:
            logger.exception("Login form interaction failed")

        # After successful login, GOG redirects to gog.com/on_login_success then to /en/
        # Wait for redirect back to gog.com and check login status
        for idx in range(15):
            current_url = await self.page.evaluate("window.location.href")
            logger.info("GOG Wait Loop %s: current_url is %s", idx, current_url)
            
            await self.page.evaluate('if(document.documentElement) document.documentElement.setAttribute("translate", "no");')
            
            if isinstance(current_url, str) and "gog.com/en" in current_url:
                # We're back on the main site, check login
                await self.sleep(2)
                if await _is_logged_in():
                    self.log_signed_in()
                    return True
                    
            # Check for 2FA / Email Verification (URL is the most reliable, DOM text as fallback)
            is_2fa = isinstance(current_url, str) and "two_factor" in current_url
            if not is_2fa:
                is_2fa = await self.page.evaluate("""
                    (() => {
                        const t = (document.body.innerText || '').toLowerCase();
                        return (t.includes('two-step') || t.includes('two step') || 
                                t.includes('2-step') || t.includes('verification code') || 
                                t.includes('kod weryfikacyjny') || t.includes('kod z e-maila') ||
                                t.includes('security code') || t.includes('weryfikacja dwuetapowa') ||
                                t.includes('kod zabezpieczający') || t.includes('authenticator app') ||
                                t.includes('aplikację uwierzytelniającą'));
                    })()
                """)
            
            if is_2fa:
                if cfg.gog_otp_enable and cfg.gog_otp_codes:
                    used_codes_file = cfg._data_dir / "used_gog_codes.txt"
                    used_codes = []
                    if used_codes_file.exists():
                        used_codes = used_codes_file.read_text("utf-8").splitlines()
                    
                    # Find first unused code
                    raw_code = None
                    for c in cfg.gog_otp_codes:
                        if c not in used_codes:
                            raw_code = c
                            break
                            
                    if raw_code:
                        code_to_use = raw_code.replace("-", "").replace(" ", "")[:8]
                        logger.info("GOG 2FA detected! GOG_OTP_ENABLE is true. Using backup code %s...", code_to_use[:3] + "*****")
                        
                        # Ensure we are on the backup code page
                        if "backup" not in current_url:
                            await self.page.get("https://login.gog.com/login/two_factor/backup")
                            await self.sleep(2)
                        
                        # Fill the inputs
                        await self.page.evaluate(f'''
                            (() => {{
                                const code = "{code_to_use}";
                                const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                                if (inputs.length > 0) {{
                                    inputs.forEach((inp, i) => {{
                                        if (i < code.length) {{
                                            let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                            if(setter) setter.call(inp, code[i]);
                                            inp.dispatchEvent(new Event("input", {{bubbles: true}}));
                                        }}
                                    }});
                                }}
                            }})()
                        ''')
                        await self.sleep(1)
                        
                        # Click the continue/submit button
                        await self.page.evaluate('''
                            const btn = document.querySelector('button[type="submit"]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.toLowerCase().includes('contin') || b.textContent.toLowerCase().includes('konty'));
                            if (btn) btn.click();
                        ''')
                        await self.sleep(5)
                        
                        # Record the code as used in the persistent data directory
                        with used_codes_file.open("a", encoding="utf-8") as f:
                            f.write(raw_code + "\n")
                        logger.debug("Successfully consumed a backup code and appended it to %s", used_codes_file.name)
                        continue # Jump to the next iteration of the loop to verify login again
                    else:
                        logger.warning("All provided GOG_OTP_CODES have been exhausted! Falling back to VNC...")

                logger.warning("GOG Two-Step Verification detected (Email/App 2FA)! Falling back to VNC...")
                
                async def _vnc_check_gog_2fa() -> bool:
                    url = await self.page.evaluate("window.location.href")
                    if isinstance(url, str) and "login.gog" not in url:
                        # Redirected away from login page, check if logged in
                        return await _is_logged_in()
                    return False
                    
                logged_in = await self._wait_for_vnc_login(
                    _vnc_check_gog_2fa,
                    timeout=180,
                    custom_msg=self._vnc_notice(
                        "GOG — 2FA code needed",
                        "GOG needs a 2FA verification code. Open the browser and enter it.",
                        180,
                    ),
                )
                if logged_in:
                    self.log_signed_in()
                    return True
                return False

            await self.sleep(2)
            if idx == 13:
                logger.warning("Automated login stuck (captcha?). Falling back to VNC...")
                # Still on login page without 2FA detected. To allow the generic VNC login
                # check to work on the main site, we navigate back to main page.
                await self.page.get(URL_CLAIM)
                await self.sleep(3)
                logged_in = await self._wait_for_vnc_login(_is_logged_in, timeout=120)
                if logged_in:
                    self.log_signed_in()
                    return True
                else:
                    return False

        # Final check - navigate to main page and verify
        await self.page.get(URL_CLAIM)
        await self.sleep(3)
        if await _is_logged_in():
            self.log_signed_in()
            return True

        logger.error("Could not verify login after attempts.")
        return False

    # ------------------------------------------------------------------
    # Claim giveaway
    # ------------------------------------------------------------------

    # Retry up to 2 times if claiming fails (with increasing wait between attempts)
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15), reraise=True)
    async def _claim_giveaway(self) -> None:
        """Look for a free giveaway on the GOG homepage and claim it."""
        await self.page.get(URL_CLAIM)
        await self.sleep(3)

        # GOG uses Angular which loads content dynamically, so the giveaway banner
        # might take a few seconds to appear. We try up to 10 times (20 seconds total).
        title = None
        for _ in range(10):
            # Run JavaScript on the page to find the giveaway banner link
            title = await self.page.evaluate(
                """(() => {
                    // Look for the giveaway overlay link (the big banner at the top)
                    const link = document.querySelector('#giveaway .giveaway__overlay-link, a.giveaway__overlay-link');
                    if (link && link.href) {
                        // Extract the game name from the URL, e.g. "the_whispering_valley"
                        const parts = link.href.split('/');
                        return parts[parts.length - 1];
                    }
                    // Fallback: search all links on the page for anything in the giveaway section
                    const allLinks = Array.from(document.querySelectorAll('a'));
                    const gwLink = allLinks.find(a => a.getAttribute('selenium-id') === 'giveawayOverlayLink' || (a.href && a.href.includes('/game/') && a.closest('#giveaway')));
                    if (gwLink && gwLink.href) {
                        const parts = gwLink.href.split('/');
                        return parts[parts.length - 1];
                    }
                    return null;
                })()"""
            )
            if title:
                break
            await self.sleep(2)

        if not title:
            logger.info("No free giveaway currently available.")
            return

        # Convert URL slug to readable title: "the_whispering_valley" → "The Whispering Valley"
        title = title.replace("_", " ").title()
        
        logger.info("Current free game: %s", title)

        # In dry run mode, log the game but don't actually claim it
        if cfg.dryrun:
            logger.info("DRYRUN – skipped '%s'.", title)
            self.notify_games.append({"title": title, "url": "https://www.gog.com", "status": "available (dry run)"})
            return

        # GOG has a direct claim endpoint that returns a JSON response.
        # Navigating to this URL triggers the claim without needing to click buttons.
        await self.page.get("https://www.gog.com/giveaway/claim")
        await self.sleep(2)

        # Read the JSON response from the claim endpoint
        body = await self.page.evaluate("document.body.innerText")

        # Save the result to the database
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="gog", user=self.user or "unknown",
                game_id=title, title=title, url=URL_CLAIM,
            )

            notify_game = {"title": title, "url": URL_CLAIM, "status": "failed"}

            # Empty JSON "{}" means the game was claimed successfully
            if body.strip() == "{}":
                status = "claimed"
                logger.info("✓ Claimed '%s' successfully!", title)
            else:
                # Parse the error/info message from the response
                try:
                    resp = json.loads(body)
                    message = resp.get("message", "")
                except json.JSONDecodeError:
                    message = body

                # "Already claimed" means the game is already in our library
                if message == "Already claimed":
                    status = "existed"
                    logger.info("'%s' already in library.", title)
                else:
                    status = message or "failed"
                    logger.warning("Claim response: %s", message)

            # Update the database record with the claim result
            obj.status = status
            notify_game["status"] = status
            self.notify_games.append(notify_game)
            await session.commit()

        # GOG automatically subscribes you to their newsletter when you claim a game.
        # If the user hasn't opted in (GOG_NEWSLETTER=0), unsubscribe automatically.
        if status == "claimed" and not cfg.gog_newsletter:
            logger.debug("Unsubscribing from newsletter")
            await self.page.get("https://www.gog.com/en/account/settings/subscriptions")
            await self.sleep(3)

    async def redeem_pending_codes(self) -> None:
        """Find GOG codes from Prime Gaming that haven't been redeemed yet, and redeem them."""
        from sqlalchemy import select
        from src.core.database import async_session, ClaimedGame
        from src.core.config import cfg
        
        # Query the database for codes that need to be redeemed
        async with async_session() as session:
            if cfg.gog_force_redeem:
                # Force mode: re-try ALL codes from the last 60 days (except already redeemed ones)
                from datetime import datetime, timedelta, timezone
                three_months_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
                stmt = select(ClaimedGame).where(
                    ClaimedGame.code.isnot(None),
                    ClaimedGame.code != "",
                    ClaimedGame.created_at >= three_months_ago,
                    ClaimedGame.status != "already redeemed"
                )
            else:
                # Normal mode: only try codes that are marked as "claimed" but not yet redeemed
                stmt = select(ClaimedGame).where(
                    ClaimedGame.status == "claimed",
                    ClaimedGame.code.isnot(None),
                    ClaimedGame.code != ""
                )
            result = await session.execute(stmt)
            old_games = result.scalars().all()
            
        if old_games:
            logger.debug("Checking for pending GOG codes: %d total external codes in DB", len(old_games))
        
        import json
        # Filter the results to only include GOG codes (not Legacy Games, Epic, etc.)
        gog_games = []
        for g in old_games:
            is_gog = False
            # Check the "extra" JSON field for the external store identifier
            if g.extra:
                try:
                    ext_store = json.loads(g.extra).get("external_store", "")
                    if "gog" in ext_store.lower():
                        is_gog = True
                except Exception:
                    pass
            
            # A GOG code is either explicitly tagged as GOG, or matches the pattern:
            # exactly 18 uppercase alphanumeric characters with no hyphens
            if is_gog or (len(g.code) == 18 and "-" not in g.code and g.code.isalnum() and g.code.isupper()):
                gog_games.append(g)
            else:
                logger.debug("Skipped non-GOG code: '%s' code=%s extra=%s", g.title, g.code, g.extra)

        if not gog_games:
            # Silent exit if nothing to do
            return
            
        logger.info("Found %d pending GOG code(s) from external sources. Starting browser...", len(gog_games))
        try:
            # Open a browser and log in to GOG
            await self.start_browser()
            await self.page.get(URL_CLAIM)
            await self.sleep(3)
            
            if not await self._ensure_logged_in():
                logger.error("Aborting GOG pending codes redemption due to login failure.")
                return
            
            # Redeem each GOG code one by one
            for g in gog_games:
                await self._redeem_gog_code(g.code, g.title, g.url)
        except Exception:
            logger.exception("Fatal error during pending codes redemption")
        finally:
            await self.close_browser()

    async def _redeem_gog_code(self, code: str, title: str, url: str) -> None:
        """Navigate to gog.com/redeem/<code> and complete the redemption process."""
        # GOG has a direct redemption URL: gog.com/redeem/XXXXXXXXXXXXX
        # Force English locale via /en/ path to get predictable button labels
        redeem_url = f"https://www.gog.com/en/redeem/{code}"
        logger.info("Redeeming GOG code for '%s' at %s", title, redeem_url)

        try:
            await self.page.get(redeem_url)
            await self.sleep(5)

            # Check if redirected to login
            current_url = await self.page.evaluate("window.location.href")
            if isinstance(current_url, str) and url_has_allowed_host(current_url, "login.gog.com"):
                logger.warning("Not logged in to GOG – need manual login via VNC.")
                async def _gog_logged_in() -> bool:
                    cur = await self.page.evaluate("window.location.href")
                    return isinstance(cur, str) and "redeem" in cur
                logged_in = await self._wait_for_vnc_login(_gog_logged_in, timeout=60)
                if not logged_in:
                    logger.warning("GOG login timed out – code not redeemed: %s", code)
                    self.notify_games.append({"title": title, "url": url, "status": f"code: {code} (GOG, not redeemed)"})
                    return

            # Explicitly dismiss CookieBot via click to remember the consent, then nuke from DOM
            await self.page.evaluate("""
                (() => {
                    const accept = document.querySelector('#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll');
                    if (accept) { accept.click(); }
                    else {
                        const btns = [...document.querySelectorAll('button, a.button, a')];
                        const acceptBtn = btns.find(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return t === 'allow all' || t === 'zaakceptuj' || t === 'accept all' || t === 'akceptuj' || t === 'reject all' || t === 'odrzuć wszystko';
                        });
                        if (acceptBtn) acceptBtn.click();
                    }
                    
                    const cookiebot = document.querySelector('#CybotCookiebotDialog');
                    if (cookiebot) cookiebot.remove();
                    const backdrop = document.querySelector('#CybotCookiebotDialogBodyUnderlay');
                    if (backdrop) backdrop.remove();
                    document.body.style.overflow = 'auto';
                })()
            """)
            await self.sleep(1)

            # Click the "Continue" button on the redemption page.
            # We use JavaScript to find and click it because GOG has overlay elements
            # that block normal clicks. Supports multiple languages.
            await self.page.evaluate("""
                (() => {
                    const btns = [...document.querySelectorAll('button, a.button, input[type="submit"]')];
                    const btn = btns.find(b => {
                        const t = (b.textContent || b.value || '').trim().toLowerCase();
                        return t === 'continue' || t === 'kontynuuj' || t === 'continuer' || t === 'weiter' || t === 'continuar';
                    });
                    if (btn && !btn.disabled) btn.click();
                })()
            """)
            await self.sleep(5)

            # Click the final "Activate" / "Redeem" button to add the game to our GOG library.
            # GOG uses different labels depending on the page language:
            #   EN: "Activate" or "Redeem"  |  PL: "Aktywuj" or "Odbierz"  |  DE: "Einlösen"  |  ES: "Canjear"
            await self.page.evaluate("""
                (() => {
                    const targets = ['activate', 'aktywuj', 'redeem', 'odbierz', 'zrealizuj', 'einlösen', 'canjear', 'activer'];
                    const btns = [...document.querySelectorAll('button, a.button, input[type="submit"]')];
                    const btn = btns.find(b => {
                        const t = (b.textContent || b.value || '').trim().toLowerCase();
                        return targets.some(target => t === target || t.includes(target));
                    });
                    if (btn && !btn.disabled) btn.click();
                })()
            """)
            await self.sleep(5)

            # Check the page to see if redemption was successful.
            # We look at the page heading and message boxes for success/already-redeemed indicators.
            # Supports both English and Polish text on the GOG website.
            result_state = await self.page.evaluate("""
                (() => {
                    const h1 = (document.querySelector('h1')?.textContent || '').toLowerCase();
                    const msgBox = (document.querySelector('.messages-container, .redeem__message, .status-msg')?.textContent || '').toLowerCase();
                    const bodyPart = (document.querySelector('.layout-body, main, #main, .content')?.textContent || '').toLowerCase();

                    if (h1.includes('success') || h1.includes('sukces') || msgBox.includes('success') || msgBox.includes('sukces') || msgBox.includes('zrealizowano') || msgBox.includes('redeemed')) return 'success';
                    if (bodyPart.includes('already') || bodyPart.includes('już') || msgBox.includes('already')) return 'already';
                    if (bodyPart.includes('success') && bodyPart.includes('order')) return 'success';
                    return 'unknown';
                })()
            """)

            from src.core.database import async_session, ClaimedGame
            from sqlalchemy import select
            
            # Update the database based on the redemption result
            if result_state == 'success':
                logger.info("✓ Redeemed '%s' on GOG!", title)
                self.notify_games.append({"title": title, "url": redeem_url, "status": "claimed and redeemed (GOG)"})
                
                # Mark the code as fully redeemed in the database
                async with async_session() as session:
                    stmt = select(ClaimedGame).where(ClaimedGame.code == code)
                    result = await session.execute(stmt)
                    obj = result.scalars().first()
                    if obj:
                        obj.status = "claimed and redeemed"
                        await session.commit()
                        
            elif result_state == 'already':
                logger.info("'%s' already redeemed on GOG.", title)
                self.notify_games.append({"title": title, "url": redeem_url, "status": "already redeemed (GOG)"})
                
                # Mark as already redeemed so we don't try again next time
                async with async_session() as session:
                    stmt = select(ClaimedGame).where(ClaimedGame.code == code)
                    result = await session.execute(stmt)
                    obj = result.scalars().first()
                    if obj:
                        obj.status = "already redeemed"
                        await session.commit()
            else:
                logger.warning("GOG redeem result unclear for '%s'. Code: %s", title, code)
                self.notify_games.append({"title": title, "url": redeem_url, "status": f"code: {code} (GOG, check manually)"})

        except Exception:
            logger.exception("Failed to redeem GOG code for '%s'", title)
            self.notify_games.append({"title": title, "url": url, "status": f"code: {code} (GOG, failed)"})

async def claim_gog() -> dict:
    """Convenience entry point."""
    claimer = GOGClaimer()
    await claimer.run()
    return {"store": "GOG", "user": claimer.user, "games": claimer.notify_games}
