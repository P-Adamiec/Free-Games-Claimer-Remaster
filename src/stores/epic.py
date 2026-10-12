"""Epic Games Store module – claims the weekly free games from epicgames.com."""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timezone

import httpx
import nodriver as uc
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.claimer import BaseClaimer, CHALLENGE_JS, OTP_KEY_ATTEMPTS, track_missing_base
from src.core.config import cfg
from src.core.run_state import needs_you, waits_for_nobody
from src.core.database import async_session, get_or_create
from src.core.url_security import url_has_allowed_host
from src.stores.epic_mobile import fetch_mobile_free_games

logger = logging.getLogger("fgc.epic")

# URL of Epic's free games page (where we look for available free games)
URL_CLAIM = "https://store.epicgames.com/en-US/free-games"

# Login page URL, includes a redirect back to the free games page after login
URL_LOGIN = (
    "https://www.epicgames.com/id/login?lang=en-US"
    "&noHostRedirect=true&redirectUrl=" + URL_CLAIM
)


# Reads the product page's main action button. The order matters: an "In Library" chip in a
# recommendation row must never win over this product's own Get button.
# Epic keeps backup codes on the same URL as the authenticator, so the heading is the only tell.
BACKUP_SCREEN_JS = """
    (() => {
        const boxes = document.querySelectorAll('input[name^="code-input-"]');
        const text = (document.body ? document.body.innerText : '').toLowerCase();
        return boxes.length > 6 || text.includes('enter backup code');
    })()
"""

PAGE_STATE_JS = """
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

    // Epic swaps the whole product page for this sentence when the game is blocked where you are.
    const heading = (document.querySelector('h1')?.textContent || '').toLowerCase();
    if (heading.includes('unavailable in your platform or region')) return { text: '', flow: 'region' };

    return { text: '', flow: 'unknown' };
})())
"""

# A checked spot to click __FINDER__'s element at, or why not: missing, disabled, hidden, offscreen (dy) or covered.
BOX_JS = """
JSON.stringify((() => {
    const el = (__FINDER__)();
    if (!el) return { reason: 'missing' };
    if (el.disabled) return { reason: 'disabled' };
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return { reason: 'hidden' };
    if (r.top < 0 || r.bottom > innerHeight) return { reason: 'offscreen', dy: Math.round(r.top + r.height / 2 - innerHeight / 2) };
    const x0 = Math.max(r.left, 1), x1 = Math.min(r.right, innerWidth - 1);
    const y0 = Math.max(r.top, 1), y1 = Math.min(r.bottom, innerHeight - 1);
    if (x1 <= x0 || y1 <= y0) return { reason: 'hidden' };
    for (let i = 0; i < 8; i++) {
        const x = Math.round(x0 + (x1 - x0) * (0.2 + Math.random() * 0.6));
        const y = Math.round(y0 + (y1 - y0) * (0.2 + Math.random() * 0.6));
        const hit = document.elementFromPoint(x, y);
        const link = hit && hit.closest('a');
        if (hit && (hit === el || el.contains(hit)) && !(link && link !== el && el.contains(link)))
            return { x, y, key: el.tagName + '|' + (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60) };
    }
    return { reason: 'covered' };
})())
"""

# The last resort when the wheel cannot move a page, e.g. inside a dialog that locks scrolling.
SCROLL_INTO_VIEW_JS = "(() => { const el = (__FINDER__)(); if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' }); })()"

# A visible, enabled element of this tag whose text is the wanted one, or (unless exact) contains it.
TEXT_FINDER = r"""() => {
    const norm = b => (b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const usable = b => { const r = b.getBoundingClientRect(), s = getComputedStyle(b);
        return !b.disabled && r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const all = [...document.querySelectorAll(__TAG__)].filter(usable);
    return all.find(b => norm(b) === __TEXT__)__INCLUDES__;
}"""


def text_finder(text: str, tag: str = "button", exact: bool = False) -> str:
    """A finder for BOX_JS: the button whose text is `text`, or contains it unless exact."""
    want = json.dumps(text.strip().lower())
    return (TEXT_FINDER.replace("__TAG__", json.dumps(tag)).replace("__TEXT__", want)
            .replace("__INCLUDES__", "" if exact else f" || all.find(b => norm(b).includes({want}))"))


FIND_CHECKOUT_FRAME = """() => [...document.querySelectorAll('iframe')]
    .find(f => /payment|purchase/i.test(f.src || '') || f.closest('#webPurchaseContainer'))"""

# The checkout frame's content corner on the page and the part of it on screen, for clicks and the wheel.
FRAME_ORIGIN_JS = """
JSON.stringify((() => {
    const f = (__FRAME__)();
    if (!f) return null;
    const r0 = f.getBoundingClientRect();
    if (r0.top < 0 || r0.bottom > innerHeight) f.scrollIntoView({ block: 'nearest', behavior: 'instant' });
    const r = f.getBoundingClientRect(), s = getComputedStyle(f);
    return { x: r.left + f.clientLeft + parseFloat(s.paddingLeft || 0),
             y: r.top + f.clientTop + parseFloat(s.paddingTop || 0),
             w: f.clientWidth, top: Math.max(r.top, 0), bottom: Math.min(r.bottom, innerHeight) };
})())
""".replace("__FRAME__", FIND_CHECKOUT_FRAME)

# True when a page point is on screen and lands on the checkout frame itself, not on a backdrop above it.
FRAME_HIT_JS = """
((px, py) => px > 0 && py > 0 && px < innerWidth && py < innerHeight
    && document.elementFromPoint(px, py) === (__FRAME__)())
""".replace("__FRAME__", FIND_CHECKOUT_FRAME)

