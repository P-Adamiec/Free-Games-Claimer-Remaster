"""Epic Games Store module – claims the weekly free games from epicgames.com."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx
import nodriver as uc
import pyotp
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.claimer import BaseClaimer, now_str
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.epic")

# URL of Epic's free games page (where we look for available free games)
URL_CLAIM = "https://store.epicgames.com/en-US/free-games"

# Login page URL — includes a redirect back to the free games page after login
URL_LOGIN = (
    "https://www.epicgames.com/id/login?lang=en-US"
    "&noHostRedirect=true&redirectUrl=" + URL_CLAIM
)


class EpicGamesClaimer(BaseClaimer):
    store_name = "epic"

    async def run(self) -> None:
        """Main entry point: detect free games and claim them."""
        logger.debug("Starting Epic Games claiming flow")
        try:
            # Epic REQUIRES a visible browser window (headful mode).
            # Running headless triggers captcha challenges from their anti-bot system.
            # GPU flags are needed so that WebGL reports a real GPU, not a software renderer.
            await self.start_browser(
                force_headful=True,
                extra_args=[
                    "--ignore-gpu-blocklist",   # Force GPU acceleration even if blocked
                    "--enable-unsafe-webgpu",    # Enable WebGPU hardware acceleration
                ],
            )
            # Set cookies to bypass age gates and cookie consent popups
            await self._set_cookies()
            await self.page.get(URL_CLAIM)
            await self.sleep(3)

            # Step 1: Make sure we are logged in
            await self._ensure_logged_in()

            # Step 2: Find which games are currently free
            free_games = await self._detect_free_games()
            if not free_games:
                logger.info("No free games found to claim.")
                return
                
            links = []
            for game in free_games:
                url = game["url"]
                title = game["title"]
                
                # Clean up title if API missed it (DOM fallback)
                if title == "Unknown":
                    game_id = url.rstrip('/').split('/')[-1]
                    title = game_id.replace('-', ' ')
                    title = re.sub(r' [0-9a-fA-F]{6}$', '', title)
                    title = title.title()
                    game["title"] = title
                
                links.append(f"  • [bold cyan]{game['title']}[/bold cyan] 🔗 {url}")

            if free_games:
                logger.info("🎮 [bold magenta]Found %d free game(s) to claim:[/bold magenta]\n%s", len(free_games), "\n".join(links))

            # --- Claim each game ---
            for game in free_games:
                await self._claim_game(game["url"])

        except Exception as exc:
            logger.exception("Fatal error")
            if cfg.notify_errors:
                await self.notify(f"epic-games failed: {exc}")
        finally:
            # We DO NOT notify individually here anymore - we just return it to the orchestrator
            # (Exception: We still keep error notifications inside logic if needed, but summary is deferred)
            logger.info("Epic Games claimer finished.")

            # Always close the browser when done
            await self.close_browser()

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    async def _set_cookies(self) -> None:
        """Pre-set cookies to skip the cookie consent popup and age verification dialogs."""
        if self.page:
            await self.page.evaluate(
                """
                // Cookie consent: pretend we already accepted cookies 5 days ago
                document.cookie = "OptanonAlertBoxClosed=" + new Date(Date.now() - 5*24*60*60*1000).toISOString() + "; domain=.epicgames.com; path=/";
                // Age gate: set all age ratings to max so no "are you 18+?" popup appears
                document.cookie = "HasAcceptedAgeGates=USK:9007199254740991,general:18,EPIC SUGGESTED RATING:18; domain=store.epicgames.com; path=/";
                """
            )

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _ensure_logged_in(self) -> None:
        """Check if we're logged into Epic. If not, try automatic login or VNC fallback."""
        
        # Navigate to Epic Store frontend to initialize cookies/session natively
        await self.page.get("https://store.epicgames.com/")
        await self.sleep(5)  # Give the SPA + web components time to hydrate

        # Cloudflare can gate the store BEFORE login (looks like a failed "attempt 1/3" loop) — detect it and hand off to VNC.
        if await self._human_challenge_present():
            if not await self._wait_out_challenge("Epic Games"):
                logger.warning("Epic security challenge not cleared in time – skipping.")
                return
            await self.page.get("https://store.epicgames.com/")
            await self.sleep(4)

        async def _is_logged_in() -> bool:
            """Check login via multiple DOM signals (egs-navigation attribute can be unreliable)."""
            return await self.page.evaluate(
                """
                (() => {
                    // Signal 1: egs-navigation web component attribute (legacy, may not work)
                    const nav = document.querySelector('egs-navigation');
                    if (nav && nav.getAttribute('isloggedin') === 'true') return true;

                    // Signal 2: Wishlist / Cart links only visible when logged in
                    const links = document.querySelectorAll('a');
                    for (const a of links) {
                        const href = (a.getAttribute('href') || '').toLowerCase();
                        if (href.includes('/wishlist') || href.includes('/cart')) return true;
                    }

                    // Signal 3: User avatar icon present (logged-in navbar shows avatar, not "Sign In")
                    const signInBtn = document.querySelector('a[href*="/login"], button');
                    const allText = document.body?.innerText || '';
                    // If "Sign in" text is prominent in the nav area, we're NOT logged in
                    const navText = (nav?.shadowRoot?.innerHTML || nav?.innerHTML || '').toLowerCase();
                    if (navText.includes('sign in') || navText.includes('sign_in')) return false;

                    // Signal 4: Check for avatar element in the navigation
                    if (nav?.shadowRoot) {
                        const avatar = nav.shadowRoot.querySelector('img[alt*="avatar"], [class*="avatar"], [class*="user"]');
                        if (avatar) return true;
                    }

                    // Signal 5: Modern Epic redesign check — if on store.epicgames.com and Wishlist/Cart/Gifts text is visible without any Sign In link
                    const allLinks = [...document.querySelectorAll('a')];
                    const hasSignIn = allLinks.some(a => {
                        const href = (a.getAttribute('href') || '').toLowerCase();
                        const txt = (a.innerText || a.textContent || '').toLowerCase();
                        return (href.includes('/login') || href.includes('id.epicgames.com')) && (txt.includes('sign in') || txt.includes('zaloguj'));
                    });
                    if (!hasSignIn && window.location.hostname.includes('store.epicgames.com')) {
                        const bodyTxt = (document.body?.innerText || '').toLowerCase();
                        if (bodyTxt.includes('wishlist') || bodyTxt.includes('cart') || bodyTxt.includes('koszyk') || bodyTxt.includes('listy życzeń')) return true;
                    }

                    return false;
                })()
                """
            )

        async def _get_display_name() -> str:
            """Try to extract the display name from the navigation component."""
            return await self.page.evaluate(
                """
                (() => {
                    const nav = document.querySelector('egs-navigation');
                    if (nav) {
                        const name = nav.getAttribute('displayname');
                        if (name) return name;
                    }
                    return '';
                })()
                """
            ) or ""

        # Retry a few times — the web component loads asynchronously
        for _ in range(3):
            if await _is_logged_in():
                self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
                self.log_signed_in()
                return
            await self.sleep(2)

        # Read credentials from the .env file
        email, password = cfg.eg_email, cfg.eg_password
        
        # No credentials provided — let the user log in manually through VNC
        if not email or not password:
            logger.warning("EG_EMAIL missing. Proceeding to login page for manual VNC login...")
            await self._navigate_organically_to_login()
            logged_in = await self._wait_for_vnc_login(_is_logged_in)
            if not logged_in:
                logger.warning("VNC login timed out – skipping.")
                return
            
            self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
            self.log_signed_in()
            return

        # Automated stealth login loop
        challenge_blocked = False
        mfa_manual = False
        for attempt in range(3):
            logger.warning("Not signed in – attempting automated login (attempt %d/3)…", attempt + 1)

            if attempt > 0:
                await self.page.get("https://store.epicgames.com/")
                await self.sleep(3)

            await self._navigate_organically_to_login()
            await self.sleep(3)

            await self._do_stealth_login()

            otp_tried = False
            # Wait loop to detect auth completion or interstitial
            for wait_sec in range(120):
                try:
                    curr_url = str(await self.page.evaluate("window.location.href") or self.page.url)
                except Exception:
                    curr_url = str(self.page.url)

                # We know auth is complete when Epic redirects us back to the store domain.
                if url_has_allowed_host(curr_url, "store.epicgames.com"):
                    break

                # 2FA code screen: auto-fill TOTP if EG_OTPKEY is set, else stop and let the user type it via VNC.
                if await self._mfa_prompt_present():
                    if cfg.eg_otpkey and not otp_tried:
                        otp_tried = True
                        await self._fill_totp()
                        await self.sleep(2)
                    elif not cfg.eg_otpkey:
                        mfa_manual = True
                        break
                    await self.sleep(1)
                    continue

                if "login/review" in curr_url:
                    logger.info("Account review interstitial detected, auto-confirming...")
                    try:
                        clicked_yes = await self.page.evaluate('''
                            (() => {
                                const btns = [...document.querySelectorAll('button')];
                                const btn = btns.find(b => b.innerText && b.innerText.toLowerCase().includes('yes, continue'));
                                if (btn) { btn.click(); return true; }
                                return false;
                            })()
                        ''')
                        if clicked_yes:
                            logger.info("Auto-clicked 'Yes, continue'")
                            await self.sleep(3)
                    except Exception:
                        pass

                # Check for "Maybe later" 2FA setup interstitial directly via DOM
                try:
                    clicked_maybe = await self.page.evaluate('''
                        (() => {
                            const btns = [...document.querySelectorAll('button')];
                            const btn = btns.find(b => b.innerText && b.innerText.toLowerCase().includes('maybe later'));
                            if (btn) { btn.click(); return true; }
                            return false;
                        })()
                    ''')
                    if clicked_maybe:
                        logger.info("Auto-clicked 'Maybe later' on 2FA setup screen.")
                        await self.sleep(3)
                except Exception:
                    pass

                # Captcha/challenge can't be auto-solved — stop retrying and hand off to VNC below.
                if wait_sec >= 4 and await self._human_challenge_present():
                    logger.warning("Captcha / security challenge detected during Epic login.")
                    challenge_blocked = True
                    break

                await self.sleep(1)

            # On a manual-code screen, don't navigate away — hand off to VNC below.
            if mfa_manual:
                break

            # verify success
            await self.page.get(URL_CLAIM)
            await self.sleep(3)
            if await _is_logged_in():
                self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
                self.log_signed_in()
                return

            if challenge_blocked:
                break  # retrying won't clear a captcha – go straight to VNC help

        # Automated login failed (captcha/2FA/repeated) — notify and wait for the user to finish via VNC.
        if mfa_manual:
            reason = " (2FA code needed)"
        elif challenge_blocked:
            reason = " (captcha/challenge)"
        else:
            reason = " after 3 attempts"
        logger.warning("Automated Epic login failed%s – requesting manual login via VNC.", reason)
        if mfa_manual:
            custom_msg = self._vnc_notice(
                "Epic Games — 2FA code needed",
                "Enter the 6-digit code Epic sent to your email or phone (or from your authenticator). "
                "If a 'One more step' security check appears, complete it too.",
            )
        else:
            custom_msg = self._vnc_notice(
                "Epic Games — login needs you",
                "A captcha or security challenge is blocking automated sign-in. Open the browser, solve it and finish logging in.",
            )
        if await self._wait_for_vnc_login(_is_logged_in, custom_msg=custom_msg):
            self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
            self.log_signed_in()
            return

        logger.warning("Epic login still not completed after VNC wait – skipping.")

    async def _mfa_prompt_present(self) -> bool:
        """True on Epic's 2FA code screen or method picker (email/SMS/authenticator)."""
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
        """Auto-enter the authenticator (TOTP) code from EG_OTPKEY, then submit."""
        if not cfg.eg_otpkey:
            return False
        try:
            otp_input = await self.page.find('input[name="code-input-0"]', timeout=5)
            if not otp_input:
                return False
            otp_code = pyotp.TOTP(cfg.eg_otpkey).now()
            logger.debug("Entering MFA (TOTP) code")
            await otp_input.clear_input()
            await self.sleep(0.5)
            await otp_input.send_keys(otp_code)
            await self.sleep(1)
            submit = await self.page.find('button[type="submit"]', timeout=5)
            if submit:
                await submit.click()
                await self.sleep(3)
            return True
        except Exception:
            return False

    async def _navigate_organically_to_login(self) -> None:
        """Navigates to the login page mimicking a click from the store, preserving Referer headers."""
        try:
            # Emulate clicking the "Sign In" link by setting location.href inside the existing page context
            await self.page.evaluate(f"window.location.href = '{URL_LOGIN}'")
        except Exception:
            await self.page.get(URL_LOGIN)

    async def _do_stealth_login(self) -> None:
        """Fill in email and password using browser-native methods.
        
        Uses real keyboard input (CDP events) instead of JavaScript injection,
        which makes the login look more human-like to anti-bot systems.
        """
        email = cfg.eg_email.strip() if cfg.eg_email else ""
        password = cfg.eg_password.strip() if cfg.eg_password else ""

        email_input = await self.page.find("#email", timeout=10)
        if email_input:
            # Click FIRST to trigger Chrome's internal credential manager autofill, then wait for it
            await email_input.click()
            await self.sleep(1.5)
            
            # Check if autofill already did our job flawlessly
            current_val = await self.page.evaluate('document.querySelector("#email")?.value')
            if not current_val or current_val.lower() != email.lower():
                logger.debug("Email autofill missing or incorrect. Typing manually...")
                if current_val:
                    await email_input.clear_input()
                    await self.sleep(0.5)
                await email_input.click()
                await email_input.send_keys(email)
                await self.sleep(0.5)
            else:
                logger.debug("Email autofill succeeded.")

            continue_btn = await self.page.find("#continue", timeout=5)
            if continue_btn:
                await continue_btn.click()
                logger.debug("Clicked continue, waiting for CSS slide animation...")
                await self.sleep(3.0)  # Wait for CSS slide transition completely

        password_input = await self.page.find("#password", timeout=10)
        if password_input:
            await password_input.click()
            await self.sleep(1.0)
            
            current_val = await self.page.evaluate('document.querySelector("#password")?.value')
            if not current_val or current_val != password:
                logger.debug("Password autofill missing or incorrect. Typing manually...")
                if current_val:
                    await password_input.clear_input()
                    await self.sleep(0.5)
                await password_input.click()
                await password_input.send_keys(password)
                await self.sleep(0.5)
            else:
                logger.debug("Password autofill succeeded.")

        # Check 'Remember Me' natively BEFORE submitting
        try:
            is_checked = await self.page.evaluate('document.querySelector("#rememberMe")?.checked')
            if not is_checked:
                remember_label = await self.page.find("label[for='rememberMe']", timeout=2)
                if remember_label:
                    await remember_label.click()
                    await self.sleep(0.5)
        except Exception:
            pass
            
        sign_in_btn = await self.page.find("#sign-in", timeout=5)
        if sign_in_btn:
            await sign_in_btn.click()
            await self.sleep(3)

        # Authenticator (TOTP) auto-fill; email/SMS codes are handled in the loop.
        if cfg.eg_otpkey:
            await self.sleep(3)
            await self._fill_totp()

    # ------------------------------------------------------------------
    # Detect free games
    # ------------------------------------------------------------------

    _PROMO_API = (
        "https://store-site-backend-static.ak.epicgames.com"
        "/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
    )

    async def _detect_free_games(self) -> list[dict]:
        """Find currently free games — tries the API first, falls back to page scraping."""
        games = await self._detect_free_games_api()
        if games:
            return games
        logger.warning("API detection returned 0 games, falling back to DOM scraping.")
        return await self._detect_free_games_dom()

    async def _detect_free_games_api(self) -> list[dict]:
        """Query Epic's public API to find which games are currently 100% off (free).

        This is the most reliable method because it doesn't depend on the page layout.
        The API returns a list of all promotional offers, and we filter for ones that
        are active right now with a 100% discount (discountPercentage == 0).
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self._PROMO_API)
                resp.raise_for_status()
                data = resp.json()

            elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
            now = datetime.now(timezone.utc)
            free_games: list[dict] = []

            for el in elements:
                # Must have active promotionalOffers (not just upcoming)
                promos = el.get("promotions")
                if not promos:
                    continue
                offers = promos.get("promotionalOffers", [])
                if not offers:
                    continue

                # Check each promotional offer for an active 100%-off (free) deal
                is_free_now = False
                for group in offers:
                    for offer in group.get("promotionalOffers", []):
                        discount = offer.get("discountSetting", {}).get("discountPercentage")
                        start = offer.get("startDate", "")
                        end = offer.get("endDate", "")
                        if discount is not None and discount == 0:
                            # discountPercentage == 0 means 100% off (free)
                            try:
                                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                                if start_dt <= now <= end_dt:
                                    is_free_now = True
                            except (ValueError, TypeError):
                                # If dates are unparseable, trust the discount
                                is_free_now = True

                if not is_free_now:
                    continue

                # Build the store URL from available slug fields
                url = self._build_game_url(el)
                if url and not any(g["url"] == url for g in free_games):
                    title = el.get("title", "Unknown")
                    free_games.append({"url": url, "title": title})

            return free_games
        except Exception:
            logger.exception("Failed to fetch free games from API")
            return []

    @staticmethod
    def _build_game_url(element: dict) -> str | None:
        """Build a store URL from an API element's slug fields.

        Priority (matches original JS logic):
          1. offerMappings[0].pageSlug  (most specific)
          2. catalogNs.mappings[0].pageSlug
          3. productSlug
          4. urlSlug (last resort, sometimes incorrect)
        """
        base = "https://store.epicgames.com/en-US/p/"

        # 1. offerMappings
        offer_mappings = element.get("offerMappings") or []
        if offer_mappings:
            slug = offer_mappings[0].get("pageSlug")
            if slug:
                return base + slug

        # 2. catalogNs.mappings
        cat_mappings = (element.get("catalogNs") or {}).get("mappings") or []
        if cat_mappings:
            slug = cat_mappings[0].get("pageSlug")
            if slug:
                return base + slug

        # 3. productSlug
        product_slug = element.get("productSlug")
        if product_slug:
            return base + product_slug

        # 4. urlSlug (fallback)
        url_slug = element.get("urlSlug")
        if url_slug:
            return base + url_slug

        return None

    async def _detect_free_games_dom(self) -> list[dict]:
        """Fallback: scrape the free-games page DOM for free game links."""
        await self.sleep(3)
        raw = await self.page.evaluate(
            """
            (() => {
                const results = [];
                // Strategy 1: cards with "Free Now" status text
                document.querySelectorAll('a[href*="/p/"], a[href*="/bundles/"]').forEach(a => {
                    const text = a.textContent || '';
                    if (text.includes('Free Now') || text.includes('Free')) {
                        const href = a.getAttribute('href');
                        if (href && !results.includes(href)) results.push(href);
                    }
                });
                // Strategy 2: offer cards with free-game data attributes
                document.querySelectorAll('[data-testid="offer-card"]').forEach(card => {
                    const link = card.closest('a') || card.querySelector('a');
                    const price = card.textContent || '';
                    if (link && (price.includes('Free Now') || price.includes('Free'))) {
                        const href = link.getAttribute('href');
                        if (href && !results.includes(href)) results.push(href);
                    }
                });
                // Ensure full URLs
                return results.map(h => {
                    if (typeof h !== 'string') return null;
                    return h.startsWith('http') ? h : 'https://store.epicgames.com' + h;
                }).filter(Boolean);
            })()
            """
        )
        # Defensive: ensure we only have strings
        if not raw or not isinstance(raw, list):
            return []
        
        free_games = []
        for u in raw:
            if isinstance(u, str):
                free_games.append({"url": u, "title": "Unknown"})
        return free_games

    # ------------------------------------------------------------------
    # Claim a single game
    # ------------------------------------------------------------------

    # Retry up to 2 times with exponential backoff if claiming fails
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15), reraise=True)
    async def _claim_game(self, url: str) -> None:
        """Claim a single free game by navigating to its page and clicking through the purchase flow."""
        # Extract the game identifier from the URL (e.g. "fortnite" from ".../p/fortnite")
        game_id = url.rstrip("/").split("/")[-1]

        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="epic", user=self.user or "unknown",
                game_id=game_id, title=game_id, url=url, status="unknown",
            )
            if not created and obj.status == "claimed":
                logger.debug("Already claimed, skip: %s", game_id)
                return

            await self.page.get(url)
            await self.sleep(4)

            # ── Handle mature content / age gate ──
            await self._click_page_button_by_text("Continue", timeout=2, log="mature content gate")

            # ── Detect page state: find the primary action button ──
            # Epic has two checkout flows:
            #   NEW (2025+): Direct "Add to library" / "Get" button on checkout overlay
            #   OLD: "purchase-cta-button" data-testid + payment iframe
            btn_info = {"text": "", "flow": "unknown"}
            for _ in range(15):
                btn_raw = await self.page.evaluate(
                    """
                    JSON.stringify((() => {
                        // NEW flow: "Add to library" button (checkout overlay)
                        const allBtns = [...document.querySelectorAll('button')];
                        for (const btn of allBtns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t.includes('add to library')) return { text: t, flow: 'new_add' };
                        }

                        // Check for "Get" button FIRST (new flow, even if it has purchase-cta-button testid)
                        for (const btn of allBtns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t === 'get') return { text: t, flow: 'new_get' };
                        }

                        // OLD flow: purchase-cta-button (only if text is NOT "get")
                        const oldBtn = document.querySelector('button[data-testid="purchase-cta-button"]');
                        if (oldBtn) {
                            const t = oldBtn.textContent.trim().toLowerCase();
                            if (t && t !== 'loading' && t !== 'get') return { text: t, flow: 'old_cta' };
                        }

                        // Check for "In Library" / "Owned" status
                        for (const btn of allBtns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t.includes('in library') || t.includes('owned')) return { text: t, flow: 'owned' };
                        }

                        return { text: '', flow: 'unknown' };
                    })())
                    """
                )
                try:
                    btn_info = json.loads(btn_raw) if isinstance(btn_raw, str) else {"text": "", "flow": "unknown"}
                except (json.JSONDecodeError, TypeError):
                    btn_info = {"text": "", "flow": "unknown"}
                if btn_info.get("text"):
                    break
                await self.sleep(1)

            btn_text = btn_info.get("text", "")
            flow_type = btn_info.get("flow", "unknown")

            # ── Read title ──
            title = await self.page.evaluate(
                """
                (() => {
                    // Check for bundle first
                    const isBundlePage = [...document.querySelectorAll('span')]
                        .some(s => s.textContent === 'About Bundle');
                    if (isBundlePage) {
                        const buySpan = [...document.querySelectorAll('span')]
                            .find(s => s.textContent.startsWith('Buy '));
                        if (buySpan) return buySpan.textContent.replace('Buy ', '');
                    }
                    return document.querySelector('h1')?.textContent?.trim() || 'Unknown';
                })()
                """
            )
            obj.title = title

            notify_game = {"title": title, "url": url, "status": "failed"}
            self.notify_games.append(notify_game)

            if "in library" in btn_text or flow_type == "owned":
                logger.info("'%s' already in library.", title)
                obj.status = obj.status if obj.status == "claimed" else "existed"
                notify_game["status"] = "existed"
                await session.commit()
                return

            if "requires base game" in btn_text:
                logger.warning("'%s' requires base game.", title)
                obj.status = "failed:requires-base-game"
                notify_game["status"] = "requires base game"
                await session.commit()
                return

            if not btn_text:
                logger.warning("No purchase/claim button found for '%s'.", title)
                obj.status = "failed"
                await self.take_screenshot(f"epic_nobutton_{game_id}")
                await session.commit()
                return

            logger.info("Claiming '%s'... (flow: %s)", title, flow_type)

            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                await session.commit()
                return

            # ── NEW FLOW: "Add to library" or "Get" button (direct click, no iframe) ──
            if flow_type in ("new_add", "new_get"):
                claimed = await self._handle_new_checkout(title, flow_type)

            # ── OLD FLOW: purchase-cta-button + payment iframe ──
            elif flow_type == "old_cta":
                await self.page.evaluate(
                    """document.querySelector('button[data-testid="purchase-cta-button"]')?.click()"""
                )
                await self.sleep(2)

                # Handle intermediate dialogs
                await self._click_page_button_by_text("Continue", timeout=3, log="Device not supported")
                await self._click_page_button_by_text("Yes, buy now", timeout=1, log="already own partial")

                # Handle End User License Agreement
                await self.page.evaluate(
                    """
                    (() => {
                        const cb = document.querySelector('input#agree');
                        if (cb && !cb.checked) cb.click();
                        const btns = [...document.querySelectorAll('button')];
                        const accept = btns.find(b => b.textContent.includes('Accept'));
                        if (accept) accept.click();
                    })()
                    """
                )

                await self.sleep(3)
                claimed = await self._handle_purchase_iframe(title)
            else:
                logger.warning("Unknown checkout flow '%s' for '%s'.", flow_type, title)
                claimed = False

            if claimed:
                import datetime
                logger.info("✓ Claimed '%s' successfully!", title)
                obj.status = "claimed"
                obj.updated_at = datetime.datetime.now(datetime.timezone.utc)
                notify_game["status"] = "claimed"
            else:
                logger.error("Failed to claim '%s'.", title)
                obj.status = "failed"
                notify_game["status"] = "failed"
                await self.take_screenshot(f"epic_failed_{game_id}")
                if cfg.notify_claim_fails:
                    await self.notify(f"epic-games: failed to claim {title}")

            await session.commit()

    # ------------------------------------------------------------------
    # NEW checkout flow: "Add to library" (2025+)
    # ------------------------------------------------------------------

    async def _handle_new_checkout(self, title: str, flow_type: str) -> bool:
        """Handle Epic's new direct checkout flow (2025+).

        Two variants:
          new_add: "Add to library" is already visible → click it directly.
          new_get: "Get" button on product page → Continue dialog → checkout
                   overlay with "Add to library" → click it.

        Uses CDP Input.dispatchMouseEvent for all clicks to bypass React
        synthetic event handling.
        """
        try:
            # ── Step 1: Click the initial button ("Get" or "Add to library") ──
            initial_btn = "add to library" if flow_type == "new_add" else "get"
            clicked = await self._cdp_click_element_by_text(initial_btn, timeout=5)

            if not clicked:
                logger.warning("Could not find '%s' button for '%s'.", initial_btn, title)
                return False

            logger.info("Clicked '%s' button for '%s'.", initial_btn, title)
            if flow_type == "new_get":
                post_get_state = ""
                for _ in range(5):
                    post_get_state = await self.page.evaluate(
                        """
                        (() => {
                            const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
                            if (body.includes('thank you') || body.includes("it's all yours")
                                || body.includes('successfully')
                                || (body.includes('in library') && !body.includes('add it to your library'))) return 'success';
                            if (document.querySelector('#webPurchaseContainer iframe')) return 'purchase iframe';

                            const btns = [...document.querySelectorAll('button')];
                            for (const btn of btns) {
                                const t = (btn.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const r = btn.getBoundingClientRect();
                                if (btn.disabled || r.width <= 0 || r.height <= 0) continue;
                                if (t.includes('add to library')) return 'add to library';
                                if (t.includes('place order')) return 'place order';
                                if (t.includes('continue')) return 'continue';
                                if (t === 'get') return 'get';
                            }
                            return '';
                        })()
                        """
                    )
                    if post_get_state and post_get_state != "get":
                        break
                    await self.sleep(1)

                if post_get_state == "get":
                    logger.warning("After Get: Get still visible after click for '%s'. Taking screenshot.", title)
                    await self.take_screenshot(f"epic_get_still_visible_{title[:20]}")
                    return False
                if post_get_state:
                    logger.info("After Get: %s detected", post_get_state)
                else:
                    logger.info("After Get: no immediate state change detected")

            await self.sleep(3)

            # ── Step 2: Handle intermediate dialogs ──
            # "Device not supported" → Continue
            cont_clicked = await self._cdp_click_element_by_text("continue", timeout=5)
            if cont_clicked:
                logger.info("Clicked 'Continue' dialog. Waiting for checkout overlay...")
                # The checkout overlay takes several seconds to load after Continue
                await self.sleep(5)
            else:
                logger.debug("No 'Continue' dialog found, proceeding.")
                await self.sleep(2)

            # EULA / Terms acceptance (check before Add to library)
            await self.page.evaluate(
                """
                (() => {
                    const cb = document.querySelector('input#agree, input[type="checkbox"]');
                    if (cb && !cb.checked) cb.click();
                })()
                """
            )
            await self._cdp_click_element_by_text("accept", timeout=1)
            await self._cdp_click_element_by_text("i agree", timeout=1)

            # ── Step 3: If we clicked "Get", wait for checkout overlay ──
            if flow_type == "new_get":
                logger.debug("Looking for 'Add to library' or 'I accept' on checkout overlay...")
                add_clicked = False
                accepted = False
                
                for attempt in range(25):
                    # Check main page for Add to Library
                    without_cdp = await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button')];
                            const btn = btns.find(b => {
                                const t = (b.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                return t.includes('add to library');
                            });
                            return btn ? true : false;
                        })()
                    """)
                    
                    if without_cdp and not add_clicked:
                        logger.debug("Found 'Add to library' button. Clicking via CDP...")
                        await self.sleep(1)
                        add_clicked = await self._cdp_click_element_by_text("add to library", timeout=2)
                        await self.sleep(2)
                    
                    # Search for 'I accept' on main page
                    needs_accept = await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button')];
                            const btn = btns.find(b => {
                                const t = (b.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                return t.includes('i accept') || t.includes('i agree');
                            });
                            return btn ? true : false;
                        })()
                    """)
                    if needs_accept and not accepted:
                        logger.debug("Found 'I accept' (Right of Withdrawal) on main page. Clicking...")
                        accepted = await self._cdp_click_element_by_text("accept", timeout=2)
                        await self.sleep(2)

                    # Also check inside the iframe for 'I accept' or 'Place Order' (just in case)
                    frame_tree = await self.page.send(uc.cdp.page.get_frame_tree())
                    iframe_frame_id = self._find_purchase_frame(frame_tree)
                    
                    if iframe_frame_id:
                        ctx_id = await self.page.send(
                            uc.cdp.page.create_isolated_world(
                                frame_id=iframe_frame_id,
                                grant_univeral_access=True,
                            )
                        )
                        # Check "Add to library" inside iframe
                        if not add_clicked:
                            did_add = await self._eval_in_frame(ctx_id, """
                                (() => {
                                    const btns = [...document.querySelectorAll('button')];
                                    const btn = btns.find(b => {
                                        const t = (b.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                        return t.includes('add to library');
                                    });
                                    if (btn) { btn.click(); return true; }
                                    return false;
                                })()
                            """)
                            if did_add:
                                logger.debug("✓ Found and clicked 'Add to library' inside iframe.")
                                add_clicked = True
                                await self.sleep(2)

                        # Check "I accept" inside iframe
                        if not accepted:
                            did_accept = await self._eval_in_frame(ctx_id, """
                                (() => {
                                    const btns = [...document.querySelectorAll('button')];
                                    const btn = btns.find(b => {
                                        const t = (b.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                        return t.includes('i accept') || t.includes('i agree');
                                    });
                                    if (btn) { btn.click(); return true; }
                                    return false;
                                })()
                            """)
                            if did_accept:
                                logger.debug("✓ Found and clicked 'I accept' inside iframe.")
                                accepted = True
                                await self.sleep(2)

                    if add_clicked and accepted:
                        # Once we've clicked Add and explicitly handled Accept, we can wait for verification
                        # We don't break immediately, let already_done or the timeout push us forward
                        pass

                    # Also check if it was already confirmed (fallback checking main page)
                    already_done = await self.page.evaluate(
                        """
                        (() => {
                            const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
                            return body.includes('thank you') || body.includes('in library')
                                || body.includes('in your library') || body.includes('successfully');
                        })()
                        """
                    )
                    if already_done:
                        logger.info("Already confirmed without needing more clicks.")
                        return True

                    await self.sleep(2)

                if not add_clicked:
                    logger.warning("'Add to library' not found for '%s'. Taking screenshot.", title)
                    await self.take_screenshot(f"epic_no_addlib_{title[:20]}")
                    
                    # Last resort: try Continue in case another dialog appeared
                    await self._cdp_click_element_by_text("continue", timeout=3)
                    await self.sleep(3)

                await self.sleep(3)

            elif flow_type == "new_add":
                await self.sleep(3)

            # ── Step 4: Verify claim success ──
            for _ in range(15):
                success = await self.page.evaluate(
                    """
                    (() => {
                        function checkDoc(doc) {
                            try {
                                const body = (doc.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
                                if (body.includes('thank you') || body.includes('successfully')
                                    || (body.includes('in library') && !body.includes('add it to your library'))) return true;

                                const btns = [...doc.querySelectorAll('button')];
                                for (const btn of btns) {
                                    const t = (btn.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                    if (t === 'in library') return true;
                                }

                                const frames = [...doc.querySelectorAll('iframe')];
                                for (const f of frames) {
                                    try {
                                        if (f.contentDocument && checkDoc(f.contentDocument)) return true;
                                    } catch(e) {}
                                }
                            } catch(e) {}
                            return false;
                        }
                        return checkDoc(document);
                    })()
                    """
                )
                if success:
                    return True
                await self.sleep(1)

            logger.warning("No confirmation found after checkout for '%s'.", title)
            return False

        except Exception:
            logger.exception("Error in new checkout flow for '%s'.", title)
            return False

    async def _cdp_click_element_by_text(
        self, text: str, *, tag: str = "button", timeout: int = 1
    ) -> bool:
        """Click an element using CDP Input.dispatchMouseEvent (real mouse click).

        This is the most reliable click method — it sends actual browser-level
        mouse input at the element's pixel coordinates, bypassing all
        JavaScript event handling (including React synthetic events).

        Sends: mouseMoved → mousePressed → mouseReleased (like a real human).
        """
        import asyncio

        for attempt in range(max(1, timeout)):
            # Step 1: Find element recursively in main doc and all iframes
            coords_raw = await self.page.evaluate(
                """
                JSON.stringify((() => {
                    function findEl(doc, ox, oy) {
                        try {
                            const btns = [...doc.querySelectorAll('%s')];
                            const candidates = btns.filter(b => {
                                const t = (b.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const rect = b.getBoundingClientRect();
                                const style = (b.ownerDocument.defaultView || window).getComputedStyle(b);
                                return !b.disabled && rect.width > 0 && rect.height > 0
                                    && style.visibility !== 'hidden' && style.display !== 'none'
                                    && (t === '%s' || t.includes('%s'));
                            });
                            const btn = candidates.find(b => (b.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase() === '%s')
                                || candidates[0];
                            if (btn) {
                                btn.scrollIntoView({ block: 'center', behavior: 'instant' });
                                const rect = btn.getBoundingClientRect();
                                const left = Math.max(rect.left, 1);
                                const right = Math.min(rect.right, window.innerWidth - 1);
                                const topEdge = Math.max(rect.top, 1);
                                const bottom = Math.min(rect.bottom, window.innerHeight - 1);
                                if (right <= left || bottom <= topEdge) return null;
                                const x = left + (right - left) / 2;
                                const y = topEdge + (bottom - topEdge) / 2;
                                const top = doc.elementFromPoint(x, y);
                                if (!top || (!btn.contains(top) && top !== btn)) return null;
                                return {
                                    x: ox + x,
                                    y: oy + y,
                                    w: rect.width, h: rect.height
                                };
                            }
                            // Search iframes
                            const frames = [...doc.querySelectorAll('iframe')];
                            for (const f of frames) {
                                try {
                                    if (f.contentDocument) {
                                        const fRect = f.getBoundingClientRect();
                                        const res = findEl(f.contentDocument, ox + fRect.x, oy + fRect.y);
                                        if (res) return res;
                                    }
                                } catch(e) {}
                            }
                        } catch(e) {}
                        return null;
                    }
                    return findEl(document, 0, 0);
                })())
                """ % (tag, text, text, text)
            )

            try:
                coords = json.loads(coords_raw) if isinstance(coords_raw, str) else None
            except (json.JSONDecodeError, TypeError):
                coords = None

            if not coords:
                await self.sleep(1)
                continue

            x, y = coords["x"], coords["y"]
            logger.debug("Found '%s' at (%.0f, %.0f) size %.0fx%.0f", text, x, y, coords.get("w", 0), coords.get("h", 0))

            # Step 2: Send CDP mouse events (mouseMoved → mousePressed → mouseReleased)
            try:
                # First move the mouse to the target (required for proper event routing)
                await self.page.send(
                    uc.cdp.input_.dispatch_mouse_event(
                        type_="mouseMoved",
                        x=x,
                        y=y,
                    )
                )
                await asyncio.sleep(0.05)

                # Press
                await self.page.send(
                    uc.cdp.input_.dispatch_mouse_event(
                        type_="mousePressed",
                        x=x,
                        y=y,
                        button=uc.cdp.input_.MouseButton("left"),
                        click_count=1,
                    )
                )
                await asyncio.sleep(0.1)

                # Release
                await self.page.send(
                    uc.cdp.input_.dispatch_mouse_event(
                        type_="mouseReleased",
                        x=x,
                        y=y,
                        button=uc.cdp.input_.MouseButton("left"),
                        click_count=1,
                    )
                )
                logger.debug("CDP click OK at (%.0f, %.0f) for '%s'.", x, y, text)
                return True
            except Exception as exc:
                logger.warning("CDP click failed for '%s': %s", text, exc)
                await self.sleep(1)

        return False

    # ------------------------------------------------------------------
    # Purchase iframe handling (via CDP) — OLD flow
    # ------------------------------------------------------------------

    async def _handle_purchase_iframe(self, title: str) -> bool:
        """Complete the purchase inside Epic's payment iframe.

        Epic's checkout is inside a cross-origin iframe (payment-store.epicgames.com).
        Normal JavaScript can't reach inside cross-origin iframes, so we use CDP:
          1. Find the iframe's unique FrameId from the browser's frame tree
          2. Create an isolated JavaScript execution context inside that frame
          3. Run our button-clicking scripts inside that context
        """
        try:
            # Wait for the iframe to appear on the main page
            for attempt in range(10):
                has_iframe = await self.page.evaluate(
                    """!!document.querySelector('#webPurchaseContainer iframe')"""
                )
                if has_iframe:
                    break
                await self.sleep(1)
            else:
                logger.warning("No purchase iframe appeared for '%s'", title)
                return False

            await self.sleep(2)  # let iframe content load

            # Find the iframe's FrameId in the frame tree
            frame_tree = await self.page.send(uc.cdp.page.get_frame_tree())
            iframe_frame_id = self._find_purchase_frame(frame_tree)
            if not iframe_frame_id:
                logger.warning("Could not locate purchase frame in tree for '%s'", title)
                return False

            # Step 2: Create an isolated JavaScript context inside the iframe
            # This gives us the ability to run code inside the payment frame
            ctx_id = await self.page.send(
                uc.cdp.page.create_isolated_world(
                    frame_id=iframe_frame_id,
                    grant_univeral_access=True,
                )
            )
            logger.debug("Created isolated world in purchase iframe, ctx=%s", ctx_id)

            text_content = await self._eval_in_frame(ctx_id, "document.body?.innerText || ''")

            # Check for "unavailable in your region" using innerText to ignore hidden script tags
            unavailable = await self._eval_in_frame(ctx_id, """
                document.body?.innerText?.toLowerCase()?.includes('unavailable in your region') || false
            """)
            if unavailable:
                logger.error("'%s' is unavailable in your region!", title)
                return False

            # Handle parental PIN if configured
            if cfg.eg_parentalpin:
                has_pin = await self._eval_in_frame(ctx_id, """
                    !!document.querySelector('.payment-pin-code')
                """)
                if has_pin:
                    logger.debug("Entering parental PIN")
                    pin = cfg.eg_parentalpin
                    await self._eval_in_frame(ctx_id, f"""
                        (() => {{
                            const input = document.querySelector('input.payment-pin-code__input');
                            if (input) {{
                                input.focus();
                                input.value = '{pin}';
                                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            }}
                            const btns = [...document.querySelectorAll('button')];
                            const cont = btns.find(b => b.innerText && b.innerText.toLowerCase().includes('continue'));
                            if (cont) cont.click();
                        }})()
                    """)
                    await self.sleep(2)

            # Click "Place Order" (wait for it to not be in loading state)
            for attempt in range(8):
                clicked = await self._eval_in_frame(ctx_id, """
                    (() => {
                        const btns = [...document.querySelectorAll('button')];
                        const po = btns.find(b =>
                            b.innerText &&
                            b.innerText.toLowerCase().includes('place order') &&
                            !b.querySelector('.payment-loading--loading')
                        );
                        if (po && !po.disabled) { po.click(); return true; }
                        return false;
                    })()
                """)
                if clicked:
                    logger.debug("Clicked 'Place Order' for '%s'", title)
                    break
                await self.sleep(2)

            await self.sleep(2)

            # Handle "I Accept" / "I Agree" button (EU accounts only)
            # JS: const btnAgree = iframe.locator('button:has-text("I Accept")');
            await self._eval_in_frame(ctx_id, """
                (() => {
                    const btns = [...document.querySelectorAll('button')];
                    const agree = btns.find(b =>
                        b.innerText && (
                            b.innerText.toLowerCase().includes('i accept') ||
                            b.innerText.toLowerCase().includes('i agree')
                        )
                    );
                    if (agree) agree.click();
                })()
            """)

            # Wait for order confirmation on the MAIN page
            # Epic shows either "Thanks for your order!" or "It's all yours" dialog
            for _ in range(20):
                await self.sleep(2)
                confirmed = await self.page.evaluate("""
                    (() => {
                        const body = (document.body?.innerText || '').toLowerCase();
                        return body.includes('thanks for your order') ||
                               body.includes("it's all yours") ||
                               body.includes('thank you for buying');
                    })()
                """)
                if confirmed:
                    # Click "Continue browsing" to dismiss the success dialog
                    await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button, a')];
                            const cb = btns.find(b => {
                                const t = (b.textContent || '').trim().toLowerCase();
                                return t === 'continue browsing' || t.includes('continue');
                            });
                            if (cb) cb.click();
                        })()
                    """)
                    await self.sleep(1)
                    return True

            logger.warning("Timed out waiting for order confirmation for '%s'", title)
            return False

        except Exception:
            logger.exception("Error in purchase iframe handling for '%s'", title)
            return False

    def _find_purchase_frame(self, frame_tree) -> str | None:
        """Search through all browser frames to find the payment/purchase iframe."""
        if not hasattr(frame_tree, 'child_frames') or not frame_tree.child_frames:
            return None
        for child in frame_tree.child_frames:
            url = child.frame.url or ""
            if "payment" in url or "purchase" in url or "webPurchaseContainer" in url:
                return child.frame.id_
            found = self._find_purchase_frame(child)
            if found:
                return found
        return None

    async def _eval_in_frame(self, context_id: int, expression: str):
        """Evaluate JavaScript in a specific frame's isolated world via CDP."""
        try:
            result = await self.page.send(
                uc.cdp.runtime.evaluate(
                    expression=expression,
                    context_id=context_id,
                    return_by_value=True,
                )
            )
            # nodriver returns (RemoteObject, Optional[ExceptionDetails])
            if isinstance(result, tuple):
                remote_obj = result[0]
                return remote_obj.value if remote_obj else None
            if result and hasattr(result, 'value'):
                return result.value
            return None
        except Exception:
            logger.debug("eval_in_frame failed: %s...", expression[:60])
            return None

    async def _click_page_button_by_text(
        self, text: str, *, timeout: int = 3, log: str = ""
    ) -> bool:
        """Find and click a button on the main page by its text content.

        Uses page.evaluate() instead of nodriver's find() to avoid issues
        with Playwright-style pseudo-selectors like :has-text() which nodriver
        does not support.
        """
        for _ in range(max(1, timeout)):
            clicked = await self.page.evaluate(f"""
                (() => {{
                    const btns = [...document.querySelectorAll('button')];
                    const btn = btns.find(b => b.textContent.includes('{text}'));
                    if (btn) {{ btn.click(); return true; }}
                    return false;
                }})()
            """)
            if clicked:
                if log:
                    logger.debug("Clicked '%s' button (%s)", text, log)
                await self.sleep(1)
                return True
            await self.sleep(1)
        return False


async def claim_epic() -> dict:
    """Convenience entry point."""
    claimer = EpicGamesClaimer()
    await claimer.run()
    return {"store": "Epic Games", "user": claimer.user, "games": claimer.notify_games}
