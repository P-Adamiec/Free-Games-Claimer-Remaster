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

from src.core.claimer import (BaseClaimer, CAPTCHA_ANSWER_JS, CHALLENGE_JS, OTP_KEY_ATTEMPTS, account_key, answer_filled,
                              captcha_answer_note, challenge_needs_you, giveaway_end, mark_page_checked, RECHECK_DAYS,
                              older_than, row_extra, text_finder, track_missing_base)
from src.core.config import cfg
from src.core.display import vnc_viewers
from src.core.run_state import (CAPTCHA_ENDS, CAPTCHA_UNSOLVED, book, booking_until, ends_before_next_run, is_booked,
                                lost_before_next_run, mark_answered, mark_unanswered, needs_you, next_run, unbook_title,
                                utc_text, waits_for_nobody)
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

# What a page said that does not change within a week's promotion.
RECHECKED = ("claimed", "existed", "skipped:region")
# Not yours to get anyway, so after a captcha they wait without being counted as waiting for you.
NOT_CLAIMABLE = ("failed:missing_base", "skipped:not-free")


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

def tile_finder(slug: str) -> str:
    """A finder for BOX_JS: a shown link to this game's page, like its tile on the free games list."""
    return TILE_FINDER.replace("__SHOWN__", SHOWN).replace("__WANT__", json.dumps("/" + slug.strip("/").lower()))


def game_ends(free_games: list[dict], extra_games: list[dict] | None) -> dict:
    """When each game's giveaway ends, by slug: Epic's own date first, GamerPower's only for a game Epic gave none."""
    ends = {g["url"].rstrip("/").split("/")[-1]: giveaway_end(g.get("ends")) for g in free_games}
    for g in extra_games or []:
        slug = (g.get("final_url") or "").rstrip("/").split("/")[-1]
        if slug and ends.get(slug) is None:
            # GamerPower names only the day (23:59, while Epic ended Sorry We're Closed at 16:00 UTC), so its start counts.
            end = giveaway_end(g.get("end_date"))
            ends[slug] = end.replace(hour=0, minute=0, second=0, microsecond=0) if end else None
    return ends


def masked_email(email: str | None) -> str:
    """How Epic writes an address on its saved-account list: first and last letter of the name, then the domain."""
    local, at, domain = (email or "").strip().lower().partition("@")
    return f"{local[:1]}***{local[-1:]}@{domain}" if at and local else ""


def saved_account_finder(email: str | None) -> str:
    """A finder for BOX_JS: your tile on Epic's saved-account list, by EG_EMAIL, else the only tile there is."""
    want = (email or "").strip().lower()
    return (SAVED_ACCOUNT_FINDER.replace("__SHOWN__", SHOWN).replace("__WANT__", json.dumps(want))
            .replace("__MASKED__", json.dumps(masked_email(want))))


# Run on the checkout <iframe>: its content corner on the page and the part of it on screen, for clicks and the wheel.
FRAME_ORIGIN_JS = """function() {
    const f = this, r0 = f.getBoundingClientRect();
    if (r0.top < 0 || r0.bottom > innerHeight) f.scrollIntoView({ block: 'nearest', behavior: 'instant' });
    const r = f.getBoundingClientRect(), s = getComputedStyle(f);
    return JSON.stringify({ x: r.left + f.clientLeft + parseFloat(s.paddingLeft || 0),
                            y: r.top + f.clientTop + parseFloat(s.paddingTop || 0),
                            w: f.clientWidth, top: Math.max(r.top, 0), bottom: Math.min(r.bottom, innerHeight) });
}"""

# Run on the checkout <iframe>: true when a page point is on screen and lands on it, not on a backdrop above it.
FRAME_HIT_JS = """function(px, py) {
    return px > 0 && py > 0 && px < innerWidth && py < innerHeight && document.elementFromPoint(px, py) === this;
}"""

# Only what a person can see: a zero-size duplicate of a button must never be the one clicked.
SHOWN = r"""(b => { const r = b.getBoundingClientRect(), s = getComputedStyle(b);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; })"""
TILE_FINDER = r"""() => [...document.querySelectorAll('a[href*="/p/"], a[href*="/bundles/"]')].filter(__SHOWN__)
    .find(a => (a.getAttribute('href') || '').split(/[?#]/)[0].replace(/\/+$/, '').toLowerCase().endsWith(__WANT__))"""