# Only what a person can see: a zero-size duplicate of a button must never be the one clicked.
SHOWN = r"""(b => { const r = b.getBoundingClientRect(), s = getComputedStyle(b);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; })"""
FIND_ADD_TO_LIBRARY = r"""() => [...document.querySelectorAll('button')].filter(__SHOWN__).find(b =>
    (b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase().includes('add to library'))""".replace("__SHOWN__", SHOWN)
FIND_I_ACCEPT = r"""() => [...document.querySelectorAll('button')].filter(__SHOWN__).find(b => {
    const t = (b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    return t.includes('i accept') || t.includes('i agree');
})""".replace("__SHOWN__", SHOWN)
FIND_PLACE_ORDER = r"""() => [...document.querySelectorAll('button')].filter(__SHOWN__).find(b => b.innerText
    && b.innerText.toLowerCase().includes('place order') && !b.querySelector('.payment-loading--loading'))""".replace(
    "__SHOWN__", SHOWN)
# Only Epic's licence box: the first checkbox can be "Share my email with <publisher>". A hidden box is ticked by its label.
FIND_LICENCE_BOX = r"""() => {
    const cb = document.querySelector('input#agree');
    if (!cb || cb.checked) return null;
    const r = cb.getBoundingClientRect();
    return (r.width > 0 && r.height > 0) ? cb : (document.querySelector('label[for="agree"]') || cb.closest('label'));
}"""
FIND_PURCHASE_CTA = r"""() => document.querySelector('button[data-testid="purchase-cta-button"]')"""
# Exactly "Accept" or "I accept": a word match could tick a cookie banner's "Accept All" for you.
FIND_ACCEPT_EXACT = r"""() => [...document.querySelectorAll('button')].find(b =>
    ['accept', 'i accept'].includes((b.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase()))"""
FIND_CONTINUE_BROWSING = r"""() => [...document.querySelectorAll('button, a')].find(b => {
    const t = (b.textContent || '').trim().toLowerCase();
    return t === 'continue browsing' || t.includes('continue');
})"""


def offers_a_purchase(state: dict) -> bool:
    """True when the product page's button sells the game ("Buy Now", "Pre-Purchase", a price) instead of giving it."""
    state = state or {}
    return state.get("flow") == "old_cta" and bool(
        re.search(r"buy|purchase|pre-?order|cart|\d", state.get("text") or "", re.I))


def checkout_total_is_zero(text) -> bool | None:
    """Epic's checkout total: True when nothing is to pay, False for any price, None when no total is shown."""
    low = str(text or "").lower()
    at = low.rfind("total")
    if at < 0:
        return None
    window = low[at + 5:at + 80]
    # Digits win over the word "free", so "Total (VAT free) €19.99" is never read as free.
    number = re.search(r"\d[\d.,   ]*", window)
    if number:
        return not re.sub(r"\D", "", number.group(0)).strip("0")
    return True if "free" in window else None


def is_owned(state: dict) -> bool:
    """True when the page offers no way to claim any more, only a link into the library."""
    state = state or {}
    return state.get("flow") == "owned" or "in library" in (state.get("text") or "").lower()


def is_region_locked(state: dict) -> bool:
    """True on Epic's "unavailable in your platform or region" page, which carries no game at all."""
    return (state or {}).get("flow") == "region"


def region_locked_status(previous: str | None) -> str | None:
    """A game you already took stays owned; anything else leaves the summary, you can do nothing about it."""
    return "existed" if (previous or "") in ("claimed", "existed") else None


def title_from_slug(game_id: str) -> str:
    """`buried-stars-d7c88c` -> `Buried Stars`, for when the page has no name to read."""
    return re.sub(r" [0-9a-f]{6}$", "", (game_id or "").replace("-", " "), flags=re.I).title()


