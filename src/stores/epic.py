"""Epic Games Store module – claims the weekly free games from epicgames.com."""

from __future__ import annotations

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
from src.core.notifier import notify, format_game_list

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
                await notify(f"epic-games failed: {exc}")
        finally:
            # Send a notification with a summary of all claimed/failed games
            claimed_or_failed = [g for g in self.notify_games if g["status"] in ("claimed", "failed")]
            if claimed_or_failed and cfg.notify_summary:
                msg = f"**Epic Games** ({self.user}):\n{format_game_list(self.notify_games)}"
                await notify(msg)
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
        await self.sleep(3)

        async def _is_logged_in() -> bool:
            """Check if logged in by reading Epic's navigation bar attribute."""
            return await self.page.evaluate(
                """document.querySelector('egs-navigation')?.getAttribute('isloggedin') === 'true'"""
            )

        if await _is_logged_in():
            self.user = await self.page.evaluate(
                """document.querySelector('egs-navigation')?.getAttribute('displayname')"""
            )
            self.log_signed_in()
            return

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
            
            self.user = await self.page.evaluate(
                """document.querySelector('egs-navigation')?.getAttribute('displayname')"""
            )
            self.log_signed_in()
            return

        # Automated stealth login loop
        for attempt in range(3):
            logger.warning("Not signed in – attempting automated login (attempt %d/3)…", attempt + 1)
            
            if attempt > 0:
                await self.page.get("https://store.epicgames.com/")
                await self.sleep(3)
                
            await self._navigate_organically_to_login()
            await self.sleep(3)
            
            await self._do_stealth_login()
            
            # Wait loop to detect auth completion or interstitial
            for wait_sec in range(120):
                if "login" not in self.page.url:
                    break
                    
                if "login/review" in self.page.url:
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
                        await self.sleep(2)
                except Exception:
                    pass
                
                if wait_sec == 3:
                    logger.warning("Waiting for login to finish. If Captcha appeared, solve it via VNC! (2 min limit)")
                await self.sleep(1)

            # verify success
            await self.page.get(URL_CLAIM)
            await self.sleep(3)
            if await _is_logged_in():
                self.user = await self.page.evaluate(
                    """document.querySelector('egs-navigation')?.getAttribute('displayname')"""
                )
                self.log_signed_in()
                return
        
        logger.warning("Automated login failed after 3 attempts.")

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

        if cfg.eg_otpkey:
            # Handle MFA natively
            await self.sleep(3)
            try:
                otp_input = await self.page.find('input[name="code-input-0"]', timeout=5)
                if otp_input:
                    otp_code = pyotp.TOTP(cfg.eg_otpkey).now()
                    logger.debug("Entering MFA code")
                    await otp_input.clear_input()
                    await self.sleep(0.5)
                    await otp_input.send_keys(otp_code)
                    await self.sleep(1)
                    submit = await self.page.find('button[type="submit"]', timeout=5)
                    if submit:
                        await submit.click()
                        await self.sleep(3)
            except Exception:
                pass  # No MFA prompt

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

            # Wait for the purchase button to fully load (it starts as empty or "Loading")
            # We retry up to 10 times (10 seconds) until it shows meaningful text
            btn_text = ""
            for _ in range(10):
                btn_text = await self.page.evaluate(
                    """
                    (() => {
                        const btn = document.querySelector('button[data-testid="purchase-cta-button"]');
                        if (!btn) return '';
                        const t = btn.textContent.trim().toLowerCase();
                        if (!t || t === 'loading') return '';
                        return t;
                    })()
                    """
                )
                if btn_text:
                    break
                await self.sleep(1)

            # ── Handle mature content / age gate (before GET click) ──
            await self._click_page_button_by_text("Continue", timeout=2, log="mature content gate")

            # ── Read title ──
            title = await self.page.evaluate(
                """
                (() => {
                    // Check for bundle first
                    const aboutBundle = document.querySelector('span');
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

            if "in library" in btn_text:
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

            # ── Click GET button ──
            logger.info("Claiming '%s'...", title)
            await self.page.evaluate(
                """document.querySelector('button[data-testid="purchase-cta-button"]')?.click()"""
            )
            await self.sleep(2)

            # ── Handle "Device not supported" Continue dialog (AFTER GET) ──
            # This is the exact issue from the user's screenshot!
            # JS: page.click('button:has-text("Continue")').catch(_ => {});
            await self._click_page_button_by_text("Continue", timeout=3, log="Device not supported")

            # ── Handle "Yes, buy now" dialog ──
            # JS: page.click('button:has-text("Yes, buy now")').catch(_ => {});
            await self._click_page_button_by_text("Yes, buy now", timeout=1, log="already own partial")

            # ── Handle End User License Agreement ──
            # JS: check checkbox #agree, then click Accept
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

            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "skipped"
                await session.commit()
                return

            # The "Place Order" button is inside a cross-origin iframe (payment-store.epicgames.com).
            # Normal page.evaluate() can't see inside cross-origin iframes due to browser security.
            # We use Chrome DevTools Protocol (CDP) commands to create an isolated execution
            # context inside the iframe and run JavaScript there.
            await self.sleep(3)
            claimed = await self._handle_purchase_iframe(title)

            if claimed:
                logger.info("✓ Claimed '%s' successfully!", title)
                obj.status = "claimed"
                obj.updated_at = None  # triggers onupdate
                notify_game["status"] = "claimed"
            else:
                logger.error("Failed to claim '%s'.", title)
                obj.status = "failed"
                notify_game["status"] = "failed"
                await self.take_screenshot(f"epic_failed_{game_id}")
                if cfg.notify_claim_fails:
                    await notify(f"epic-games: failed to claim {title}")

            await session.commit()

    # ------------------------------------------------------------------
    # Purchase iframe handling (via CDP)
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

            # Wait for "Thanks for your order!" on the MAIN page
            for _ in range(20):
                await self.sleep(2)
                thanks = await self.page.evaluate(
                    """document.body?.innerText?.toLowerCase()?.includes('thanks for your order') || false"""
                )
                if thanks:
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


async def claim_epic() -> None:
    """Convenience entry point."""
    claimer = EpicGamesClaimer()
    await claimer.run()
