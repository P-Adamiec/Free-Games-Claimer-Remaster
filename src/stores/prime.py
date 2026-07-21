"""Prime Gaming (Amazon) module – claims free games from the Prime Gaming catalogue."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import logging

import nodriver as uc
import pyotp
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.claimer import BaseClaimer, now_str, filenamify
from src.core.config import cfg
from src.core.database import async_session, get_or_create

logger = logging.getLogger("fgc.prime")

# Prime Gaming claims page URL (shows all available free games)
BASE_URL = "https://luna.amazon.com"
URL_CLAIM = f"{BASE_URL}/claims/home"

# Path to the JSON file where we export claimed games with codes
JSON_FILE = cfg._data_dir / "prime-gaming.json"


def _save_to_json(title: str, *, code: str = "", store: str = "", url: str = "", status: str = "") -> None:
    """Append/update a game entry in prime-gaming.json alongside the SQLite DB.
    
    Only saves games with actual codes (GOG, Legacy Games, etc.).
    Skips if the title already exists with a code (no duplicates).
    """
    try:
        data: dict = {}
        if JSON_FILE.exists():
            data = json.loads(JSON_FILE.read_text(encoding="utf-8"))
        # Skip if already saved with a code (no duplicates)
        if title in data and data[title].get("code"):
            return
        data[title] = {
            "code": code,
            "store": store,
            "url": url,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("Failed to write to %s", JSON_FILE, exc_info=True)


class PrimeGamingClaimer(BaseClaimer):
    store_name = "prime"

    async def run(self) -> None:
        """Main entry point: find and claim free games from Prime Gaming."""
        logger.debug("Starting Prime Gaming claiming flow")
        try:
            # Step 1: Open a browser and navigate to the Prime Gaming claims page
            await self.start_browser()

            # Force the password path: kill WebAuthn + Amazon's passkey/mshop sign-in forms.
            try:
                await self.page.send(
                    uc.cdp.page.add_script_to_evaluate_on_new_document(
                        source="""
                            try { Object.defineProperty(window, 'PublicKeyCredential', { configurable: true, get: () => undefined }); } catch (e) {}
                            try {
                                const noop = () => new Promise(() => {});  // never resolves, so no passkey error
                                if (navigator.credentials) { navigator.credentials.get = noop; navigator.credentials.create = noop; }
                            } catch (e) {}
                            (function () {
                                // Remove the wrong sign-in forms; leave only form[name="signIn"] (password).
                                const KILL = ['form[name="signInWithPasskeyButton"]', 'form[name="signInWithMShopButton"]', '#auth-signin-via-passkey-section', '#auth-signin-via-passkey-btn'];
                                const sweep = () => { try {
                                    KILL.forEach(s => document.querySelectorAll(s).forEach(el => el.remove()));
                                    document.querySelectorAll('[data-action="SIGNIN_PASSKEY_COLLECT"]').forEach(el => el.removeAttribute('data-action'));
                                } catch (e) {} };
                                try {
                                    sweep();
                                    document.addEventListener('DOMContentLoaded', sweep);
                                    new MutationObserver(sweep).observe(document, { childList: true, subtree: true });
                                } catch (e) {}
                            })();
                        """
                    )
                )
            except Exception as e:
                self.logger.debug("Passkey neutralization script injection exception: %s", e)

            await self.page.get(URL_CLAIM)
            await self.sleep(4)

            # Step 2: Make sure we are logged into Amazon
            await self._ensure_logged_in()

            # Step 3: Find and claim all available free games
            await self._claim_internal_games()

            # Note: GOG codes extracted here are redeemed later by main.py
            # to avoid browser profile conflicts (GOG needs its own browser).

        except Exception as exc:
            logger.exception("Fatal error")
            if cfg.notify_errors:
                await self.notify(f"prime-gaming failed: {exc}")
        finally:
            # Export claimed games with codes to a JSON file for user convenience
            try:
                await self._export_legacy_json()
            except Exception as e:
                logger.error("Failed to export legacy JSON: %s", e)

            # Summary notifications
            if self.notify_games and cfg.notify_summary:
                self.logger.info("Prime Gaming claimer finished with %d games.", len(self.notify_games))

            # Always close the browser when done
            await self.close_browser()

            # --- Note: GOG code redemption was moved to main.py to execute post-run cleanly ---

    async def _export_legacy_json(self) -> None:
        """Export all Prime games with codes to data/prime-gaming.json.
        
        This file is a convenience export for users who want to see their
        claimed codes in a readable format. Only includes games with actual
        redeemable codes (not account-linked games like Epic or Amazon).
        """
        import json
        from pathlib import Path
        from sqlalchemy import select
        from src.core.database import ClaimedGame
        
        async with async_session() as session:
            stmt = select(ClaimedGame).where(ClaimedGame.store == "prime")
            result = await session.execute(stmt)
            prime_games = result.scalars().all()
            
        out = {}
        for g in prime_games:
            usr = g.user or "unknown"
            if usr not in out:
                out[usr] = {}
            if g.code:
                out[usr][g.title] = {
                    "title": g.title,
                    "code": g.code,
                    "status": g.status,
                    "url": g.url,
                    "store": g.store,
                }
                
        out_file = Path(cfg._data_dir) / "prime-gaming.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=4, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _click_sign_in(self) -> bool:
        """Click Luna's exact sign-in button, never a fuzzy 'Sign in' text match."""
        try:
            btn = await self.page.select('button[data-a-target="sign-in-button"]', timeout=4)
            if btn:
                await btn.click()
                return True
        except Exception:
            pass
        # Fallback: mark the element whose EXACT text is 'Sign in' and click it.
        try:
            marked = await self.page.evaluate("""
                (() => {
                    const el = [...document.querySelectorAll('button, a, [role="button"]')]
                        .find(b => (b.textContent || '').trim() === 'Sign in');
                    if (el) { el.setAttribute('data-fgc-signin', '1'); return true; }
                    return false;
                })()
            """)
            if marked:
                el = await self.page.select('[data-fgc-signin="1"]', timeout=2)
                if el:
                    await el.click()
                    return True
        except Exception:
            pass
        return False

    async def _passkey_error_present(self) -> bool:
        """True when Amazon's passkey error is showing (a dead-end for the bot)."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const el = document.querySelector('#passkey-error-alert');
                    if (el && el.offsetParent !== null && !(el.className || '').includes('aok-hidden')) return true;
                    const b = (document.body ? (document.body.innerText || '') : '').toLowerCase();
                    return b.includes('no passkeys for this account') || b.includes("your passkey isn't working");
                })()
            """))
        except Exception:
            return False

    async def _is_signed_in(self) -> bool:
        """Return True on a positive Luna signed-in signal (account header)."""
        result = await self.page.evaluate(
            """
            JSON.stringify((() => {
                const vis = (el) => !!el && el.offsetParent !== null;
                const name = document.querySelector('[data-a-target="user-dropdown-first-name-text"]');
                if (name) {
                    const t = (name.textContent || '').trim();
                    if (t.length > 0) return { loggedIn: true, user: t };
                }
                // Account dropdown exists only when signed in.
                if (document.querySelector('[data-a-target="amazon-dropdown-header-interactable"], [data-a-target="amazon-dropdown-header-message"]')) {
                    return { loggedIn: true, user: '' };
                }
                // A visible sign-in button means signed out.
                if (vis(document.querySelector('[data-a-target="sign-in-button"]'))) return { loggedIn: false, user: '' };
                return { loggedIn: false, user: '' };
            })())
            """
        )
        try:
            data = json.loads(result) if isinstance(result, str) else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        if data.get("loggedIn"):
            user = data.get("user", "")
            if user:
                self.user = user
            elif not self.user:
                self.user = "unknown"
            return True
        return False

    async def _ensure_logged_in(self, silent_if_logged_in: bool = False) -> None:
        """Check if logged in; if not, click 'Sign in' in a loop until logged in.

        Matches the JS pattern: sometimes after Amazon login completes,
        Prime Gaming still shows 'Sign in' and needs another click to finalize.
        This while loop keeps clicking and re-checking.
        """
        await self.sleep(3)

        async def _has_sign_in_button() -> bool:
            result = await self.page.evaluate(
                """
                JSON.stringify({
                    found: !!Array.from(document.querySelectorAll('button'))
                        .find(b => b.textContent.trim() === 'Sign in')
                })
                """
            )
            try:
                data = json.loads(result) if isinstance(result, str) else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            return data.get("found", False)

        if await self._is_signed_in():
            if not silent_if_logged_in:
                self.log_signed_in()
            return
            
        if silent_if_logged_in:
            logger.warning("Session lost mid-operation! Attempting re-login…")

        # --- Login loop: keep clicking "Sign in" until logged in ---
        email, password = cfg.pg_email, cfg.pg_password
        max_attempts = 5

        for attempt in range(max_attempts):
            if not await _has_sign_in_button():
                # No sign in button, but also not logged in — wait a moment and recheck
                await self.sleep(3)
                if await self._is_signed_in():
                    break
                # Maybe page hasn't loaded yet, reload
                await self.page.get(URL_CLAIM)
                await self.sleep(4)
                if await self._is_signed_in():
                    break
                continue

            logger.warning("'Sign in' button found (attempt %d/%d) – clicking…",
                           attempt + 1, max_attempts)

            # Click the exact Luna sign-in button (not fuzzy 'Sign in' text).
            if await self._click_sign_in():
                await self.sleep(3)

            if email and password:
                # Check if we landed on Amazon login page
                current_url = await self.page.evaluate("window.location.href")
                if isinstance(current_url, str) and ("signin" in current_url or "ap/signin" in current_url):
                    await self._do_login()
                    # A passkey dead-end won't clear by retrying — hand off to VNC.
                    if await self._passkey_error_present():
                        logger.warning("Amazon passkey error – automated login can't proceed.")
                        break
            else:
                if attempt == 0:
                    logger.warning("PG_EMAIL / PG_PASSWORD not set. Waiting for VNC login...")
                    
                    async def _vnc_check() -> bool:
                        if await self._is_signed_in():
                            return True
                            
                        # Workaround: sometimes Amazon redirects to home page but drops
                        # the auth session, requiring a second 'Sign in' click manually.
                        url = await self.page.evaluate("window.location.href")
                        if isinstance(url, str) and ("claims/home" in url or "gaming.amazon" in url):
                            if await _has_sign_in_button():
                                logger.info("Auto-clicking 'Sign in' again during manual wait (redirect bug)...")
                                if await self._click_sign_in():
                                    await self.sleep(3)
                        return False

                    logged_in = await self._wait_for_vnc_login(_vnc_check)
                    if not logged_in:
                        logger.warning("VNC login timed out – skipping.")
                        return

            # Navigate back to claims page and re-check
            await self.page.get(URL_CLAIM)
            await self.sleep(4)

            if await self._is_signed_in():
                self.log_signed_in()
                return

        # Automated login didn't complete – ask the user to finish it via VNC.
        logger.warning("Automated Prime login did not complete – requesting manual VNC login.")
        custom_msg = self._vnc_notice(
            "Prime Gaming / Amazon Luna — login needs you",
            "Automated sign-in couldn't finish (passkey or extra verification). Open the browser and finish signing in.",
        )
        if await self._wait_for_vnc_login(self._is_signed_in, custom_msg=custom_msg):
            self.log_signed_in()
            return
        logger.warning("Prime login still not completed after VNC wait – skipping.")

    async def _do_login(self) -> None:
        """Fill in email + password on the Amazon login page."""
        import json
        email = cfg.pg_email
        password = cfg.pg_password
        
        # --- Handle "Switch accounts" / Saved Profile Screen ---
        # Amazon often skips the email input if the browser session remembers
        # the user's previous login, presenting a "Switch accounts" selection.
        await self.page.evaluate('''
            (() => {
                const targetEmail = (%s || '').toLowerCase().trim();
                const bodyText = (document.body.innerText || '').toLowerCase();
                const isSwitchScreen = bodyText.includes('switch accounts') || bodyText.includes('add account') || bodyText.includes('przełącz konto') || bodyText.includes('dodaj konto');
                if (!isSwitchScreen) return;

                const allEls = [...document.querySelectorAll('div, span, a, button, li, label')].filter(el => {
                    const t = (el.innerText || el.textContent || '').trim();
                    return t && el.tagName !== 'BODY' && el.tagName !== 'HTML' && !el.className.includes('header');
                });
                // Sort by length ascending to match the most specific (deepest) elements first
                allEls.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);

                // 1. Try to click existing profile whose text includes our target email
                if (targetEmail) {
                    for (const el of allEls) {
                        const t = (el.innerText || el.textContent || '').toLowerCase();
                        if (t.includes(targetEmail)) {
                            const clickable = el.closest('a, button, [role="button"], [role="link"], li, [class*="profile"], [class*="switcher"], [class*="account"], [class*="item"]') || el;
                            clickable.click();
                            return;
                        }
                    }
                }

                // 2. If target profile not found or email empty, click "Add account" / "Use another account"
                const addKeywords = ['add account', 'use another account', 'dodaj konto', 'użyj innego konta', 'another account', 'innego konta'];
                for (const el of allEls) {
                    const t = (el.innerText || el.textContent || '').toLowerCase();
                    if (addKeywords.some(kw => t.includes(kw))) {
                        const clickable = el.closest('a, button, [role="button"], [role="link"], li, [class*="add"], [class*="account"], [class*="item"]') || el;
                        clickable.click();
                        return;
                    }
                }
            })()
        ''' % json.dumps(email))
        await self.sleep(2)

        # Email field
        email_input = await self.page.find("#ap_email", timeout=10)
        if not email_input:
            email_input = await self.page.find('[name="email"]', timeout=5)
        if email_input:
            await email_input.apply('(el) => { let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set; if(setter) setter.call(el, ""); el.dispatchEvent(new Event("input", {bubbles: true})); }')
            await email_input.send_keys(email)
            continue_btn = await self.page.find("#continue", timeout=5)
            if continue_btn:
                await continue_btn.click()
                await self.sleep(2)

        # Password field
        password_input = await self.page.find("#ap_password", timeout=10)
        if not password_input:
            password_input = await self.page.find('[name="password"]', timeout=5)
        if password_input:
            await password_input.apply('(el) => { let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set; if(setter) setter.call(el, ""); el.dispatchEvent(new Event("input", {bubbles: true})); }')
            await password_input.send_keys(password)
            await self.sleep(1)
            # Send Enter key directly to submit form natively (most stealthy anti-bot approach)
            await password_input.send_keys("\r")
            await self.sleep(3)

        # Fallback check: if form did not submit via Enter and #signInSubmit is still present, click ONLY #signInSubmit
        try:
            submit_btn = await self.page.find("#signInSubmit", timeout=2)
            if submit_btn:
                await submit_btn.click()
        except Exception:
            pass
        await self.sleep(4)

        # Handle MFA (TOTP authenticator app)
        if cfg.pg_otpkey:
            await self.sleep(3)
            try:
                otp_input = await self.page.find("input[name='otpCode']", timeout=5)
                if otp_input:
                    otp_code = pyotp.TOTP(cfg.pg_otpkey).now()
                    logger.debug("Entering MFA code")
                    await otp_input.send_keys(otp_code)
                    submit = await self.page.find('input[type="submit"]', timeout=5)
                    if submit:
                        await submit.click()
                        await self.sleep(4)
            except Exception:
                pass  # No MFA prompt

        # Handle Amazon security code verification (shopping app push / SMS code).
        # Amazon may ask the user to enter a code sent to their Amazon shopping app
        # or via SMS. This requires manual intervention via VNC.
        await self._handle_security_code_challenge()

    # ------------------------------------------------------------------
    # Amazon security code (2FA via shopping app / SMS)
    # ------------------------------------------------------------------

    async def _handle_security_code_challenge(self) -> None:
        """Detect and handle Amazon's security code verification page.

        Amazon sometimes requires a security code sent via the Amazon shopping
        app or SMS after login. This page shows "Enter security code" with a
        text input and a "Verify" button. Since we can't read the code
        programmatically, we notify the user and wait for manual VNC entry.
        """
        # Give page a bit more time to render any popups/challenges
        await self.sleep(2)
        
        # Check if we landed on a security code page
        is_security_page = await self.page.evaluate("""
            (() => {
                const body = (document.body?.innerText || '').toLowerCase();
                // Detect both English and common variations, including SMS OTP
                return body.includes('enter security code') ||
                       body.includes('security code') && body.includes('verify') ||
                       body.includes('enter your code') && body.includes('amazon') ||
                       body.includes('approval required') ||
                       body.includes('approve the notification') ||
                       body.includes('two-step verification') ||
                       body.includes('one time password') ||
                       body.includes('text message with a one time password');
            })()
        """)

        if not is_security_page:
            return

        logger.warning("⚠️ Amazon security code verification required! Open %s to enter the code via VNC.", cfg.vnc_url)

        # _wait_for_vnc_login below sends the unified VNC notification.

        # Wait for the user to enter the code and the page to move on.
        # Uses the same timeout as VNC manual login (VNC_LOGIN_TIMEOUT).
        async def _security_code_done() -> bool:
            body = await self.page.evaluate(
                "(document.body?.innerText || '').toLowerCase()"
            )
            # If the page no longer shows the security code prompt, we're done
            return not (
                'enter security code' in body or
                'approval required' in body or
                ('security code' in body and 'verify' in body) or
                'two-step verification' in body or
                'one time password' in body
            )

        msg = self._vnc_notice(
            "Prime Gaming / Amazon — security code needed",
            "Amazon needs a verification code (SMS / app / email). Open the browser and enter it.",
        )

        resolved = await self._wait_for_vnc_login(_security_code_done, custom_msg=msg)
        if resolved:
            logger.info("✓ Security code accepted — continuing.")
        else:
            logger.warning("Security code timeout — login may have failed.")

    # ------------------------------------------------------------------
    # Scroll until stable (port from original JS)
    # ------------------------------------------------------------------

    async def _scroll_until_stable(self) -> None:
        """Scroll down repeatedly until the page height stabilizes (all games loaded).

        The original JS uses ``page.keyboard.press('PageDown')`` which sends a
        physical key event to the **focused element** — the game list is inside
        a nested scrollable container, NOT the window.  ``window.scrollBy()``
        would scroll the wrong element and never trigger lazy-loading.

        We simulate this by:
        1. Clicking inside the games container to focus it
        2. Using keyboard-style scroll via JS ``dispatchEvent(new KeyboardEvent(...))``
           or direct ``scrollBy`` on the correct container element
        3. Measuring ``.scrollHeight`` until it stabilizes (``waitUntilStable`` pattern)
        """
        # Click into the page to ensure focus is on the scrollable area
        try:
            await self.page.evaluate(
                """document.querySelector('.tw-full-width')?.click()"""
            )
        except Exception:
            pass

        prev_height = None
        stable_count = 0
        for _ in range(40):  # safety limit
            height = await self.page.evaluate(
                """(() => {
                    const container = document.querySelector('div[data-a-target="offer-list-FGWP_FULL"]');
                    return container ? container.children.length : 0;
                })()"""
            )
            logger.debug("scrollUntilStable cardCount=%s", height)
            if height == prev_height:
                stable_count += 1
                if stable_count >= 3:
                    break
            else:
                stable_count = 0
            prev_height = height
            # Use scrollIntoView on the last child to trigger IntersectionObserver lazy loading
            await self.page.evaluate(
                """(() => {
                    const container = document.querySelector('div[data-a-target="offer-list-FGWP_FULL"]');
                    if (container && container.lastElementChild) {
                        container.lastElementChild.scrollIntoView({ behavior: 'instant', block: 'end' });
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                })()"""
            )
            await self.sleep(3)

    # ------------------------------------------------------------------
    # Session recovery
    # ------------------------------------------------------------------

    async def _ensure_page_alive(self) -> None:
        """Check if the CDP tab/session is still responsive. If not, recover it.

        After login (especially VNC manual login), the browser may open a new tab
        or the original tab's CDP session may become invalid. This method detects
        that and re-attaches to a working tab so navigation doesn't crash.
        """
        try:
            # Quick health check: try to read the current URL
            await self.page.evaluate("window.location.href")
        except Exception:
            logger.warning("CDP session lost — attempting to recover...")
            try:
                # Get all open tabs from the browser and pick the first valid one
                tabs = self.browser.tabs
                if tabs:
                    self.page = tabs[0]
                    logger.info("Recovered CDP session on tab: %s", self.page.target.url)
                else:
                    # No tabs at all — open a fresh one
                    self.page = await self.browser.get("about:blank")
                    logger.info("Opened new tab after session loss.")
            except Exception:
                logger.exception("Session recovery failed — opening fresh tab.")
                self.page = await self.browser.get("about:blank")

    # ------------------------------------------------------------------
    # Claim games
    # ------------------------------------------------------------------

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15), reraise=True)
    async def _claim_internal_games(self) -> None:
        """Navigate to the games tab, load all, show stats, and claim."""
        # Ensure the CDP session is still alive before navigating
        # (login flow or VNC interaction may have invalidated the tab reference)
        await self._ensure_page_alive()

        await self.page.get(URL_CLAIM)
        await self.sleep(4)

        # Click the "Game" filter tab (may need a retry as the tab can load slowly)
        for attempt in range(3):
            game_tab = await self.page.find('button[data-type="Game"]', timeout=10)
            if game_tab:
                await game_tab.click()
                await self.sleep(3)
                break
            await self.sleep(2)

        # Wait for the games container to appear (give it more time)
        container_found = False
        for _ in range(20):
            has_container = await self.page.evaluate(
                """!!document.querySelector('div[data-a-target="offer-list-FGWP_FULL"]')"""
            )
            if has_container:
                container_found = True
                break
            await self.sleep(1)

        if not container_found:
            logger.warning("Games container not found after waiting. Trying to continue anyway.")

        # Scroll until all games are loaded
        await self._scroll_until_stable()

        # --- DOM diagnostics (helps debug zero-game issues) ---
        # NOTE: nodriver's evaluate() returns JS objects as Python lists,
        # NOT dicts. We must JSON.stringify() in JS and json.loads() in Python.
        import json

        diag_raw = await self.page.evaluate(
            """
            JSON.stringify((() => {
                const container = document.querySelector('div[data-a-target="offer-list-FGWP_FULL"]');
                const tw = document.querySelector('.tw-full-width');
                const allTargets = [...document.querySelectorAll('[data-a-target]')].map(
                    el => el.getAttribute('data-a-target')
                ).filter(t => t && t.includes('offer'));
                const allCards = document.querySelectorAll('[data-a-target="item-card"]').length;
                const allCollected = [...document.querySelectorAll('p')].filter(
                    p => p.textContent.trim() === 'Collected'
                ).length;
                return {
                    hasContainer: !!container,
                    containerChildren: container ? container.children.length : 0,
                    hasTwFullWidth: !!tw,
                    allOfferTargets: allTargets.slice(0, 10),
                    totalItemCards: allCards,
                    totalCollectedPs: allCollected,
                    currentURL: window.location.href,
                };
            })())
            """
        )
        diag = json.loads(diag_raw) if isinstance(diag_raw, str) else {}
        
        # DOM diagnostics: Useful for debugging when 'containerChildren' is 0 or games aren't detected properly.
        # logger.info("DOM diagnostics: container=%s, children=%s, totalCards=%s, collectedPs=%s, url=%s",
        #              diag.get("hasContainer"), diag.get("containerChildren"),
        #              diag.get("totalItemCards"), diag.get("totalCollectedPs"),
        #              diag.get("currentURL"))
        # logger.debug("offer targets: %s", diag.get("allOfferTargets"))

        if diag.get("containerChildren", 0) == 0:
            logger.warning("Container is empty or not found! It might be hidden or DOM structure changed.")

        # --- DB Fetch to skip known codes ---
        existing_codes_map = {}
        if cfg.pg_force_check_collected:
            from src.core.database import async_session, ClaimedGame
            from sqlalchemy import select
            async with async_session() as session:
                stmt = select(ClaimedGame).where(ClaimedGame.store == "prime", ClaimedGame.code.isnot(None), ClaimedGame.code != "")
                result = await session.execute(stmt)
                for g in result.scalars().all():
                    existing_codes_map[g.game_id or g.title] = g.code

        # --- Statistics ---
        force_check_str = "true" if cfg.pg_force_check_collected else "false"
        stats_raw = await self.page.evaluate(
            f"""
            JSON.stringify((() => {{
                const forceCheck = {force_check_str};
                const container = document.querySelector('div[data-a-target="offer-list-FGWP_FULL"]');
                if (!container) return {{ collected: 0, unclaimed: 0, games: [], totalCards: 0 }};

                // Count total item cards and collected ones
                // Use container.children to get ALL cards (some may lack .item-card__action)
                const allCards = container.querySelectorAll('.item-card__action');
                // Also count direct children as backup for total
                const containerChildren = container.children.length;
                let totalCards = Math.max(allCards.length, containerChildren);
                let collected = 0;
                allCards.forEach(card => {{
                    const ps = card.querySelectorAll('p');
                    ps.forEach(p => {{ if (p.textContent.trim() === 'Collected') collected++; }});
                }});

                const actionCards = container.querySelectorAll('.item-card__action');
                const games = [];

                    actionCards.forEach(card => {{
                        const btn = card.querySelector('button[data-a-target="FGWPOffer"]');
                        const link = card.querySelector('a[data-a-target="FGWPOffer"]');
                        let isUnclaimed = !!(btn || link);
                        
                        let targetIsHowToPlay = false;
                        let targetBtn = btn || link;
                        
                        if (!isUnclaimed) {{
                            if (!forceCheck) return;
                            // Search for the how to play / play game button on collected games
                            const htpBtn = Array.from(card.querySelectorAll('button, a')).find(b => b.textContent && (b.textContent.toLowerCase().includes('how to play') || b.textContent.toLowerCase().includes('play game')));
                            if (!htpBtn) return;

                            targetIsHowToPlay = true;
                            targetBtn = htpBtn;
                        }}

                        let p = card;
                        let titleText = 'Unknown';
                        while (p && p.tagName !== 'BODY') {{
                            const tEl = p.querySelector('.item-card-details__body__primary');
                            if (tEl && tEl.textContent.trim()) {{
                                titleText = tEl.textContent.trim();
                                break;
                            }}
                            if (p.getAttribute('aria-label')) {{
                                titleText = p.getAttribute('aria-label').replace('Claim ', '').trim();
                            }}
                            p = p.parentElement;
                        }}

                        const hrefValue = link ? link.getAttribute('href') : '';
                        
                        // Extract detail URL from any link on the card (item-how-to-play or learn-more-card)
                        let detailUrl = '';
                        const htpLink = card.querySelector('a[data-a-target="item-how-to-play"]');
                        if (htpLink) {{
                            detailUrl = htpLink.getAttribute('href') || '';
                        }}
                        if (!detailUrl) {{
                            // Walk up to find learn-more-card link
                            let ancestor = card;
                            while (ancestor && ancestor.tagName !== 'BODY') {{
                                if (ancestor.tagName === 'A' && ancestor.getAttribute('data-a-target') === 'learn-more-card') {{
                                    detailUrl = ancestor.getAttribute('href') || '';
                                    break;
                                }}
                                ancestor = ancestor.parentElement;
                            }}
                        }}
                        
                        // Extract platform from URL slug: /claims/game-name-PLATFORM/dp/
                        // Known slugs: epic, gog, legacy, aga (amazon)
                        let platform = '';
                        if (detailUrl) {{
                            const slugMatch = detailUrl.match(/\/claims\/(.+?)\/dp\//);
                            if (slugMatch) {{
                                const slug = slugMatch[1].toLowerCase();
                                const platformMap = {{
                                    'epic': 'epic games',
                                    'gog': 'gog',
                                    'legacy': 'legacy games',
                                    'aga': 'amazon',
                                }};
                                // Scan ALL hyphen-separated segments (from end to start)
                                // because the platform tag can appear anywhere in the slug
                                const parts = slug.split('-');
                                for (let i = parts.length - 1; i >= 0; i--) {{
                                    if (platformMap[parts[i]]) {{
                                        platform = platformMap[parts[i]];
                                        break;
                                    }}
                                }}
                            }}
                        }}

                        games.push({{
                            title: titleText,
                            href: hrefValue,
                            detailUrl: detailUrl,
                            platform: platform,
                            type: btn ? 'internal' : 'external',
                            alreadyCollected: targetIsHowToPlay,
                        }});
                    }});

                games.reverse();

                return {{
                    collected,
                    unclaimed: games.filter(g => !g.alreadyCollected).length,
                    games,
                    totalCards,
                }};
            }})())
            """
        )
        stats = json.loads(stats_raw) if isinstance(stats_raw, str) else {}

        total_cards = int(stats.get("totalCards", 0))
        collected = int(stats.get("collected", 0))
        all_games = stats.get("games", [])
        
        # When FORCE_CHECK is on, do NOT filter by existing codes — recheck everything
        # Epic/Amazon will be caught and skipped Python-side after clicking
        filtered_games = []
        for g in all_games:
            if not cfg.pg_force_check_collected and g.get("alreadyCollected") and existing_codes_map.get(g["title"]):
                continue
            filtered_games.append(g)

        total = total_cards
        unclaimed_new = len([g for g in filtered_games if not g.get("alreadyCollected")])
        to_recheck = len([g for g in filtered_games if g.get("alreadyCollected")])

        logger.info("Total games: %d | Already claimed: %d | Unclaimed: %d | To re-check (force): %d",
                    total, collected, unclaimed_new, to_recheck)

        if cfg.pg_force_check_collected:
            logger.info("PG_FORCE_CHECK_COLLECTED is enabled: explicitly parsing already collected tiles lacking codes in DB.")

        if not filtered_games:
            logger.info("No unclaimed games or pending caches.")
            return

        # --- Claim each game ---
        logger.info("Claiming/Checking %d game(s)…", len(filtered_games))
        for game_info in filtered_games:
            # Note: For checking already collected codes, the 'type' might default to external due to lack of standard 'href' layout.
            # But we can route them all to _claim_single_external which will scrape the external code properly.
            # Actually, "How to play" doesn't strictly have a href, so it falls to 'internal'. 
            # We'll just run _claim_single_external which has robust DOM scraping logic capable of catching the popup output!
            if game_info.get("alreadyCollected"):
                await self._claim_single_external(game_info)
            else:
                game_type = game_info.get("type", "internal")
                if game_type == "internal":
                    await self._claim_single_internal(game_info)
                else:
                    await self._claim_single_external(game_info)

    async def _claim_single_internal(self, game_info: dict) -> None:
        """Claim a single internal Prime Gaming game via button click."""
        title = game_info.get("title", "Unknown")
        href = game_info.get("href", "")
        url = BASE_URL + href.split("?")[0] if href else URL_CLAIM

        if cfg.dryrun:
            logger.info("DRYRUN – skipped '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": "available (dry run)"})
            return
            
        # Ensure session hasn't expired mid-loop before processing the card
        await self._ensure_logged_in(silent_if_logged_in=True)

        try:
            claimed = await self.page.evaluate(
                f"""
                (() => {{
                    const cards = document.querySelectorAll('.item-card__action');
                    for (const card of cards) {{
                        let p = card;
                        let text = 'Unknown';
                        while (p && p.tagName !== 'BODY') {{
                            const tEl = p.querySelector('.item-card-details__body__primary');
                            if (tEl && tEl.textContent.trim()) {{ text = tEl.textContent.trim(); break; }}
                            if (p.getAttribute('aria-label')) {{ text = p.getAttribute('aria-label'); }}
                            p = p.parentElement;
                        }}
                        
                        if (text.includes({repr(title)}) || text === {repr(title)}) {{
                            const btn = card.querySelector('button');
                            if (btn && btn.textContent.includes('Claim')) {{
                                btn.click();
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }})()
                """
            )

            if claimed:
                await self.sleep(3)
                logger.info("✓ Claimed '%s'!", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="prime", user=self.user or "unknown",
                        game_id=title, title=title, url=url, status="claimed",
                    )
                    obj.status = "claimed"
                    await session.commit()
                self.notify_games.append({"title": title, "url": url, "status": "claimed"})
                await self.take_screenshot(f"prime_{filenamify(title)}")
            else:
                logger.warning("Could not click claim for '%s'.", title)
                self.notify_games.append({"title": title, "url": url, "status": "failed"})

        except Exception:
            logger.exception("Failed to claim '%s'", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed"})

    async def _claim_single_external(self, game_info: dict) -> None:
        """Claim an external Prime Gaming game – get the code, redeem GOG, log others."""
        title = game_info.get("title", "Unknown")
        href = game_info.get("href", "")
        detail_url = game_info.get("detailUrl", "")
        platform = game_info.get("platform", "")  # From URL slug
        url = BASE_URL + href.split("?")[0] if href else URL_CLAIM

        if cfg.dryrun:
            logger.info("DRYRUN – skipped external '%s'.", title)
            self.notify_games.append({"title": title, "url": url, "status": f"available (dry run, {platform or 'external'})"})
            return

        # Ensure session hasn't expired mid-loop before processing the card
        await self._ensure_logged_in(silent_if_logged_in=True)

        try:
            # Navigate directly to the detail page if we have a detail URL
            # This is more reliable than finding and clicking the card by title
            # (avoids issues with lazy-loaded cards disappearing after page reload)
            if detail_url:
                full_detail_url = BASE_URL + detail_url if detail_url.startswith('/') else detail_url
                await self.page.get(full_detail_url)
                await self.sleep(4)

                # Session sometimes drops when navigating to a detail page.
                if not await self._is_signed_in():
                    logger.warning("Session lost on detail page for '%s' — attempting re-login…", title)
                    # Navigate back to claims page and re-authenticate
                    await self.page.get(URL_CLAIM)
                    await self.sleep(3)
                    await self._ensure_logged_in()
                    # Navigate back to the detail page after re-login
                    await self.page.get(full_detail_url)
                    await self.sleep(4)

                claimed = True
            else:
                # Fallback: try clicking the card on the claims page
                claimed = await self.page.evaluate(
                    f"""
                    (() => {{
                        const isAlreadyCollected = {str(game_info.get("alreadyCollected", False)).lower()};
                        if ({repr(href)} && !isAlreadyCollected) {{
                            const a = document.querySelector(`a[data-a-target="FGWPOffer"][href="${{{repr(href)}}}"]`) || 
                                      document.querySelector(`a[href="${{{repr(href)}}}"]`);
                            if (a) {{
                                a.click();
                                return true;
                            }}
                        }}
                        
                        const cards = document.querySelectorAll('.item-card__action');
                        for (const card of cards) {{
                            let p = card;
                            let text = 'Unknown';
                            while (p && p.tagName !== 'BODY') {{
                                const tEl = p.querySelector('.item-card-details__body__primary');
                                if (tEl && tEl.textContent.trim()) {{ text = tEl.textContent.trim(); break; }}
                                if (p.getAttribute('aria-label')) {{ text = p.getAttribute('aria-label'); }}
                                p = p.parentElement;
                            }}
                            
                            if (text.includes({repr(title)}) || text === {repr(title)}) {{
                                const a = card.querySelector('a[data-a-target="FGWPOffer"]');
                                const htp = Array.from(card.querySelectorAll('button, a')).find(b => b.textContent && (b.textContent.toLowerCase().includes('how to play') || b.textContent.toLowerCase().includes('play game')));
                                const btnToClick = {str(game_info.get('alreadyCollected', False)).lower()} ? htp : (a || htp);
                                
                                if (btnToClick) {{
                                    btnToClick.click();
                                    return true;
                                }}
                            }}
                        }}
                        return false;
                    }})()
                    """
                )

            if not claimed:
                logger.warning("Could not click claim for external '%s'.", title)
                self.notify_games.append({"title": title, "url": url, "status": "failed"})
                return

            if not detail_url:
                await self.sleep(4)

            # Click "Get game" / "Claim" / "Complete Claim" button
            await self.page.evaluate(
                """
                (() => {
                    const texts = ["Get game", "Claim", "Complete Claim"];
                    const els = document.querySelectorAll('button, a');
                    for (const el of els) {
                        const txt = el.textContent.trim();
                        if (texts.includes(txt) && el.offsetParent !== null) {
                            el.click();
                            return;
                        }
                    }
                })()
                """
            )
            await self.sleep(4)

            # Use platform from URL slug as primary store identification
            store = platform or 'unknown'
            
            # If URL slug didn't give us a platform, check the current page URL (fallback for click-navigated pages)
            if store == 'unknown':
                current_url = str(await self.page.evaluate("window.location.href"))
                import re
                slug_match = re.search(r'/claims/(.+?)/dp/', current_url)
                if slug_match:
                    slug = slug_match.group(1).lower()
                    platform_map = {
                        'epic': 'epic games',
                        'gog': 'gog',
                        'legacy': 'legacy games',
                        'aga': 'amazon',
                    }
                    for part in reversed(slug.split('-')):
                        if part in platform_map:
                            store = platform_map[part]
                            break

            # If still unknown, try text-based detection as fallback
            if store == 'unknown':
                store_raw = await self.page.evaluate(
                    """
                    (() => {
                        const bodyText = (document.body.innerText || '').toLowerCase();
                        const descEl = document.querySelector('[data-a-target="DescriptionItemDetails"]');
                        const descText = descEl ? descEl.textContent.toLowerCase() : '';
                        const allText = bodyText + ' ' + descText;
                        
                        const knownStores = [
                            { keywords: ['epic games store', 'epic games launcher', 'epicgames'], name: 'epic games' },
                            { keywords: ['gog.com', 'gog.com/redeem'], name: 'gog' },
                            { keywords: ['legacy games'], name: 'legacy games' },
                            { keywords: ['microsoft store'], name: 'microsoft store' },
                            { keywords: ['xbox'], name: 'xbox' },
                            { keywords: ['battle.net', 'blizzard'], name: 'battle.net' },
                            { keywords: ['ubisoft connect', 'uplay', 'ubisoft'], name: 'ubisoft' },
                            { keywords: ['ea app', 'origin'], name: 'ea app' },
                            { keywords: ['steam'], name: 'steam' },
                            { keywords: ['amazon games', 'amazon luna'], name: 'amazon' },
                        ];
                        
                        for (const store of knownStores) {
                            if (store.keywords.some(kw => allText.includes(kw))) {
                                return store.name;
                            }
                        }
                        
                        const match1 = allText.match(/redeem your (?:product )?code on\s+([^.\n]+)/i);
                        if (match1 && match1[1]) return match1[1].trim();
                        
                        return 'unknown';
                    })()
                    """
                )
                store = str(store_raw).lower().strip() if isinstance(store_raw, str) else 'unknown'
            
            logger.info("External game '%s' on store: %s", title, store)

            # Fast bail-out for account-linked platforms (Epic, Amazon, Luna) – no code to extract
            store_lower = str(store).lower()
            if any(kw in store_lower for kw in ('epic', 'amazon', 'luna')):
                logger.info("Skipping '%s' – platform '%s' uses account linking, no code.", title, store)
                # Save to DB so we don't revisit
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="prime", user=self.user or "unknown",
                        game_id=title, title=title, url=url, status="claimed",
                    )
                    obj.extra = json.dumps({"external_store": store})
                    await session.commit()
                self.notify_games.append({"title": title, "url": url, "status": f"skipped ({store})"})
                return

            # Try to get the redemption code
            code = await self.page.evaluate(
                """
                (() => {
                    // Legacy input fields
                    const input = document.querySelector('input[type="text"]');
                    if (input && input.value && input.value.length > 5) return input.value;
                    
                    // Old data-a-target
                    const codeEl = document.querySelector('[data-a-target="ClaimStateClaimCodeContent"]');
                    if (codeEl) return codeEl.textContent.replace('Your code: ', '').trim();
                    
                    // New layout (April 2026): Find the "Claim Code" button, the code is right next to it!
                    const btns = Array.from(document.querySelectorAll('button, a'));
                    const claimBtn = btns.find(b => b.textContent.includes('Claim Code') || b.textContent.includes('Copy'));
                    if (claimBtn && claimBtn.previousElementSibling) {
                        const txt = claimBtn.previousElementSibling.textContent.trim();
                        if (txt && txt.length > 5 && !txt.includes(' ')) return txt;
                    }
                    
                    // Fallback: search for any div containing typical uppercase code structure
                    const allDivs = document.querySelectorAll('div');
                    for (const div of allDivs) {
                        const txt = div.textContent.trim();
                        if (/^[A-Z0-9-]{12,30}$/.test(txt)) return txt; // e.g. GOG / Xbox typical code formats
                    }
                    
                    return null;
                })()
                """
            )

            if not code:
                # Check if account linking is needed (Epic, Origin, etc.)
                link_needed = await self.page.evaluate(
                    """
                    (() => {
                        const txt = document.body.textContent.toLowerCase();
                        if (txt.includes('link game account') || txt.includes('link account')) return true;
                        
                        // Check if any button has "Link " text (like "Link Epic Games account")
                        const btns = Array.from(document.querySelectorAll('button, a'));
                        if (btns.some(b => b.textContent.toLowerCase().includes('link ') && b.textContent.toLowerCase().includes('account'))) return true;
                        
                        return false;
                    })()
                    """
                )
                if link_needed:
                    logger.warning("'%s' requires account linking for %s – skipping.", title, store)
                    self.notify_games.append({"title": title, "url": url, "status": f"needs linking ({store})"})
                else:
                    logger.warning("No code found for '%s'.", title)
                    self.notify_games.append({"title": title, "url": url, "status": "claimed (no code)"})

                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="prime", user=self.user or "unknown",
                        game_id=title, title=title, url=url, status="claimed",
                    )
                    await session.commit()
                return

            logger.info("Code for '%s': %s", title, code)

            # Save to DB
            async with async_session() as session:
                obj, _ = await get_or_create(
                    session, store="prime", user=self.user or "unknown",
                    game_id=title, title=title, url=url, status="claimed", code=code,
                )
                obj.code = code
                obj.extra = json.dumps({"external_store": store})
                await session.commit()

            _save_to_json(title, code=code, store=store, url=url, status="claimed")

            # External or GOG code: log and save
            redeem_urls = {
                "gog": "https://www.gog.com/redeem",
                "microsoft store": "https://account.microsoft.com/billing/redeem",
                "xbox": "https://account.microsoft.com/billing/redeem",
                "legacy games": "https://www.legacygames.com/primedeal",
            }
            redeem_url = redeem_urls.get(store, "")
            if redeem_url:
                logger.info("Redeem '%s' at: %s (code: %s)", title, redeem_url, code)
            else:
                logger.info("Redeem '%s' manually on %s (code: %s)", title, store, code)

            # For GOG codes, we'll let GOGClaimer redeem them retroactively using the correct profile!
            if "gog" in str(store).lower():
                self.notify_games.append({"title": title, "url": f"https://www.gog.com/redeem/{code}", "status": f"code: {code} (GOG, pending auto-redeem)"})
            else:
                self.notify_games.append({"title": title, "url": url, "status": f"code: {code} ({store})"})

            await self.take_screenshot(f"prime_{filenamify(title)}")

        except Exception:
            logger.exception("Failed to claim external '%s'", title)
            self.notify_games.append({"title": title, "url": url, "status": "failed"})
        finally:
            # Navigate back to claims page for next game
            await self.page.get(URL_CLAIM)
            await self.sleep(3)
            game_tab = await self.page.find('button[data-type="Game"]', timeout=5)
            if game_tab:
                await game_tab.click()
                await self.sleep(2)



async def claim_prime() -> dict:
    """Convenience entry point."""
    claimer = PrimeGamingClaimer()
    await claimer.run()
    return {"store": "Prime Gaming", "user": claimer.user, "games": claimer.notify_games}