class EpicGamesClaimer(BaseClaimer):
    store_name = "epic"

    def __init__(self) -> None:
        super().__init__()
        # Mobile game URL -> "Android"/"iOS", so claims can be told apart (same title on every platform).
        self._platform_labels: dict[str, str] = {}

    async def run(self, extra_games: list | None = None) -> None:
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
            if not await self._ensure_logged_in():
                logger.error("Aborting Epic Games flow due to login failure.")
                if cfg.notify_errors:
                    await self.notify("epic-games: could not sign in, no games were claimed.")
                return

            # Step 2: Find which games are currently free
            free_games = await self._detect_free_games()
            if not free_games and not extra_games:
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

            # --- Claim each game, then whatever GamerPower found for Epic ---
            for game in free_games:
                await self._claim_game(game["url"])
            handled = {url.rstrip("/").split("/")[-1] for url in (g["url"] for g in free_games)}
            for game in (extra_games or []):
                if game["final_url"].rstrip("/").split("/")[-1] in handled:
                    logger.debug("[GamerPower] '%s' is on Epic's own list, already handled.", game.get("title"))
                    continue
                logger.info("🎮 [GamerPower] '%s' → Epic Games", game.get("title", "Unknown"))
                await self._claim_game(game["final_url"])

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

    async def _ensure_logged_in(self) -> bool:
        """Check if we're logged into Epic. If not, try automatic login or VNC fallback.

        Returns False only when signing in definitely failed, so the caller can stop
        instead of clicking through pages logged out.
        """
        
        # Navigate to Epic Store frontend to initialize cookies/session natively
        await self.page.get("https://store.epicgames.com/")
        await self.sleep(5)  # Give the SPA + web components time to hydrate

        # Cloudflare can gate the store BEFORE login (looks like a failed "attempt 1/3" loop), detect it and hand off to VNC.
        if await self._human_challenge_present():
            if not await self._wait_out_challenge("Epic Games"):
                logger.warning("Epic security challenge not cleared in time – skipping.")
                return False
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

                    // Signal 5: Modern Epic redesign check, if on store.epicgames.com and Wishlist/Cart/Gifts text is visible without any Sign In link
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

        # Retry a few times, the web component loads asynchronously
        for _ in range(3):
            if await _is_logged_in():
                self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
                self.log_signed_in()
                return True
            await self.sleep(2)

        # Read credentials from the .env file
        email, password = cfg.eg_email, cfg.eg_password
        
        # No credentials provided, let the user log in manually through VNC
        if not email or not password:
            logger.warning("EG_EMAIL missing. Proceeding to login page for manual VNC login...")
            await self._navigate_organically_to_login()
            logged_in = await self._wait_for_vnc_login(_is_logged_in)
            if not logged_in:
                logger.warning("VNC login timed out – skipping.")
                return False

            self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
            self.log_signed_in()
            return True

        # Automated stealth login loop
        challenge_blocked = False
        mfa_manual = False
        for attempt in range(3):
            # A prompt nobody answered means nobody will answer the next two either.
            if waits_for_nobody(self.store_name):
                logger.warning("Epic is waiting for you, so the remaining attempts are skipped.")
                needs_you(self.store_name)
                return False
            logger.warning("Not signed in – attempting automated login (attempt %d/3)…", attempt + 1)

            if attempt > 0:
                await self.page.get("https://store.epicgames.com/")
                await self.sleep(3)

            await self._navigate_organically_to_login()
            await self.sleep(3)

            await self._do_stealth_login()

            otp_tried = 0
            backup_tried = False
            # Wait loop to detect auth completion or interstitial
            for wait_sec in range(120):
                try:
                    curr_url = str(await self.page.evaluate("window.location.href") or self.page.url)
                except Exception:
                    curr_url = str(self.page.url)

                # We know auth is complete when Epic redirects us back to the store domain.
                if url_has_allowed_host(curr_url, "store.epicgames.com"):
                    break

                # 2FA code screen: fill in the TOTP if EG_OTP_KEY is set, otherwise hand it to the user.
                if await self._mfa_prompt_present():
                    if not cfg.eg_otp_key or otp_tried >= OTP_KEY_ATTEMPTS:
                        # Seed gone or absent: one recovery code, then it is over to you.
                        if not backup_tried and await self._fill_backup_code():
                            backup_tried = True
                            await self.sleep(3)
                            continue
                        mfa_manual = True
                        break
                    if otp_tried:
                        # Epic answers a stale request with "Incorrect response. Please refresh the page."
                        logger.warning("Epic did not accept the code (%s), reloading and trying once more.",
                                       await self._mfa_error_text() or "no message on screen")
                        await self.page.reload()
                        await self.sleep(4)
                    otp_tried += 1
                    await self._fill_totp()
                    await self.sleep(3)
                    continue

                # Epic asks which account to continue with after any half-finished sign-in.
                # Verified live: the screen is a list of accounts, there is no Continue button.
                if "/id/login/switch-account" in curr_url.lower():
                    logger.debug("Epic is asking which account to continue with, picking yours.")
                    await self.page.evaluate(f"""
                        (() => {{
                            const want = {json.dumps((cfg.eg_email or "").lower())};
                            const tiles = [...document.querySelectorAll('[id^="account-"]')];
                            const tile = tiles.find(t => (t.textContent || '').toLowerCase().includes(want))
                                || tiles[0];
                            if (tile) tile.click();
                        }})()
                    """)
                    await self.sleep(5)
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
                            logger.debug("Auto-clicked 'Yes, continue'")
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
                        logger.debug("Auto-clicked 'Maybe later' on 2FA setup screen.")
                        await self.sleep(3)
                except Exception:
                    pass

                # A check is yours to clear, and then the bot sends the filled form itself.
                if wait_sec >= 4 and await self._human_challenge_present():
                    logger.warning("Captcha / security challenge detected during Epic login.")
                    if await self._wait_out_challenge("Epic", store_key=self.store_name)                             and await self._press_sign_in():
                        continue
                    challenge_blocked = True
                    break

                await self.sleep(1)

            # The wait loop can also run out of time with the code screen still up (issue #46).
            if not mfa_manual and await self._mfa_prompt_present():
                mfa_manual = True

            # A code screen or a captcha is yours to finish, so the page is left exactly as it stands.
            if mfa_manual or challenge_blocked:
                break

            # verify success
            await self.page.get(URL_CLAIM)
            await self.sleep(3)
            if await _is_logged_in():
                self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
                self.log_signed_in()
                return True

        # Automated login failed (captcha/2FA/repeated), notify and wait for the user to finish via VNC.
        if mfa_manual:
            reason = " (2FA code needed)"
        elif challenge_blocked:
            reason = " (captcha/challenge)"
        else:
            reason = " after 3 attempts"
        logger.warning("Automated Epic login failed%s – requesting manual login via VNC.", reason)
        if mfa_manual:
            custom_msg = self._vnc_notice(
                "Epic Games: 2FA code needed",
                "Enter the 6-digit code Epic sent to your email or phone (or from your authenticator). "
                "If a 'One more step' security check appears, complete it too.",
            )
        else:
            custom_msg = self._vnc_notice(
                "Epic Games: login needs you",
                "A captcha or security challenge is blocking automated sign-in. Open the browser, solve it and finish logging in.",
            )
        if await self._wait_for_vnc_login(_is_logged_in, custom_msg=custom_msg):
            self.user = await _get_display_name() or cfg.eg_email or "EpicUser"
            self.log_signed_in()
            return True

        logger.warning("Epic login still not completed after VNC wait – skipping.")
        return False

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

    async def _mfa_error_text(self) -> str:
        """Whatever Epic printed above the code boxes, empty when it printed nothing."""
        try:
            said = await self.page.evaluate(r"""
                (() => {
                    const box = document.querySelector('[role="alert"], [class*="error"], [class*="Error"]');
                    return box ? (box.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120) : '';
                })()
            """)
            return str(said or "")
        except Exception as exc:
            logger.debug("Could not read Epic's 2FA message: %s", exc)
            return ""

    async def _fill_code(self, code: str) -> bool:
        """Type a code into Epic's six boxes and submit. Verified live: they take all six at once."""
        try:
            first_box = await self.page.select('input[name="code-input-0"]', timeout=5)
            if not first_box:
                return False
            await first_box.clear_input()
            await self.sleep(0.5)
            await first_box.send_keys(code)
            await self.sleep(1)
            # Epic ticks "Remember device" by default; this only helps when it did not.
            await self._remember_this_browser()
            submit = await self.page.select('button[type="submit"]', timeout=5)
            if submit:
                await submit.click()
                await self.sleep(3)
            return True
        except Exception as exc:
            logger.debug("Could not enter the 2FA code: %s", exc)
            return False

    async def _fill_totp(self) -> bool:
        """Auto-enter the authenticator (TOTP) code from EG_OTP_KEY, then submit."""
        if not cfg.eg_otp_key:
            return False
        logger.debug("Entering MFA (TOTP) code")
        self._last_totp = await self._fresh_totp(cfg.eg_otp_key, self._last_totp)
        return await self._fill_code(self._last_totp)

    async def _open_backup_code_screen(self) -> bool:
        """Switch from the authenticator boxes to the backup-code ones.

        Verified live: "Try another way" opens /id/login/mfa/options, where #option-backupCode
        returns eight text boxes instead of the six numeric ones.
        """
        # The options page renders on its own schedule, so each step is waited for, not assumed.
        for _ in range(6):
            try:
                if await self.page.evaluate(BACKUP_SCREEN_JS):
                    return True
                step = await self.page.evaluate("""
                    (() => {
                        const opt = document.querySelector('#option-backupCode');
                        if (opt) { opt.click(); return 'option'; }
                        const link = [...document.querySelectorAll('a, button, [role="button"]')]
                            .find(e => /another way/i.test((e.textContent || '').trim()));
                        if (link) { link.click(); return 'link'; }
                        return '';
                    })()
                """)
                logger.debug("Backup code screen step: %s", step or "nothing to click")
            except Exception as exc:
                logger.debug("Could not open Epic's backup code screen: %s", exc)
                return False
            await self.sleep(2)
        return False

    async def _fill_backup_code(self) -> bool:
        """Spend one recovery code from EG_OTP_CODES, the way GOG does."""
        if not cfg.eg_otp_codes:
            return False
        code = self._next_unused_code(cfg.eg_otp_codes, "used_epic_codes.txt")
        if not code:
            logger.warning("Every Epic recovery code has been used already.")
            return False
        # The authenticator boxes take digits only, a backup code is eight characters of text.
        if not await self._open_backup_code_screen():
            logger.warning("Epic's backup code screen did not open, leaving your codes alone.")
            return False
        logger.info("Trying one of your Epic recovery codes.")
        if not await self._fill_code(code.replace("-", "").replace(" ", "")):
            return False
        self._mark_code_used(code, "used_epic_codes.txt", cfg.eg_otp_codes)
        return True

    async def _navigate_organically_to_login(self) -> None:
        """Navigates to the login page mimicking a click from the store, preserving Referer headers."""
        try:
            # Emulate clicking the "Sign In" link by setting location.href inside the existing page context
            await self.page.evaluate(f"window.location.href = '{URL_LOGIN}'")
        except Exception:
            await self.page.get(URL_LOGIN)

    async def _press_sign_in(self) -> bool:
        """Send the sign-in form again, for when a human check interrupted one you had filled."""
        # No text fallback: "Sign in" also matches "Sign in with Apple" (#72).
        button = await self.page.select("#sign-in", timeout=5)
        if not button:
            logger.debug("No sign-in button came back after the check.")
            return False
        logger.info("Human check cleared, finishing the sign-in.")
        await button.click()
        await self.sleep(6)
        return True

    async def _do_stealth_login(self) -> None:
        """Fill in email and password using browser-native methods.
        
        Uses real keyboard input (CDP events) instead of JavaScript injection,
        which makes the login look more human-like to anti-bot systems.
        """
        email = cfg.eg_email.strip() if cfg.eg_email else ""
        password = cfg.eg_password.strip() if cfg.eg_password else ""

        email_input = await self.page.select("#email", timeout=10)
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

            continue_btn = await self.page.select("#continue", timeout=5)
            if continue_btn:
                await continue_btn.click()
                logger.debug("Clicked continue, waiting for CSS slide animation...")
                await self.sleep(3.0)  # Wait for CSS slide transition completely

        password_input = await self.page.select("#password", timeout=10)
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
                remember_label = await self.page.select("label[for='rememberMe']", timeout=2)
                if remember_label:
                    await remember_label.click()
                    await self.sleep(0.5)
        except Exception:
            pass
            
        sign_in_btn = await self.page.select("#sign-in", timeout=5)
        if sign_in_btn:
            await sign_in_btn.click()
            await self.sleep(3)

        # Authenticator (TOTP) auto-fill; email/SMS codes are handled in the loop.
        if cfg.eg_otp_key:
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
        """Find currently free games, tries the API first, falls back to page scraping."""
        games = await self._detect_free_games_api()
        if not games:
            logger.warning("API detection returned 0 games, falling back to DOM scraping.")
            games = await self._detect_free_games_dom()
        games.extend(await self._detect_mobile_free_games(games))
        return games

    async def _detect_mobile_free_games(self, known: list[dict]) -> list[dict]:
        """Free mobile (Android/iOS) giveaways, same product pages, different listing API."""
        if not cfg.eg_mobile:
            return []
        try:
            # The mobile API only answers same-site requests from the store page.
            current = await self.page.evaluate("location.href")
            if not url_has_allowed_host(str(current), "store.epicgames.com"):
                await self.page.get(URL_CLAIM)
                await self.sleep(3)
            mobile = await fetch_mobile_free_games(self._fetch_store_json, cfg.eg_mobile_platform_list)
        except Exception:
            logger.exception("Mobile free-game detection failed, continuing with PC games only")
            return []
        seen = {g["url"] for g in known}
        new = [g for g in mobile if g["url"] not in seen]
        for game in new:
            if game.get("label"):
                self._platform_labels[game["url"]] = game["label"]
        if new:
            logger.info("📱 Including %d free mobile game(s) (%s)", len(new), ", ".join(cfg.eg_mobile_platform_list))
        return new

    async def _fetch_store_json(self, url: str) -> dict:
        """Read a store JSON API from inside the page (Cloudflare 403s plain HTTP clients)."""
        raw = await self.page.evaluate(
            f"fetch({json.dumps(url)}, {{credentials: 'omit'}}).then(r => r.text())",
            await_promise=True,
        )
        return json.loads(raw) if isinstance(raw, str) else {}

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

            logger.debug("Promo API: %d element(s), %d currently free: %s",
                         len(elements), len(free_games), [g["url"] for g in free_games])
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

    async def _page_state(self, tries: int = 15) -> dict:
        """Read the product page's main action button: {"text", "flow"}."""
        state = {"text": "", "flow": "unknown"}
        for _ in range(tries):
            raw = await self.page.evaluate(PAGE_STATE_JS)
            try:
                state = json.loads(raw) if isinstance(raw, str) else state
            except (json.JSONDecodeError, TypeError):
                state = {"text": "", "flow": "unknown"}
            if state.get("text") or is_region_locked(state):
                break
            await self.sleep(1)
        return state

    async def _confirm_in_library(self, url: str, tries: int = 3) -> bool:
        """Re-read the product page after checkout, only an owned page proves the claim landed."""
        for attempt in range(tries):
            if attempt:
                # Epic's library can take a few seconds to catch up with the order.
                await self.sleep(6)
            try:
                await self.page.get(url)
                await self.sleep(4)
                await self._click_page_button_by_text("Continue", timeout=2, log="mature content gate")
                state = await self._page_state(tries=8)
            except Exception as exc:
                logger.debug("Ownership check %d/%d failed: %s", attempt + 1, tries, exc)
                continue
            logger.debug("Ownership check %d/%d: button=%r flow=%s",
                         attempt + 1, tries, state.get("text"), state.get("flow"))
            if is_owned(state):
                return True
        return False

    async def _checkout_challenge_present(self) -> bool:
        """A human check on the page, or inside the checkout frame where the page check cannot look."""
        if await self._human_challenge_present():
            return True
        try:
            frame_id = self._find_purchase_frame(await self.page.send(uc.cdp.page.get_frame_tree()))
            if not frame_id:
                return False
            ctx_id = await self.page.send(
                uc.cdp.page.create_isolated_world(frame_id=frame_id, grant_univeral_access=True)
            )
            return bool(await self._eval_in_frame(ctx_id, CHALLENGE_JS))
        except Exception as exc:
            logger.debug("Could not look for a human check in the checkout frame: %s", exc)
            return False

    async def _clear_checkout_challenge(self, title: str) -> bool:
        """Hand a checkout captcha to you over VNC. True when there was none or it got solved."""
        if not await self._checkout_challenge_present():
            return True
        logger.debug("Human check on the checkout for '%s'.", title)
        return await self._wait_out_challenge("Epic", store_key=self.store_name,
                                              present_fn=self._checkout_challenge_present)

    # Retry up to 2 times with exponential backoff if claiming fails
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15), reraise=True)
    async def _claim_game(self, url: str) -> None:
        """Claim a single free game by navigating to its page and clicking through the purchase flow."""
        self._click_counts = {}
        # Extract the game identifier from the URL (e.g. "fortnite" from ".../p/fortnite")
        game_id = url.rstrip("/").split("/")[-1]

        async with async_session() as session:
            obj, created = await get_or_create(
                session, store=self.store_name, user=self.user or "unknown",
                game_id=game_id, title=game_id, url=url, status="unknown",
            )
            # The page, not the database, decides: a row saying "claimed" can be stale
            # or simply wrong, and skipping on it alone would hide the game forever.
            if not created:
                logger.debug("DB says '%s' is '%s', verifying on the store page anyway", game_id, obj.status)

            await self.page.get(url)
            await self.sleep(4)

            # ── Handle mature content / age gate ──
            await self._click_page_button_by_text("Continue", timeout=2, log="mature content gate")

            # ── Detect page state: find the primary action button ──
            # Epic has two checkout flows:
            #   NEW (2025+): Direct "Add to library" / "Get" button on checkout overlay
            #   OLD: "purchase-cta-button" data-testid + payment iframe
            state = await self._page_state()
            btn_text = state.get("text", "")
            flow_type = state.get("flow", "unknown")
            logger.debug("Page state for %s: button=%r flow=%s", game_id, btn_text, flow_type)

            # The heading here is Epic's sentence, so the name comes from the database or the address.
            if is_region_locked(state):
                known = obj.title if obj.title and obj.title != game_id and not obj.title.endswith("region.") else ""
                title = known or title_from_slug(game_id)
                outcome = region_locked_status(obj.status)
                if outcome:
                    logger.info("'%s' is blocked in your region on Epic now, it stays in your library.", title)
                    self.notify_games.append({"title": title, "url": url, "status": outcome})
                else:
                    logger.info("'%s' is not available in your region on Epic, skipping.", title)
                    obj.status = "skipped:region"
                obj.title = title
                await session.commit()
                return

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
            # Mobile SKUs share the PC title, so tag them (e.g. "Foretales (Android)").
            label = self._platform_labels.get(url)
            if label:
                title = f"{title} ({label})"
            obj.title = title

            notify_game = {"title": title, "url": url, "status": "failed"}
            self.notify_games.append(notify_game)

            if is_owned(state):
                logger.info("'%s' already in library.", title)
                obj.status = obj.status if obj.status == "claimed" else "existed"
                notify_game["status"] = "existed"
                await session.commit()
                return

            if "requires base game" in btn_text:
                # Same tag as Steam, so one setting silences both (issue #48); told once, then weeks later (#78).
                notify_game["status"] = track_missing_base(obj, remember=not cfg.dryrun)
                if notify_game["status"] == "failed:missing_base":
                    logger.warning("'%s' requires base game.", title)
                else:
                    logger.info("'%s' still requires its base game (already reported).", title)
                await session.commit()
                return

            if not btn_text:
                logger.warning("No purchase/claim button found for '%s'.", title)
                obj.status = "failed"
                await self.take_screenshot(f"epic_nobutton_{game_id}")
                await session.commit()
                return

            # A page that sells the game (an ended GamerPower giveaway, next week's game) is never checked out.
            if offers_a_purchase(state):
                logger.info("'%s' is not free on Epic right now (button: %s), skipping.", title, btn_text)
                obj.status = notify_game["status"] = "skipped:not-free"
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
                checkout_ok = await self._handle_new_checkout(title, flow_type)

            # ── OLD FLOW: purchase-cta-button + payment iframe ──
            elif flow_type == "old_cta":
                await self._cdp_click_found(FIND_PURCHASE_CTA, "purchase button", allow_script=True, tries=2)
                await self.sleep(2)

                # Handle intermediate dialogs
                await self._click_page_button_by_text("Continue", timeout=3, log="Device not supported")
                await self._click_page_button_by_text("Yes, buy now", timeout=1, log="already own partial")

                # Handle End User License Agreement
                await self._cdp_click_found(FIND_LICENCE_BOX, "licence box", allow_script=True, tries=2)
                await self._cdp_click_found(FIND_ACCEPT_EXACT, "Accept", allow_script=True, tries=2)

                await self.sleep(3)
                checkout_ok = await self._handle_purchase_iframe(title)
                if checkout_ok == "not-free":
                    obj.status = notify_game["status"] = "skipped:not-free"
                    await session.commit()
                    return
            else:
                logger.warning("Unknown checkout flow '%s' for '%s'.", flow_type, title)
                checkout_ok = False

            # Checking ownership leaves the page, which would drop an order still waiting on a captcha.
            await self._clear_checkout_challenge(title)

            # The checkout page only says what it thinks happened, the library says what is true.
            if await self._confirm_in_library(url):
                logger.info("✓ Claimed '%s' successfully!", title)
                obj.status = "claimed"
                obj.updated_at = datetime.now(timezone.utc)
                notify_game["status"] = "claimed"
            else:
                # The game is still free to take, so the summary says so instead of hiding a "failed".
                if checkout_ok:
                    logger.warning("Claim of '%s' was not confirmed by the page, claim it yourself: %s", title, url)
                    obj.status = "failed:unconfirmed"
                    await self.take_screenshot(f"epic_unconfirmed_{game_id}")
                else:
                    logger.error("Failed to claim '%s', claim it yourself: %s", title, url)
                    obj.status = "failed"
                    await self.take_screenshot(f"epic_failed_{game_id}")
                notify_game["status"] = "notified"
                needs_you(self.store_name)

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

            logger.debug("Clicked '%s' button for '%s'.", initial_btn, title)
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
                    logger.debug("After Get: %s detected", post_get_state)
                else:
                    logger.debug("After Get: no immediate state change detected")

            await self.sleep(3)

            # ── Step 2: Handle intermediate dialogs ──
            # "Device not supported" → Continue
            cont_clicked = await self._cdp_click_element_by_text("continue", timeout=5)
            if cont_clicked:
                logger.debug("Clicked 'Continue' dialog. Waiting for checkout overlay...")
                # The checkout overlay takes several seconds to load after Continue
                await self.sleep(5)
            else:
                logger.debug("No 'Continue' dialog found, proceeding.")
                await self.sleep(2)

            # EULA / Terms acceptance (check before Add to library)
            await self._cdp_click_found(FIND_LICENCE_BOX, "licence box", allow_script=True, tries=2)
            await self._cdp_click_element_by_text("accept", timeout=1, exact=True)
            await self._cdp_click_element_by_text("i agree", timeout=1, exact=True)

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
                        accepted = (await self._cdp_click_element_by_text("i accept", timeout=2, exact=True)
                                    or await self._cdp_click_element_by_text("i agree", timeout=1, exact=True))
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
                        # The frame is cross-origin, so a script click there was the one click Epic could tell from a hand.
                        last_try = attempt == 24
                        if not add_clicked and await self._cdp_click_in_frame(
                                ctx_id, FIND_ADD_TO_LIBRARY, "add to library", allow_script=last_try):
                            logger.debug("✓ Found and clicked 'Add to library' inside iframe.")
                            add_clicked = True
                            await self.sleep(2)

                        if not accepted and await self._cdp_click_in_frame(
                                ctx_id, FIND_I_ACCEPT, "i accept", allow_script=last_try):
                            logger.debug("✓ Found and clicked 'I accept' inside iframe.")
                            accepted = True
                            await self.sleep(2)

                    if add_clicked and accepted:
                        # Once we've clicked Add and explicitly handled Accept, we can wait for verification
                        # We don't break immediately, let already_done or the timeout push us forward
                        pass

                    # A captcha holds the order, and the page text below can read as success meanwhile (#61).
                    if not await self._clear_checkout_challenge(title):
                        return False

                    # Also check if it was already confirmed (fallback checking main page)
                    already_done = await self.page.evaluate(
                        """
                        (() => {
                            const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
                            // "Add it to your library" is the offer, not a confirmation.
                            const inLib = (body.includes('in library') || body.includes('in your library'))
                                && !body.includes('add it to your library');
                            return body.includes('thank you') || body.includes('successfully') || inLib;
                        })()
                        """
                    )
                    if already_done:
                        logger.debug("Already confirmed without needing more clicks.")
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
                if not await self._clear_checkout_challenge(title):
                    return False
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

    async def _read_box(self, run_js, finder: str) -> dict:
        """BOX_JS for this finder in the page or the checkout frame; an unreadable page says so."""
        try:
            raw = await run_js(BOX_JS.replace("__FINDER__", finder))
        except Exception as e:
            return {"reason": f"unreadable ({e})"}
        try:
            return json.loads(raw) if isinstance(raw, str) else {"reason": "unreadable"}
        except ValueError:
            return {"reason": "unreadable"}

    async def _page_wheel_point(self) -> tuple:
        """A spot over the middle of the page for the mouse wheel."""
        try:
            vw, vh = json.loads(await self.page.evaluate("JSON.stringify([innerWidth, innerHeight])"))
        except Exception:
            vw, vh = 1280, 720
        return vw * random.uniform(0.35, 0.65), vh * random.uniform(0.35, 0.65)

    async def _bring_into_view(self, read, run_js, wheel_point, finder: str, label: str) -> dict:
        """Scroll an off-screen element into view with the mouse wheel, the jump only when the wheel moves nothing."""
        box = await read()
        for _ in range(6):
            if box.get("reason") != "offscreen":
                return box
            point = await wheel_point()
            if not point:
                break
            await self._wheel_scroll(point[0], point[1], box["dy"])
            moved = await read()
            if moved.get("reason") == "offscreen" and abs(moved.get("dy", 0) - box["dy"]) < 5:
                break
            box = moved
        if box.get("reason") == "offscreen":
            logger.debug("The wheel does not move '%s' into view, scrolling to it directly.", label)
            await run_js(SCROLL_INTO_VIEW_JS.replace("__FINDER__", finder))
            box = await read()
        return box

    async def _click_took(self, read, label: str, key: str = "", before: str = "") -> bool:
        """True after two reads in a row show the clicked element gone, disabled, covered or replaced by another one.

        A script-clicked hidden element must change, not just stay hidden, to count.
        """
        settled = 0
        for _ in range(8):
            await self.sleep(0.5)
            box = await read()
            reason = box.get("reason", "")
            replaced = "x" in box and key and box.get("key") != key
            gone = reason in ("missing", "disabled", "hidden", "covered") or reason.startswith("unreadable")
            settled = settled + 1 if replaced or (gone and reason != before) else 0
            if settled >= 2:
                return True
        logger.debug("'%s' is still there and clickable after the click, it did not take.", label)
        return False

    async def _click_until_taken(self, read, run_js, wheel_point, to_page, finder: str, label: str,
                                 tries: int = 1, allow_script: bool = False) -> bool:
        """Click with the mouse until the click takes, at most twice per button in one checkout.

        A covered element (an overlay or the captcha) is waited out and never clicked through; a script click is only
        for a hidden element, or, when allowed, for one two mouse clicks could not move.
        """
        clicks = self.__dict__.setdefault("_click_counts", {})
        reason, key = "missing", ""
        for attempt in range(max(1, tries)):
            if attempt:
                await self.sleep(1)
            box = await self._bring_into_view(read, run_js, wheel_point, finder, label)
            if "x" not in box:
                reason = box.get("reason", "missing")
                if reason == "hidden":
                    break
                continue
            if clicks.get(label, 0) >= 2:
                reason = "not taken"
                break
            point = await to_page(box)
            if not point:
                reason = "covered on the page"
                continue
            key = box.get("key", "")
            await self._human_click(*point)
            clicks[label] = clicks.get(label, 0) + 1
            if await self._click_took(read, label, key):
                logger.debug("Clicked '%s' with the mouse.", label)
                return True
            reason = "not taken"
        if reason == "hidden" or (allow_script and reason == "not taken"):
            if await run_js(self._script_click_js(finder)) is True and await self._click_took(read, label, key, reason):
                logger.debug("'%s' is %s, clicked by script.", label, reason)
                return True
        logger.debug("'%s' not clicked: %s.", label, reason)
        return False

    @staticmethod
    def _script_click_js(finder: str) -> str:
        return f"(() => {{ const el = ({finder})(); if (el && !el.disabled) {{ el.click(); return true; }} return false; }})()"

    async def _cdp_click_found(self, finder: str, label: str, allow_script: bool = False, tries: int = 1) -> bool:
        """Click on the page with the mouse until it takes, see _click_until_taken."""
        run_js = self.page.evaluate

        async def read():
            return await self._read_box(run_js, finder)

        async def as_is(box):
            return box["x"], box["y"]

        return await self._click_until_taken(read, run_js, self._page_wheel_point, as_is, finder, label,
                                             tries=tries, allow_script=allow_script)

    async def _cdp_click_element_by_text(self, text: str, *, tag: str = "button", timeout: int = 1,
                                         exact: bool = False) -> bool:
        """Click the button with this text with the mouse until it takes, waiting up to `timeout` tries for it."""
        return await self._cdp_click_found(text_finder(text, tag, exact), text, tries=timeout)

    async def _cdp_click_in_frame(self, ctx_id: int, finder: str, label: str, allow_script: bool = False,
                                  tries: int = 1) -> bool:
        """Click inside the cross-origin checkout frame with the mouse: the spot inside it plus where it sits on the page."""

        async def run_js(expression):
            return await self._eval_in_frame(ctx_id, expression)

        async def read():
            return await self._read_box(run_js, finder)

        async def frame():
            try:
                return json.loads(await self.page.evaluate(FRAME_ORIGIN_JS) or "null")
            except Exception:
                return None

        async def wheel_point():
            f = await frame()
            if not f or f["bottom"] - f["top"] < 40:
                return None
            return (f["x"] + f["w"] * random.uniform(0.3, 0.7),
                    f["top"] + (f["bottom"] - f["top"]) * random.uniform(0.3, 0.7))

        async def to_page(box):
            f = await frame()
            if not f:
                return None
            px, py = round(f["x"] + box["x"]), round(f["y"] + box["y"])
            return (px, py) if await self.page.evaluate(f"({FRAME_HIT_JS})({px}, {py})") is True else None

        return await self._click_until_taken(read, run_js, wheel_point, to_page, finder, f"{label} (checkout frame)",
                                             tries=tries, allow_script=allow_script)

    # ------------------------------------------------------------------
    # Purchase iframe handling (via CDP), OLD flow
    # ------------------------------------------------------------------

    async def _handle_purchase_iframe(self, title: str) -> bool | str:
        """Complete the purchase inside Epic's payment iframe, or answer "not-free" when its total is not zero.

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

            # The same window sells games, so only a total of zero is ever ordered; an unreadable one is not.
            free = None
            for _ in range(5):
                text = await self._eval_in_frame(ctx_id, "document.body?.innerText || ''")
                free = checkout_total_is_zero(text)
                if free is not None:
                    break
                await self.sleep(2)
            # Only the total itself: the rest of the checkout can show your e-mail address.
            at = str(text or "").lower().rfind("total")
            logger.debug("Checkout total for '%s' reads as free=%s: %r", title, free,
                         str(text)[at:at + 80].replace("\n", " | ") if at >= 0 else f"no total in {len(str(text or ''))} chars")
            if free is False:
                logger.warning("'%s' costs money in Epic's checkout, nothing was ordered.", title)
                return "not-free"
            if free is None:
                logger.warning("Could not read the checkout total for '%s', nothing was ordered.", title)
                return False

            # Click "Place Order" (wait for it to not be in loading state)
            for attempt in range(8):
                clicked = await self._cdp_click_in_frame(ctx_id, FIND_PLACE_ORDER, "place order", allow_script=attempt == 7)
                if clicked:
                    logger.debug("Clicked 'Place Order' for '%s'", title)
                    break
                await self.sleep(2)

            await self.sleep(2)

            # Handle "I Accept" / "I Agree" button (EU accounts only)
            await self._cdp_click_in_frame(ctx_id, FIND_I_ACCEPT, "i accept", allow_script=True, tries=2)

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
                    await self._cdp_click_found(FIND_CONTINUE_BROWSING, "continue browsing", allow_script=True, tries=2)
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
        """Click the page button whose text contains `text`, waiting up to `timeout` tries for it to appear."""
        finder = f"() => [...document.querySelectorAll('button')].filter({SHOWN}).find(b => b.textContent.includes({json.dumps(text)}))"
        if not await self._cdp_click_found(finder, text, allow_script=True, tries=max(1, timeout)):
            return False
        if log:
            logger.debug("Clicked '%s' button (%s)", text, log)
        await self.sleep(1)
        return True


async def claim_epic(extra_games: list | None = None) -> dict:
    """Convenience entry point."""
    claimer = EpicGamesClaimer()
    await claimer.run(extra_games)
    return {"store": "Epic Games", "user": claimer.user, "games": claimer.notify_games}