# Epic's "continue as" list after a half-finished sign-in, on /id/login/switch-account and on the MFA address alike.
# Verified live (PR #91): each tile is a div[role=button] with id="account-…" and the account's e-mail, no Continue button.
# With EG_EMAIL set only its tile counts, otherwise only a lone tile: which account to claim on is never a guess.
SAVED_ACCOUNT_FINDER = r"""() => {
    if (!location.pathname.toLowerCase().includes('/id/login/')) return null;
    const tiles = [...document.querySelectorAll('[id^="account-"]')].filter(__SHOWN__);
    const want = __WANT__;
    const masked = __MASKED__;
    // Live, 21.10: the tile shows the address masked ("p***l@gmail.com"), and a single tile can be another account.
    if (want) {
        const hits = tiles.filter(t => { const s = (t.textContent || '').toLowerCase();
            return s.includes(want) || (masked && s.includes(masked)); });
        return hits.length === 1 ? hits[0] : null;
    }
    return tiles.length === 1 ? tiles[0] : null;
}"""
# Under the tiles: starts a fresh sign-in, the way a person goes on when none of the saved accounts is theirs.
USE_ANOTHER_ACCOUNT = "() => [...document.querySelectorAll('#use-different-account')].filter(__SHOWN__)[0]".replace(
    "__SHOWN__", SHOWN)
HAS_SAVED_ACCOUNTS_JS = "!!document.querySelector('[id^=\"account-\"]')"
# What Epic prints when it will not take what was sent around a check (#46, #100).
STALE_ANSWER_JS = "(document.body?.innerText || '').toLowerCase().includes('refresh the page')"
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


# A price with two decimals in any currency and order ("PLN 0.00", "0,00 €", "1 234,56"); "over 18" is not one.
AMOUNT_RE = re.compile(r"(?<!\d)\d{1,3}(?:[\s.,]\d{3})*[.,]\d{2}(?!\d)")


