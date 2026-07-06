"""AliExpress store module – automated authentication and daily check-in coin collection."""

from __future__ import annotations

import asyncio
import json
import logging

import nodriver as uc

from src.core.claimer import BaseClaimer
from src.core.config import cfg
from src.core.notifier import notify

logger = logging.getLogger("fgc.aliexpress")

URL_LOGIN = "https://www.aliexpress.com/p/ug-login-page/login.html?fromMsite=true"
URL_COINS = "https://m.aliexpress.com/p/coin-index/index.html"
URL_HOME = "https://www.aliexpress.com/"


class AliExpressClaimer(BaseClaimer):
    store_name = "aliexpress"

    async def run(self) -> None:
        """Main entry point for the AliExpress daily check-in flow."""
        logger.debug("Starting AliExpress daily check-in flow")
        try:
            # Step 1: Launch browser with mobile user agent, as required by AliExpress mobile pages
            # Window size is determined by cfg.width and cfg.height from the .env file in BaseClaimer.start_browser()
            mobile_ua = (
                "Mozilla/5.0 (Linux; Android 13; SM-S918B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.6099.199 Mobile Safari/537.36"
            )
            await self.start_browser(extra_args=[
                f"--user-agent={mobile_ua}",
            ])

            # Enable CDP mobile device metrics override (fitting within actual window height)
            self.logger.debug("Enabling CDP mobile device metrics emulation...")
            try:
                # Ensure viewport height fits inside physical window height from .env (cfg.height)
                # and use scale_factor=1.0 without touch emulation so VNC mouse scrolling works
                # and bottom modals/drawers are not cut off below the screen!
                viewport_height = min(800, cfg.height - 40) if cfg.height > 100 else 680
                await self.page.send(uc.cdp.emulation.set_device_metrics_override(
                    width=450,
                    height=viewport_height,
                    device_scale_factor=1.0,
                    mobile=True
                ))
                await self.page.send(uc.cdp.emulation.set_user_agent_override(
                    user_agent=mobile_ua,
                    accept_language="en-US,en;q=0.9",
                    platform="Android"
                ))
            except Exception as e:
                self.logger.debug("CDP emulation override exception: %s", e)

            # Block external custom app protocol requests (e.g. aliexpress:// or intent://)
            # which cause Chromium to pop up 'Open xdg-open?' system dialogs on desktop Linux/VNC!
            try:
                await self.page.send(uc.cdp.network.enable())
                await self.page.send(uc.cdp.network.set_blocked_urls(urls=[
                    "*aliexpress://*",
                    "*intent://*",
                    "*market://*",
                    "*android-app://*",
                    "*alipay://*",
                    "*taobao://*",
                    "aliexpress:*",
                    "intent:*",
                    "market:*",
                    "android-app:*",
                    "alipay:*",
                    "taobao:*"
                ]))
            except Exception as e:
                self.logger.debug("CDP set_blocked_urls exception: %s", e)

            # Inject mobile-specific stealth override and prevent custom protocol app launches
            mobile_stealth_js = """
                Object.defineProperty(navigator, 'plugins', { get: () => [] });
                Object.defineProperty(navigator, 'platform', { get: () => 'Linux armv8l' });
                Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 });
                
                const isAppUrl = (url) => {
                    if (!url) return false;
                    const u = String(url).toLowerCase().trim();
                    return u.startsWith('aliexpress:') || u.startsWith('intent:') || u.startsWith('market:') || u.startsWith('android-app:') || u.startsWith('alipay:') || u.startsWith('taobao:') || u.includes('xdg-open');
                };
                
                const origOpen = window.open;
                window.open = function(url, ...args) {
                    if (isAppUrl(url)) return null;
                    return origOpen.apply(this, [url, ...args]);
                };

                window.addEventListener('click', function(e) {
                    const target = e.target && e.target.closest ? e.target.closest('a') : null;
                    if (target && target.href && isAppUrl(target.href)) {
                        e.preventDefault();
                        e.stopPropagation();
                    }
                }, true);

                const origAssign = window.location.assign;
                window.location.assign = function(url) {
                    if (isAppUrl(url)) return;
                    return origAssign.apply(this, arguments);
                };
                const origReplace = window.location.replace;
                window.location.replace = function(url) {
                    if (isAppUrl(url)) return;
                    return origReplace.apply(this, arguments);
                };
            """
            try:
                await self.page.send(
                    uc.cdp.page.add_script_to_evaluate_on_new_document(
                        source=mobile_stealth_js,
                    )
                )
                await self.page.evaluate(mobile_stealth_js)
            except Exception as e:
                self.logger.debug("Mobile stealth JS injection exception: %s", e)

            # Step 2: Ensure we are logged in FIRST (starting from login link to avoid unauthenticated coins skeleton freeze)
            if not await self._ensure_logged_in():
                logger.error("Aborting AliExpress flow due to login failure.")
                return

            # Step 3: Navigate to the AliExpress coins mobile page for daily check-in
            logger.info("Navigating to mobile coins check-in page...")
            await self.page.get(URL_COINS)
            await self.sleep(6)

            # Step 4: Verify and report daily check-in status
            await self._verify_check_in()

        except Exception as exc:
            logger.exception("Fatal error during AliExpress check-in flow")
            if cfg.notify_errors:
                await notify(f"aliexpress failed: {exc}")
        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Login & Authentication
    # ------------------------------------------------------------------

    async def _is_logged_in(self) -> bool:
        """Check if the user is logged in (no longer on login page / shows authenticated elements)."""
        try:
            res = await self.page.evaluate("""
                (() => {
                    const url = window.location.href.toLowerCase();
                    const text = (document.body ? (document.body.textContent || '') : '').toLowerCase();
                    
                    // If we are still on any URL containing login, we are not logged in yet
                    if (url.includes('/login') || url.includes('login.html') || url.includes('ug-login-page')) {
                        return false;
                    }
                    
                    // If we see specific authenticated coin index indicators
                    if (text.includes('day streak') || text.includes('seria') || text.includes('coins tomorrow') || text.includes('check-in coins') || text.includes('monety za zameldowanie') || text.includes('moje monety')) {
                        return true;
                    }
                    
                    // Check if any button or link has login text
                    const loginEls = [...document.querySelectorAll('button, a, span')].filter(el => {
                        const t = (el.textContent || '').trim().toLowerCase();
                        return (t === 'log in' || t === 'zaloguj' || t === 'zaloguj się' || t === 'sign in') && el.offsetParent !== null;
                    });
                    if (loginEls.length > 0) {
                        return false;
                    }
                    
                    // Otherwise if we left login.html and are on aliexpress domain, consider logged in
                    return url.includes('aliexpress.com') && !url.includes('/login');
                })()
            """)
            return bool(res)
        except Exception as e:
            self.logger.debug("Error checking login state: %s", e)
            return False

    async def _ensure_logged_in(self) -> bool:
        """Verify login status via direct login link, attempt automated login, or fall back to VNC for OTP code."""
        await self.sleep(2)

        self.logger.info("Opening direct login link to check/perform authentication...")
        await self.page.get(URL_LOGIN)
        await self.sleep(4)

        # 1. If already logged in, AliExpress automatically redirects away from login.html
        if await self._is_logged_in():
            self.logger.info("Session verified: already logged in (redirected from login page)!")
            self.log_signed_in(cfg.ae_email or "AliExpress User")
            return True

        self.logger.info("On login page. Proceeding with authentication...")

        # Dismiss cookies if prompt exists (using native click)
        try:
            cookie_btn = await self.page.find("Accept cookies", timeout=2)
            if not cookie_btn:
                cookie_btn = await self.page.find("Akceptuj", timeout=1)
            if cookie_btn:
                await cookie_btn.click()
                await self.sleep(1)
        except Exception:
            pass

        # Handle 'Switch account' if present (similar to aliexpress.js line 57)
        try:
            switch_btn = await self.page.find("Switch account", timeout=2)
            if not switch_btn:
                switch_btn = await self.page.find("Przełącz konto", timeout=1)
            if switch_btn:
                await switch_btn.click()
                await self.sleep(2)
        except Exception:
            pass

        # 2. Automated login if credentials are configured
        if cfg.ae_email and cfg.ae_password:
            self.logger.info("Attempting automated AliExpress login...")
            try:
                # First check if a password field is already present (AliExpress remembered the account!)
                pass_el = None
                try:
                    pass_el = await self.page.select('input[type="password"]', timeout=2)
                except Exception:
                    pass

                if not pass_el:
                    # Fresh login screen: find and enter email first
                    email_el = None
                    for sel in ['input[label="Email or phone number"]', 'input[placeholder*="Email"]', 'input[type="email"]', 'input[name*="email"]']:
                        try:
                            email_el = await self.page.select(sel, timeout=1.5)
                            if email_el:
                                break
                        except Exception:
                            pass

                    if email_el:
                        self.logger.info("Email input found. Entering email...")
                        await email_el.click()
                        await self.sleep(0.5)
                        await email_el.send_keys(cfg.ae_email)
                        await self.sleep(0.5)

                        # Blur active element so Continue button enables
                        await self.page.evaluate("if (document.activeElement && document.activeElement.blur) document.activeElement.blur();")
                        await self.sleep(1)

                        cont_btn = await self.page.find("Continue", timeout=3)
                        if not cont_btn:
                            cont_btn = await self.page.find("Kontynuuj", timeout=2)
                        if cont_btn:
                            self.logger.info("Clicking Continue button...")
                            await cont_btn.click()
                        await self.sleep(4)

                    # Now look for password input after Continue
                    for psel in ['#fm-login-password', 'input[type="password"]', 'input[label="Password"]', 'input[placeholder*="Password"]', 'input[name*="password"]']:
                        try:
                            pass_el = await self.page.select(psel, timeout=2)
                            if pass_el:
                                break
                        except Exception:
                            pass
                else:
                    self.logger.info("ℹ️ AliExpress remembered account! (Password input available directly without entering email)")

                if pass_el:
                    self.logger.info("Entering password...")
                    await pass_el.click()
                    await self.sleep(0.5)
                    await pass_el.send_keys(cfg.ae_password)
                    await self.sleep(0.5)

                    # Blur active element
                    await self.page.evaluate("if (document.activeElement && document.activeElement.blur) document.activeElement.blur();")
                    await self.sleep(1)

                    # Click Sign in button with native click ONLY
                    sign_btn = await self.page.find("Sign in", timeout=3)
                    if not sign_btn:
                        sign_btn = await self.page.find("Zaloguj", timeout=2)
                    if not sign_btn:
                        sign_btn = await self.page.find("Log in", timeout=2)
                    if sign_btn:
                        self.logger.info("Clicking Sign in button...")
                        await sign_btn.click()
                    await self.sleep(6)
            except Exception as e:
                self.logger.debug("Automated login steps encountered an exception: %s", e)

            if await self._is_logged_in():
                self.log_signed_in(cfg.ae_email or "AliExpress User")
                return True

        # 3. Fallback to VNC manual login if 6-digit verification code or CAPTCHA is required
        self.logger.warning("⚠️ Verification required (e.g., 6-digit email verification code or CAPTCHA)!")
        
        custom_msg = (
            f"🔐 **{self.store_name} Verification Required!**\n\n"
            f"Please enter the 6-digit verification code from your email or complete manual login in the browser.\n"
            f"🌐 **VNC Login URL:** http://{cfg.vnc_ip}:{cfg.novnc_port}\n"
            f"⏱️ **Timeout:** Waiting for {cfg.vnc_login_timeout} seconds..."
        )
        if await self._wait_for_vnc_login(self._is_logged_in, custom_msg=custom_msg):
            self.log_signed_in(cfg.ae_email or "AliExpress User")
            return True

        self.logger.error("Timed out waiting for AliExpress login.")
        return False

    # ------------------------------------------------------------------
    # Check-in Verification
    # ------------------------------------------------------------------

    async def _verify_check_in(self) -> None:
        """Verify navigation to the coin index page, dismiss promo overlays, click 'Collect', and report streak status."""
        current_url = await self.page.evaluate("window.location.href")
        if "/p/coin-index/" not in str(current_url):
            self.logger.info("Navigating to coins page to trigger daily check-in...")
            await self.page.get(URL_COINS)
            await self.sleep(5)

        # Dismiss double-coin or promotional modals if present (.hideDoubleButton)
        try:
            hide_btn = await self.page.select('.hideDoubleButton', timeout=1.5)
            if hide_btn:
                self.logger.info("🧹 Dismissing double-coin / promotional overlay button...")
                await hide_btn.click()
                await self.sleep(1)
        except Exception:
            pass

        if cfg.dryrun:
            self.logger.info("DRYRUN – skipped AliExpress coin check-in.")
            self.notify_games.append({
                "title": "AliExpress Daily Check-in",
                "url": URL_COINS,
                "status": "available (dry run)"
            })
            return

        action_status = "checked in / active"
        # Check for and click 'Collect' / 'Odbierz' button to claim daily check-in coins!
        self.logger.info("Checking for daily coin collection button...")
        try:
            collect_btn = None
            for btn_text in ["Collect", "Odbierz", "Check in", "Zamelduj się", "Claim", "Earn more coins", "Zdobądź więcej"]:
                collect_btn = await self.page.find(btn_text, timeout=1.5)
                if collect_btn:
                    if btn_text in ["Earn more coins", "Zdobądź więcej"]:
                        self.logger.info("✨ Daily check-in coins already claimed today! ('%s' button detected - no action needed)", btn_text)
                        action_status = "already claimed today ✨"
                    else:
                        self.logger.info("🎯 Found check-in button '%s'! Clicking to collect daily coins...", btn_text)
                        await collect_btn.click()
                        await self.sleep(3)
                        action_status = "claimed today 🪙"
                    break
        except Exception as e:
            self.logger.debug("Exception while looking for Collect button: %s", e)

        # JS fallback for Collect click if native find missed stylized buttons
        try:
            clicked_js = await self.page.evaluate("""
                (() => {
                    const btns = [...document.querySelectorAll('button, div[role="button"], span, a')];
                    for (const b of btns) {
                        const t = (b.textContent || '').trim().toLowerCase();
                        if ((t === 'collect' || t === 'odbierz' || t === 'claim' || t === 'check in' || t === 'odbierz monety') && b.offsetParent !== null) {
                            b.click();
                            return t;
                        }
                    }
                    return null;
                })()
            """)
            if clicked_js:
                self.logger.info("🎯 Clicked check-in button via JavaScript fallback ('%s')", clicked_js)
                action_status = "claimed today via JS 🪙"
                await self.sleep(3)
        except Exception:
            pass

        # Extract streak / coins info from page for logging and notifications
        info = await self.page.evaluate(r"""
            (() => {
                const text = document.body ? (document.body.textContent || '') : '';
                let streak = 'active';
                let coins = '';
                
                // Try matching day streak number e.g. "3 day streak"
                const streakMatch = text.match(/(\d+)\s*day streak/i) || text.match(/seria\s*(\d+)\s*dni/i);
                if (streakMatch) {
                    streak = streakMatch[1] + ' days';
                }
                
                // Try matching total coins or coins tomorrow
                const tomorrowMatch = text.match(/Get\s*(\d+)\s*check-in coins tomorrow/i) || text.match(/Jutro\s*(\d+)\s*monet/i);
                const tomorrow = tomorrowMatch ? tomorrowMatch[1] : null;
                
                return { streak: streak, tomorrow: tomorrow };
            })()
        """)

        streak_str = info.get("streak", "active") if isinstance(info, dict) else "active"
        tomorrow_str = f" (+{info['tomorrow']} tomorrow)" if isinstance(info, dict) and info.get("tomorrow") else ""

        status_msg = f"{action_status} (Streak: {streak_str}){tomorrow_str}"
        self.logger.info("✅ AliExpress daily check-in confirmed! %s", status_msg)

        self.notify_games.append({
            "title": "AliExpress Daily Check-in",
            "url": URL_COINS,
            "status": status_msg
        })

        # Keep browser open on the coin check-in page for visual verification in VNC
        self.logger.info("⏳ Keeping coin check-in page open for 4 seconds for visual verification in VNC...")
        await self.sleep(4)


async def claim_aliexpress() -> dict:
    """Convenience entry point for AliExpress daily check-in."""
    claimer = AliExpressClaimer()
    await claimer.run()
    return {"store": "AliExpress", "user": claimer.user, "games": claimer.notify_games}