def checkout_total_is_zero(text, title: str = "") -> bool | None:
    """Epic's checkout total: True when nothing is to pay, False for any price, None when no total is shown."""
    low = str(text or "").lower()
    at = low.rfind("total")
    if at >= 0:
        window = low[at + 5:at + 80]
        # Digits win over the word "free", so "Total (VAT free) €19.99" is never read as free.
        number = re.search(r"\d[\d.,   ]*", window)
        if number:
            return not re.sub(r"\D", "", number.group(0)).strip("0")
        return True if "free" in window else None
    # Epic's newer checkout shows a free item with its price only, no total line; a title can carry digits too.
    if title:
        low = low.replace(str(title).lower(), " ")
    amounts = AMOUNT_RE.findall(low)
    if not amounts:
        return None
    return all(not re.sub(r"\D", "", amount).strip("0") for amount in amounts)


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
        # Games whose checkout puzzle you were already asked to solve this run, so a second one does not ask again.
        self._checkout_asked: set[str] = set()
        # Games whose checkout check did not clear this run, so it is not waited for twice; the puzzles among them stop the run.
        self._checkout_stuck: set[str] = set()
        self._checkout_puzzles: set[str] = set()
        # Puzzles that went to you over VNC and vanished with no answer, per game: solved or expired, the order decides.
        self._checkout_handed: dict[str, int] = {}
        # When each game's giveaway ends (by slug), and that of the game being claimed, for a puzzle on its last chance.
        self._game_ends: dict[str, datetime | None] = {}
        self._ends_now: datetime | None = None
        self._claiming_url = ""
        self._from_list = False
        # A meeting in noVNC claims only the kept games; with you there every puzzle is yours at once.
        self._meeting = False
        self._with_you = False
        # Who watched noVNC when you were last asked, so only a later connection counts as you coming.
        self._seen_at_prompt: set | None = None

    async def run(self, extra_games: list | None = None, booked_items: list | None = None, with_you: bool = False) -> None:
        """Main entry point: detect free games and claim them, or only the games kept for a meeting in noVNC."""
        logger.debug("Starting Epic Games claiming flow")
        if booked_items is not None:
            self._meeting, self._with_you = True, with_you
            # You connected, so the prompt nobody answered earlier in this run does not hold back the sign-in or this one.
            if with_you:
                mark_answered(self.store_name)
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

            if booked_items is not None:
                held = 0
                for item in booked_items:
                    url = item["data"]
                    self._game_ends[url.rstrip("/").split("/")[-1]] = item["ends"]
                    if item.get("platform"):
                        self._platform_labels[url] = item["platform"]
                    # A puzzle nobody answered at this meeting either: only a game kept on its last run is still opened.
                    if waits_for_nobody(self.store_name) and not item["last_chance"]:
                        held += 1
                        self.notify_games.append({"title": item["title"], "url": url, "status": "failed:captcha"})
                        continue
                    await self._claim_game(url, from_list=item.get("from_list", False))
                self._captcha_held(held, "game")
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
            # Mobile games have no tile on the free games list, so they are opened by their address.
            todo = [(game["url"], game["url"] not in self._platform_labels) for game in free_games]
            self._game_ends = game_ends(free_games, extra_games)
            handled = {url.rstrip("/").split("/")[-1] for url, _ in todo}
            for game in (extra_games or []):
                if game["final_url"].rstrip("/").split("/")[-1] in handled:
                    logger.debug("[GamerPower] '%s' is on Epic's own list, already handled.", game.get("title"))
                    continue
                logger.info("🎮 [GamerPower] '%s' → Epic Games", game.get("title", "Unknown"))
                todo.append((game["final_url"], False))
            for url, from_list in todo:
                await self._claim_game(url, from_list=from_list)

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

    async def _pick_saved_account(self, wait: int = 1) -> bool:
        """Click your account on Epic's saved-account list, looking for it up to `wait` times a second apart."""
        if not await self._cdp_click_found(saved_account_finder(cfg.eg_email), "saved account",
                                           allow_script=True, tries=wait):
            return False
        logger.info("Epic asked which saved account to continue with, picked yours.")
        return True

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
            logged_in = False
            # Epic's login page often only asks which saved account to continue with, nothing to wake you for.
            if await self._pick_saved_account(wait=10):
                # The VNC wait sends its notice before it first looks, so the pick gets its redirect to the store first.
                for _ in range(15):
                    await self.sleep(2)
                    logged_in = await _is_logged_in()
                    if logged_in:
                        break
            if not logged_in:
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
        account_choice = False
        other_account_tried = False
        # One automatic finish after a check you cleared; another check means Epic does not trust this sign-in (#100).
        resumed_after_check = False
        for attempt in range(3):
            # A prompt nobody answered means nobody will answer the next two either.
            if waits_for_nobody(self.store_name):
                logger.warning("Epic is waiting for you, so the remaining attempts are skipped.")
                needs_you(self.store_name, 0, "game")
                return False
            # The first try is routine after a session ran out, only a further one means something is in the way.
            (logger.info if attempt == 0 else logger.warning)("Not signed in – attempting automated login (attempt %d/3)…", attempt + 1)

            if attempt > 0:
                await self.page.get("https://store.epicgames.com/")
                await self.sleep(3)

            await self._navigate_organically_to_login()
            await self.sleep(3)

            await self._do_stealth_login()

            otp_tried = 0
            backup_tried = False
            unpicked = 0
            # Wait loop to detect auth completion or interstitial
            for wait_sec in range(120):
                try:
                    curr_url = str(await self.page.evaluate("window.location.href") or self.page.url)
                except Exception:
                    curr_url = str(self.page.url)

                # We know auth is complete when Epic redirects us back to the store domain.
                if url_has_allowed_host(curr_url, "store.epicgames.com"):
                    break

                # Epic's saved-account list, also on the MFA address, which the code screen check would take for itself.
                low_url = curr_url.lower()
                on_switch = "/id/login/switch-account" in low_url
                listed = on_switch or ("/id/login/mfa" in low_url and await self.page.evaluate(HAS_SAVED_ACCOUNTS_JS))
                if listed and await self._pick_saved_account():
                    continue
                # Still up after a few seconds: none of the saved accounts is yours.
                if on_switch:
                    unpicked += 1
                    if unpicked >= 5:
                        # EG_EMAIL and EG_PASSWORD are at hand, so the bot signs in as a person would instead of asking you.
                        if not other_account_tried and await self._cdp_click_found(
                                USE_ANOTHER_ACCOUNT, "use another account", allow_script=True, tries=3):
                            other_account_tried, unpicked = True, 0
                            await self.sleep(3)
                            await self._do_stealth_login()
                            continue
                        account_choice = True
                        break

                # 2FA code screen: fill in the TOTP if EG_OTP_KEY is set, otherwise hand it to the user; a check on it comes first.
                if await self._mfa_prompt_present() and not await self._human_challenge_present():
                    if not cfg.eg_otp_key or otp_tried >= OTP_KEY_ATTEMPTS:
                        # Seed gone or absent: one recovery code, then it is over to you; never one after a check (#100).
                        if not backup_tried and not resumed_after_check and await self._fill_backup_code():
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

                # A check is yours to clear, and then the bot finishes the sign-in itself, once per run.
                if wait_sec >= 4 and await self._human_challenge_present():
                    logger.warning("Captcha / security challenge detected during Epic login.")
                    if resumed_after_check:
                        # Another check after the bot finished once: it may still pass by itself, but it is never asked twice.
                        if await self._wait_out_challenge("Epic", store_key=self.store_name, ask=False):
                            continue
                    elif await self._wait_out_challenge("Epic", store_key=self.store_name) and await self._resume_after_check():
                        resumed_after_check = True
                        continue
                    challenge_blocked = True
                    break

                await self.sleep(1)

            # The wait loop can also run out of time with the code screen still up (issue #46).
            if not (mfa_manual or challenge_blocked) and await self._mfa_prompt_present():
                mfa_manual = True

            # A code screen or a captcha is yours to finish, so the page is left exactly as it stands.
            if mfa_manual or challenge_blocked or account_choice:
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
        elif account_choice:
            reason = " (saved account to pick)"
        else:
            reason = " after 3 attempts"
        logger.warning("Automated Epic login failed%s – requesting manual login via VNC.", reason)
        if mfa_manual:
            custom_msg = self._vnc_notice(
                "Epic Games: 2FA code needed",
                "Enter the 6-digit code Epic sent to your email or phone (or from your authenticator). "
                "If a 'One more step' security check appears, complete it too.",
            )
        elif account_choice:
            custom_msg = self._vnc_notice(
                "Epic Games: pick your account",
                "Epic asks which saved account to continue with, none of them matches EG_EMAIL and signing in with "
                "another account did not finish. Pick yours in the browser.",
            )
        else:
            custom_msg = self._vnc_notice(
                "Epic Games: login needs you",
                "A captcha or security challenge is blocking automated sign-in. Open the browser, solve it and finish logging in. "
                "If Epic says 'Incorrect response. Please refresh the page.', click the reload arrow in the browser window "
                "and sign in again.",
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
                logger.debug("No code boxes on the page, so no code was typed.")
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
        # No text fallback: "Sign in" also matches "Sign in with Apple" (#72); #continue is the e-mail step's button (#100).
        button = await self.page.select("#sign-in", timeout=5) or await self.page.select("#continue", timeout=1)
        if not button:
            logger.debug("Neither the sign-in nor the continue button came back after the check.")
            return False
        logger.info("Human check cleared, finishing the sign-in.")
        await button.click()
        await self.sleep(6)
        return True

    async def _resume_after_check(self) -> bool:
        """Finish a sign-in a human check interrupted, once: the step it stopped, or a reload when Epic asks for one (#100)."""
        if url_has_allowed_host(str(await self.page.evaluate("window.location.href") or ""), "store.epicgames.com"):
            return True
        stale = False
        # Epic's answer to the check can come a moment after the check itself is gone.
        for _ in range(4):
            if stale := bool(await self.page.evaluate(STALE_ANSWER_JS)):
                break
            await self.sleep(1)
        logger.debug("After the check Epic shows: %s; refresh asked: %s", await self._mfa_error_text() or "no message", stale)
        if stale:
            await self.page.reload(ignore_cache=False)
            await self.sleep(4)
        if await self.page.evaluate(HAS_SAVED_ACCOUNTS_JS):
            return True
        if await self._mfa_prompt_present():
            # Without EG_OTP_KEY the code is yours, so the screen is left alone for you.
            if cfg.eg_otp_key and await self._fill_totp():
                logger.info("Human check cleared, entering a fresh code.")
            return True
        # The form goes through the bot's own steps again: they skip what is filled and type what is missing (#100).
        if await self.page.select("#email, #password", timeout=5):
            logger.info("Human check cleared, signing in again.")
            await self._do_stealth_login()
            return True
        return await self._press_sign_in()

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
                is_free_now, free_until = False, ""
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
                                    is_free_now, free_until = True, end
                            except (ValueError, TypeError):
                                # If dates are unparseable, trust the discount
                                is_free_now, free_until = True, ""

                if not is_free_now:
                    continue

                # Build the store URL from available slug fields
                url = self._build_game_url(el)
                if url and not any(g["url"] == url for g in free_games):
                    title = el.get("title", "Unknown")
                    free_games.append({"url": url, "title": title, "ends": free_until})

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

    async def _click_game_tile(self, url: str) -> bool:
        """Open a game from its tile with the mouse, as a person would; False when there is no tile to click."""
        slug = url.rstrip("/").split("/")[-1]
        finder, label = tile_finder(slug), f"tile of {slug}"
        try:
            if not await self._cdp_click_found(finder, label):
                await self.page.get(URL_CLAIM)
                await self.sleep(3)
                if not await self._cdp_click_found(finder, label):
                    logger.debug("No tile for '%s' on the free games list.", slug)
                    return False
            for _ in range(10):
                if str(await self.page.evaluate("location.pathname")).rstrip("/").lower().endswith("/" + slug.lower()):
                    logger.debug("Opened '%s' from its tile.", slug)
                    return True
                await self.sleep(0.5)
        except Exception as exc:
            logger.debug("Could not open '%s' from its tile: %s", slug, exc)
            return False
        logger.debug("The tile of '%s' did not lead to its page.", slug)
        return False

    async def _open_game_page(self, url: str, ask: bool = True, from_list: bool = False) -> bool:
        """Open a game page and get past a Cloudflare check; False when the check stayed."""
        if not (from_list and await self._click_game_tile(url)):
            logger.debug("Opening %s by its address.", url)
            await self.page.get(url)
        await self.sleep(4)
        if not await self._human_challenge_present():
            return True
        # Cloudflare reloads the page itself once its check passes, so there is no second load here.
        return await self._wait_out_challenge("Epic", store_key=self.store_name, ask=ask)

    async def _confirm_in_library(self, url: str, tries: int = 3) -> bool:
        """Re-read the product page after checkout, only an owned page proves the claim landed."""
        for attempt in range(tries):
            if attempt:
                # Epic's library can take a few seconds to catch up with the order.
                await self.sleep(6)
            try:
                # A check nobody cleared comes back on every load, so the page is not loaded again.
                if not await self._open_game_page(url):
                    return False
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

    async def _checkout_challenge_reason(self) -> str:
        """What reads as a human check on the page, or inside the checkout frame where the page check cannot look."""
        reason = await self._human_challenge_reason()
        if reason:
            return reason
        try:
            frame_id = self._find_purchase_frame(await self.page.send(uc.cdp.page.get_frame_tree()))
            if not frame_id:
                return ""
            ctx_id = await self.page.send(
                uc.cdp.page.create_isolated_world(frame_id=frame_id, grant_univeral_access=True)
            )
            found = str(await self._eval_in_frame(ctx_id, CHALLENGE_JS) or "")
            return f"checkout frame, {found}" if found else ""
        except Exception as exc:
            logger.debug("Could not look for a human check in the checkout frame: %s", exc)
            return ""

    async def _checkout_challenge_present(self) -> bool:
        """A human check on the page or in the checkout frame."""
        return bool(await self._checkout_challenge_reason())

    async def _captcha_answer_lengths(self) -> list:
        """How long the captcha answer is on the page and in the checkout frame, for the log."""
        lengths = []
        try:
            lengths.append(await self.page.evaluate(CAPTCHA_ANSWER_JS))
            frame_id = self._find_purchase_frame(await self.page.send(uc.cdp.page.get_frame_tree()))
            if frame_id:
                ctx_id = await self.page.send(
                    uc.cdp.page.create_isolated_world(frame_id=frame_id, grant_univeral_access=True)
                )
                lengths.append(await self._eval_in_frame(ctx_id, CAPTCHA_ANSWER_JS))
        except Exception as exc:
            logger.debug("Could not read the captcha answer field: %s", exc)
        return lengths

    def _book(self, title: str, url: str, ends: datetime | None, last_chance: bool) -> bool:
        """Keep a game whose puzzle nobody solved for a meeting with you in noVNC; False when there is no time for one."""
        viewers = vnc_viewers()
        if self._meeting or not url or viewers is None:
            # Outside the container nobody can be seen opening noVNC, so there is no meeting to wait for.
            if viewers is None:
                logger.debug("No noVNC to watch here, so '%s' is not kept for a meeting.", title)
            return False
        until = booking_until(ends, last_chance)
        if until is None:
            logger.debug("No noVNC meeting for '%s': %s.", title,
                         "a single run does not wait" if next_run() is None else f"too little time is left (ends {ends})")
            return False
        seen = self._seen_at_prompt if self._seen_at_prompt is not None else viewers
        # The tile click and the platform tag come back at the meeting, so the game opens the way it did today.
        if book(self.store_name, {"title": title, "data": url, "ends": ends, "last_chance": last_chance, "until": until,
                                  "seen": set(seen), "from_list": self._from_list, "platform": self._platform_labels.get(url)}):
            logger.info("'%s' waits for you in noVNC until %s: once you connect, the bot opens its checkout again.",
                        title, utc_text(until))
        return True

    async def _clear_checkout_challenge(self, title: str) -> bool:
        """Wait out a checkout check that passes by itself; a puzzle waits for the next run, or for you with EG_CAPTCHA_ASK."""
        if title in self._checkout_stuck:
            logger.debug("The checkout check for '%s' already did not clear in this run.", title)
            return False
        reason = await self._checkout_challenge_reason()
        if not reason:
            return True
        logger.debug("Human check on the checkout for '%s': %s", title, reason)
        # A Cloudflare check passes by itself after about two minutes (#95); a puzzle only expires unsolved (TODO 67).
        person = challenge_needs_you(reason)
        last_chance = ends_before_next_run(self._ends_now)
        # On its last run a game gets a second ask, never a third: an expired puzzle reads as an answer (TODO 67).
        again = last_chance and self._checkout_handed.get(title, 0) < 2
        ask = (title not in self._checkout_asked or again) and (cfg.eg_captcha_ask or not person or last_chance)
        if person and self._meeting:
            # At a meeting you are there; the bot's own last try asks nobody.
            ask = self._with_you
        if person and ask:
            self._checkout_asked.add(title)
            self._seen_at_prompt = vnc_viewers() or set()
            if last_chance and not cfg.eg_captcha_ask and not waits_for_nobody(self.store_name):
                ends = self._ends_now.astimezone(timezone.utc)
                # A GamerPower end names only the day, kept at its start, so only the day is shown.
                shown = ends.strftime("%Y-%m-%d") if (ends.hour, ends.minute) == (0, 0) else ends.strftime("%Y-%m-%d %H:%M UTC")
                why = "before the next run" if next_run() else "and this single run does not wait"
                logger.info("'%s' is free only until %s, %s, so you are asked to solve its captcha.", title, shown, why)
        elif person:
            logger.debug("A puzzle in the checkout for '%s' is not handed to you (EG_CAPTCHA_ASK off, or asked once).", title)
        cleared = await self._wait_out_challenge(f"Epic ('{title}')", settle=12 if person else 180, store_key=self.store_name,
                                                 present_fn=self._checkout_challenge_present,
                                                 reason_fn=self._checkout_challenge_reason, ask=ask)
        if not cleared:
            self._checkout_stuck.add(title)
            if person:
                self._checkout_puzzles.add(title)
                # A puzzle that was yours and stayed unsolved waits for you in noVNC.
                if cfg.eg_captcha_ask or last_chance:
                    self._book(title, self._claiming_url, self._ends_now, last_chance)
        elif person and ask and self._handed_over:
            # Solved or expired (after about two minutes) look alike here; a filled answer is a solve, else the order decides.
            lengths = await self._captcha_answer_lengths()
            logger.debug("The puzzle for '%s' is gone: %s.", title, captcha_answer_note(lengths))
            if not answer_filled(lengths):
                self._checkout_handed[title] = self._checkout_handed.get(title, 0) + 1
        return cleared

    # Retry up to 2 times with exponential backoff if claiming fails
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15), reraise=True)
    async def _claim_game(self, url: str, from_list: bool = False) -> None:
        """Claim a single free game by navigating to its page and clicking through the purchase flow."""
        self._click_counts = {}
        self._from_list = from_list
        # Extract the game identifier from the URL (e.g. "fortnite" from ".../p/fortnite")
        game_id = url.rstrip("/").split("/")[-1]

        async with async_session() as session:
            obj, created = await get_or_create(
                session, store=self.store_name, user=account_key(cfg.eg_email, self.user),
                game_id=game_id, title=game_id, url=url, status="unknown",
            )
            extra = row_extra(obj)
            known = obj.status in RECHECKED
            owned_entry = {"title": obj.title or title_from_slug(game_id), "url": url, "status": "existed"}
            if known and not older_than(extra.get("page_checked"), RECHECK_DAYS):
                logger.debug("'%s' is %s, its page was read %s, not opened this run.",
                             game_id, obj.status, extra["page_checked"])
                if obj.status != "skipped:region":
                    self.notify_games.append(owned_entry)
                return
            self._ends_now = self._game_ends.get(game_id)
            self._claiming_url = url
            last_chance = ends_before_next_run(self._ends_now)
            # After a puzzle nobody answered only a known last run is opened, where a puzzle costs nothing; a single run cannot tell one.
            if waits_for_nobody(self.store_name) and not lost_before_next_run(self._ends_now) and not self._meeting:
                if known or obj.status in NOT_CLAIMABLE:
                    logger.debug("'%s' is %s, its check waits for the next run after a captcha.", game_id, obj.status)
                    if known and obj.status != "skipped:region":
                        self.notify_games.append(owned_entry)
                # With EG_CAPTCHA_ASK the game is yours anyway, so it joins your noVNC meeting unopened.
                elif not (cfg.eg_captcha_ask and self._book(title_from_slug(game_id), url, self._ends_now, False)):
                    logger.info("'%s' waits for the next run after a captcha in Epic's checkout.", title_from_slug(game_id))
                    needs_you(self.store_name, 1, "game", why=CAPTCHA_UNSOLVED)
                return
            # Each further checkout could draw another puzzle, so after one a game that needs a visit waits, unless this is its last run.
            if self._checkout_puzzles and not cfg.eg_captcha_ask and not last_chance:
                if known or obj.status in NOT_CLAIMABLE:
                    logger.debug("'%s' is %s, its check waits for the next run after a captcha.", game_id, obj.status)
                    if known and obj.status != "skipped:region":
                        self.notify_games.append(owned_entry)
                else:
                    logger.info("'%s' waits for the next run after a captcha in Epic's checkout.", title_from_slug(game_id))
                    needs_you(self.store_name, 1, "game", why=CAPTCHA_UNSOLVED)
                return
            mark_page_checked(obj)

            # A game you have, or cannot get in your region, is not worth asking you to solve a security check.
            if not await self._open_game_page(url, ask=not known, from_list=from_list):
                # From the address: an earlier run may have stored the check page's heading as the name.
                title = title_from_slug(game_id)
                if known:
                    logger.info("'%s' is behind a security check on Epic, looked at again in %d days.",
                                title, RECHECK_DAYS)
                    if obj.status != "skipped:region":
                        self.notify_games.append(owned_entry)
                else:
                    lost = lost_before_next_run(self._ends_now)
                    logger.warning("Security check on '%s' not cleared, %s.", title,
                                   "and its giveaway ends before the next run" if lost else "trying again next run")
                    self.notify_games.append({"title": title, "url": url, "status": "failed"})
                    needs_you(self.store_name, 1, "game", why=CAPTCHA_ENDS if lost else CAPTCHA_UNSOLVED)
                await session.commit()
                return

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

            # ── NEW FLOW: "Get" on the page, then "Add to library" in the checkout frame ──
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
            # A checkout that did not finish needs one look, not three reloads of a page on a session under watch.
            # A puzzle you may have solved deserves the library's full time to catch up.
            if await self._confirm_in_library(url, tries=3 if checkout_ok or title in self._checkout_handed else 1):
                logger.info("✓ Claimed '%s' successfully!", title)
                obj.status = "claimed"
                obj.updated_at = datetime.now(timezone.utc)
                notify_game["status"] = "claimed"
                unbook_title(self.store_name, title)
            else:
                # A puzzle handed to you that vanished with no answer and no order behind it expired unsolved.
                if not checkout_ok and title in self._checkout_handed:
                    self._checkout_puzzles.add(title)
                    mark_unanswered(self.store_name)
                    self._book(title, url, self._ends_now, ends_before_next_run(self._ends_now))
                # Finishing it is the bot's job: a failed row is tried again on every run while the game is free.
                if checkout_ok:
                    logger.warning("Claim of '%s' was not confirmed by the page, trying again next run.", title)
                    obj.status = "failed:unconfirmed"
                    await self.take_screenshot(f"epic_unconfirmed_{game_id}")
                elif title in self._checkout_puzzles:
                    later = "it waits for you in noVNC" if is_booked(self.store_name, title) else "trying again next run"
                    logger.warning("A captcha in Epic's checkout stopped '%s', %s.", title, later)
                    obj.status = "failed"
                    await self.take_screenshot(f"epic_failed_{game_id}")
                else:
                    logger.error("Could not claim '%s', trying again next run.", title)
                    obj.status = "failed"
                    await self.take_screenshot(f"epic_failed_{game_id}")
                notify_game["status"] = "failed"
                if is_booked(self.store_name, title):
                    # It waits for you in noVNC, listed apart in the summary; the meeting reports how it ended.
                    self.notify_games.remove(notify_game)
                # A captcha left unsolved is the one part a person could do, so the summary says so.
                elif waits_for_nobody(self.store_name) or title in self._checkout_puzzles:
                    lost = lost_before_next_run(self._ends_now)
                    needs_you(self.store_name, 1, "game", why=CAPTCHA_ENDS if lost else CAPTCHA_UNSOLVED)

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
                            if (document.querySelector('iframe[src*="/checkout"]')) return 'purchase iframe';

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
                                ctx_id, FIND_ADD_TO_LIBRARY, "add to library", allow_script=last_try,
                                frame_id=iframe_frame_id):
                            logger.debug("✓ Found and clicked 'Add to library' inside iframe.")
                            add_clicked = True
                            await self.sleep(2)

                        if not accepted and await self._cdp_click_in_frame(
                                ctx_id, FIND_I_ACCEPT, "i accept", allow_script=last_try, frame_id=iframe_frame_id):
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
                                  tries: int = 1, *, frame_id) -> bool:
        """Click inside the cross-origin checkout frame with the mouse: the spot inside it plus where it sits on the page."""

        async def run_js(expression):
            return await self._eval_in_frame(ctx_id, expression)

        async def read():
            return await self._read_box(run_js, finder)

        async def on_owner(function, *args):
            # The <iframe> holding this very frame: a selector once picked Epic's hidden hCaptcha frame instead.
            try:
                backend_id, _ = await self.page.send(uc.cdp.dom.get_frame_owner(frame_id))
                owner = await self.page.send(uc.cdp.dom.resolve_node(backend_node_id=backend_id))
                result, _ = await self.page.send(uc.cdp.runtime.call_function_on(
                    function, object_id=owner.object_id, return_by_value=True,
                    arguments=[uc.cdp.runtime.CallArgument(value=a) for a in args]))
                return result.value if result else None
            except Exception as e:
                logger.debug("Could not find the checkout frame on the page: %s", e)
                return None

        async def frame():
            raw = await on_owner(FRAME_ORIGIN_JS)
            return json.loads(raw) if isinstance(raw, str) else None

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
            if await on_owner(FRAME_HIT_JS, px, py) is True:
                return px, py
            logger.debug("Point (%d, %d) is not on the checkout frame at %s.", px, py, f)
            return None

        return await self._click_until_taken(read, run_js, wheel_point, to_page, finder, f"{label} (checkout frame)",
                                             tries=tries, allow_script=allow_script)

    # ------------------------------------------------------------------
    # Purchase iframe handling (via CDP), OLD flow
    # ------------------------------------------------------------------

    async def _handle_purchase_iframe(self, title: str) -> bool | str:
        """Complete the purchase inside Epic's payment iframe, or answer "not-free" when its total is not zero.

        Epic's checkout is inside its own frame (store.epicgames.com/checkout), which page
        JavaScript does not reach into, so we use CDP:
          1. Find the iframe's unique FrameId from the browser's frame tree
          2. Create an isolated JavaScript execution context inside that frame
          3. Run our button-clicking scripts inside that context
        """
        try:
            # Wait for the checkout frame in the frame tree: the page no longer wraps it in #webPurchaseContainer.
            for attempt in range(10):
                iframe_frame_id = self._find_purchase_frame(await self.page.send(uc.cdp.page.get_frame_tree()))
                if iframe_frame_id:
                    break
                await self.sleep(1)
            else:
                logger.warning("No purchase iframe appeared for '%s'", title)
                return False

            await self.sleep(2)  # let iframe content load

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
                free = checkout_total_is_zero(text, title)
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
                clicked = await self._cdp_click_in_frame(ctx_id, FIND_PLACE_ORDER, "place order", allow_script=attempt == 7,
                                                         frame_id=iframe_frame_id)
                if clicked:
                    logger.debug("Clicked 'Place Order' for '%s'", title)
                    break
                await self.sleep(2)

            await self.sleep(2)

            # Handle "I Accept" / "I Agree" button (EU accounts only)
            await self._cdp_click_in_frame(ctx_id, FIND_I_ACCEPT, "i accept", allow_script=True, tries=2,
                                           frame_id=iframe_frame_id)

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
            if "payment" in url or "purchase" in url or "/checkout" in url:
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


async def claim_epic_booked(items: list, with_you: bool) -> dict:
    """A meeting in noVNC for the games whose puzzle nobody solved: with you there, or the bot's own last try."""
    claimer = EpicGamesClaimer()
    await claimer.run(booked_items=items, with_you=with_you)
    return {"store": "Epic Games", "user": claimer.user, "games": claimer.notify_games}
