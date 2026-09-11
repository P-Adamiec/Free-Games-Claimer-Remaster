"""GamerPower store module.

Fetches giveaways from GamerPower API, skips any games we've already
claimed in other stores (Steam, Epic, GOG), and processes indirect
redemption sites (Fanatical, Alienware Arena, Itch.io, IndieGala)
according to user configuration.
"""
import json
import re
import asyncio
import httpx
from urllib.parse import urlparse

from sqlalchemy import select

from src.core.claimer import BaseClaimer, OTP_KEY_ATTEMPTS, mask_account
from src.core.config import cfg
from src.core.database import async_session, ClaimedGame, get_or_create
from src.core.run_state import needs_you, waits_for_nobody
from src.core.selection import is_store_active
from src.core.url_security import url_has_allowed_host
import logging
from src.core.claimer import filenamify

logger = logging.getLogger("fgc.gamerpower")

GAMERPOWER_API_URL = "https://www.gamerpower.com/api/giveaways"

# Full games and Early Access are worth claiming; DLC needs a per-game account (GP_CLAIM_DLC).
CLAIMABLE_TYPES = ("game", "early access")

# Host to store key. Order matters only for readability, hosts never overlap.
_STORE_HOSTS = (
    ("steam", "store.steampowered.com"),
    ("epic", "epicgames.com"),
    ("gog", "gog.com"),
    ("fanatical", "fanatical.com"),
    ("alienware", "alienwarearena.com"),
    ("itchio", "itch.io"),
    ("indiegala", "indiegala.com"),
    ("ubisoft", "ubisoft.com"),
)

# Stores with a module of their own, which finds these giveaways without GamerPower's help.
COVERED_ELSEWHERE = {"ubisoft": "ubisoft"}

# Only consulted when the resolved address gives nothing away.
_INSTRUCTION_HINTS = (
    ("indiegala", "indiegala"),
    ("alienware", "alienware"),
    ("fanatical", "fanatical"),
    ("itchio", "itch.io"),
)


def wanted_types(claim_dlc: bool) -> set[str]:
    """Giveaway types worth processing, lowercased for comparison."""
    types = set(CLAIMABLE_TYPES)
    if claim_dlc:
        types.add("dlc")
    return types


def is_wanted(entry: dict, claim_dlc: bool) -> bool:
    """True when this giveaway is a type we try to claim."""
    return str((entry or {}).get("type") or "").strip().lower() in wanted_types(claim_dlc)


def classify_target(final_url: str, instructions: str = "") -> str:
    """Which store a giveaway ends at, by host first and instructions only as a fallback."""
    for store, host in _STORE_HOSTS:
        if url_has_allowed_host(final_url, host, allow_subdomains=True):
            return store

    text = (instructions or "").lower()
    for store, hint in _INSTRUCTION_HINTS:
        if hint in text:
            return store
    return "unknown"

# Two-factor screens differ per site, so the code box is found by what it is, not by its name.
OTP_FIELD = "[data-otp-field]"

OTP_STATE_JS = r"""
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const hint = /code|otp|totp|2fa|two.?factor|verif/i;
        const fields = [...document.querySelectorAll('input')].filter(vis)
            .filter(i => ['text', 'tel', 'number', ''].includes((i.type || '').toLowerCase()));
        const labelled = fields.filter(i => hint.test(
            [i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.autocomplete].join(' ')));
        const body = document.body ? (document.body.innerText || '') : '';
        const target = labelled[0] || (fields.length === 1 ? fields[0] : null);
        if (target) target.setAttribute('data-otp-field', '1');
        return JSON.stringify({
            labelled: labelled.length,
            visibleTextFields: fields.length,
            hasPassword: !!document.querySelector('input[type="password"]'),
            talksAboutIt: /two.?factor|verification code|authenticator/i.test(body),
        });
    })()
"""


# Fanatical signs in through a modal opened from the header; /en/login is a 404, and the
# first visible text box on any page is the search field, not the email one.
FAN_SIGNED_OUT_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        return [...document.querySelectorAll('button, a')].filter(vis)
            .some(b => /^sign in$/i.test((b.textContent || '').trim()));
    })()
"""

FAN_OPEN_LOGIN_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const b = [...document.querySelectorAll('button, a')].filter(vis)
            .find(x => /^sign in$/i.test((x.textContent || '').trim()));
        if (!b) return false;
        b.click();
        return true;
    })()
"""

FAN_MARK_FIELDS_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const mail = document.querySelector('#emailInput');
        const pass = [...document.querySelectorAll('#passwordInput, input[type="password"]')].find(vis);
        if (mail) mail.setAttribute('data-fgc-mail', '1');
        if (pass) pass.setAttribute('data-fgc-pass', '1');
        return !!mail && !!pass;
    })()
"""

# The header carries a Sign in button too, so the submit is looked up inside the modal.
FAN_SUBMIT_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const pass = document.querySelector('[data-fgc-pass]');
        if (!pass) return false;
        let scope = pass;
        for (let i = 0; i < 6 && scope.parentElement; i++) {
            scope = scope.parentElement;
            if (scope.tagName === 'FORM' || /modal|dialog/i.test(String(scope.className))) break;
        }
        const b = [...scope.querySelectorAll('button')].filter(vis)
            .find(x => /^sign in$/i.test((x.textContent || '').trim()));
        if (!b) return false;
        b.click();
        return true;
    })()
"""


def fanatical_game_id(url: str) -> str:
    """Fanatical's own identifier for a giveaway: the slug behind /game/ or /giveaway/."""
    match = re.search(r"/(?:game|giveaway|bundle)/([a-z0-9-]+)", str(url or "").lower())
    return match.group(1) if match else ""


# An owned itch.io game carries a purchase banner; a page you do not own carries none.
ITCH_OWNED_JS = """
    (() => !!document.querySelector('.purchase_banner, .ownership_reason'))()
"""


def download_only_status(first_time: bool) -> str:
    """What the summary says about a giveaway itch.io only hands out as a file."""
    # Said plainly once, then parked under a "skipped" status the summary filter hides.
    return "download only, nothing to claim 📥" if first_time else "skipped:download-only"


def itch_game_id(url: str) -> str:
    """Itch.io's own identifier for a game: creator host plus slug, e.g. `dev.itch.io/game`."""
    parsed = urlparse(str(url or ""))
    slug = parsed.path.strip("/").split("/")[0]
    if not parsed.netloc or not slug:
        return ""
    return f"{parsed.netloc.lower()}/{slug.lower()}"


def needs_otp(state: dict) -> bool:
    """True when the page is asking for an authenticator code rather than a password."""
    state = state or {}
    if state.get("labelled"):
        return True
    # No named field: only trust a page that says so and offers exactly one box to type in.
    return bool(state.get("talksAboutIt")
                and state.get("visibleTextFields") == 1
                and not state.get("hasPassword"))


def login_help_message(label: str, code_screen: bool, tried_backup: bool = False) -> str:
    """What to tell the user when a side store will not sign in on its own."""
    if not code_screen:
        return f"{label} did not accept the automated sign-in. Open the browser and finish it."

    lines = [f"{label} is asking for your authenticator code. Open the browser and type it."]
    if tried_backup:
        lines.append("A recovery code was spent on this attempt and did not get through either.")
    return " ".join(lines)


# One claimer serves four sites, so what the person reads is not what the run state counts by.
SIDE_STORE_KEYS = {"Itch.io": "itchio", "Fanatical": "fanatical",
                   "IndieGala": "indiegala", "Alienware Arena": "alienware"}


def side_store_key(label: str) -> str:
    """The store key behind a label shown to the user."""
    return SIDE_STORE_KEYS.get(label, (label or "").lower())


# Some giveaway hosts answer a bare client with a different redirect than a browser gets.
_ROUTE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/120.0.0.0 Safari/537.36")

# A giveaway that lands on a storefront banner instead of a product page cannot be claimed.
_PRODUCT_MARKERS = {"steam": ("/app/", "/sub/"), "epic": ("/p/", "/bundles/")}


def is_product_page(store: str, final_url: str) -> bool:
    """False when a giveaway lands on a storefront banner instead of a page you can claim."""
    markers = _PRODUCT_MARKERS.get(store)
    return True if not markers else any(marker in (final_url or "") for marker in markers)


def _clean_title(title: str) -> str:
    """Drop the "(Steam) Key Giveaway" tails GamerPower puts on its titles."""
    for pattern in (r'(?i)\s*\(\s*steam\s*\)\s*(?:key\s*)?giveaway\s*$',
                    r'(?i)\s*(?:steam\s*)?key\s*giveaway\s*$',
                    r'(?i)\s*giveaway\s*$',
                    r'(?i)\s*\(\s*steam\s*\)\s*key\s*$',
                    r'(?i)\s*steam\s*key\s*$'):
        title = re.sub(pattern, '', title)
    return title.strip()


async def _claimed_titles() -> set:
    """Every title already claimed anywhere, so the same game is not chased twice."""
    titles = set()
    async with async_session() as session:
        stmt = select(ClaimedGame).where(ClaimedGame.status.in_(["claimed", "existed"]))
        result = await session.execute(stmt)
        for db_game in result.scalars().all():
            titles.add(BaseClaimer._normalize_title(db_game.title))
    return titles


async def _resolve_target(game: dict) -> tuple[str, str]:
    """Follow a giveaway's redirect and work out which store it ends at. No browser."""
    giveaway_url = game.get("giveaway_url", "")
    final_url = giveaway_url.lower()
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": _ROUTE_UA},
                                     timeout=20) as client:
            res = await client.get(giveaway_url)
            final_url = str(res.url).lower()
    except Exception as e:
        logger.debug("Failed to pre-resolve URL %s: %s", giveaway_url, e)

    instructions = (game.get("instructions", "") or "").lower()
    target_store = classify_target(final_url, instructions)
    if target_store != "unknown" and classify_target(final_url) == "unknown":
        logger.debug("[GamerPower] '%s' routed to %s by its instructions, the URL gave nothing.",
                     game.get("title", "Unknown"), target_store)
    return target_store, final_url


async def discover_giveaways() -> dict:
    """Find GamerPower's giveaways and sort them by the store they end at. Never raises.

    This is the only part that talks to GamerPower: the stores themselves do the claiming.
    """
    try:
        logger.debug("Fetching giveaways from GamerPower API")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(GAMERPOWER_API_URL)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list):
            logger.debug("GamerPower returned non-list data")
            return {}

        # Filter before resolving redirects: every entry kept costs one extra HTTP request.
        kept = [item for item in data if is_wanted(item, cfg.gp_claim_dlc)]
        if len(data) - len(kept):
            logger.debug("Skipped %d giveaway(s) of an unwanted type (GP_CLAIM_DLC=%s).",
                         len(data) - len(kept), cfg.gp_claim_dlc)

        games = [{
            "title": _clean_title(item.get("title", "Unknown")),
            "url": item.get("open_giveaway_url", ""),
            "giveaway_url": item.get("open_giveaway_url", ""),
            # Carried through so routing can fall back on them, see classify_target().
            "instructions": item.get("instructions", "") or "",
            "type": item.get("type", "") or "",
            "platforms": item.get("platforms", "") or "",
        } for item in kept]

        logger.debug("GamerPower API returned %d giveaway(s)", len(games))
        db_titles = await _claimed_titles()
        unique_gp = []
        for gp in games:
            norm = BaseClaimer._normalize_title(gp["title"])
            if any(s in norm or norm in s for s in db_titles):
                logger.debug("GamerPower duplicate (already processed globally): %s", gp["title"])
            else:
                unique_gp.append(gp)

        if not unique_gp:
            logger.info("No unique GamerPower giveaways found.")
            return {}

        links = [f"  • [bold cyan]{g['title']}[/bold cyan] 🔗 {g.get('giveaway_url', '')}" for g in unique_gp]
        logger.info("🎮 [bold magenta]GamerPower: %d extra game(s):[/bold magenta]\n%s",
                    len(unique_gp), "\n".join(links))

        routed: dict = {}
        for game in unique_gp:
            store, final_url = await _resolve_target(game)
            game["final_url"] = final_url
            if not is_product_page(store, final_url):
                logger.info("⏭️ [GamerPower] '%s' → %s URL is not a game page (%s), skipping",
                            game["title"], store.title(), final_url)
                continue
            if store in COVERED_ELSEWHERE:
                logger.info("⏭️ [GamerPower] '%s' → %s, already covered by the '%s' store, skipping",
                            game["title"], store.title(), COVERED_ELSEWHERE[store])
                continue
            routed.setdefault(store, []).append(game)
        logger.debug("GamerPower routing: %s",
                     {store: len(items) for store, items in sorted(routed.items())})
        return routed
    except Exception:
        logger.exception("Could not read the GamerPower giveaways")
        return {}


class GamerPowerClaimer(BaseClaimer):
    store_name = "gamerpower"
    # The browser profile keeps its original folder name so existing side-store logins survive.
    profile_name = "base"

    def __init__(self) -> None:
        super().__init__()
        self.user = "GamerPower"
        self._fanatical_games = []
        # Itch.io signs in once per run, not once per giveaway.
        self._itch_session_ok = False

    async def run(self, routed: dict | None = None) -> None:
        """Claim the giveaways that land on sites with no store module of their own."""
        routed = routed or {}
        try:
            work = []
            for store in ("fanatical", "alienware", "itchio", "indiegala", "unknown"):
                label, _ = self._side_store(store)
                for game in routed.get(store, []):
                    if self._side_store_selected(store):
                        work.append((store, game))
                    else:
                        logger.info("⏭️ [GamerPower] '%s' → %s, not part of this run, skipping",
                                    game.get("title", "Unknown"), label)
            if not work:
                return

            # One browser for all of them: these sites are claimed page by page, not in batches.
            await self.start_browser(force_headful=True)
            for store, game in work:
                # A site whose prompt nobody answered is left alone: opening its sign-in page
                # once per giveaway would cost hours and lower the session's standing there.
                if waits_for_nobody(store):
                    logger.info("⏭️ [GamerPower] '%s' → %s is waiting for you, skipping",
                                game.get("title", "Unknown"), self._side_store(store)[0])
                    needs_you(store)
                    continue
                await self._process_side_store(store, game)
        except Exception as exc:
            logger.exception("Fatal error in GamerPower")
            if cfg.notify_errors:
                await self.notify(f"gamerpower failed: {exc}")
        finally:
            # Summary notifications deferred to main.py
            await self.close_browser()

    @staticmethod
    def _side_store_selected(target_store: str) -> bool:
        """Side stores need an account, so each one runs only when STORES names it."""
        if target_store == "unknown":
            # Opening a site nobody mapped is not supported yet, whatever GP_UNKNOWN_STORES says.
            if cfg.gp_unknown_stores:
                logger.warning("Opening unknown sites is not supported yet, GP_UNKNOWN_STORES stays off.")
            return False
        return is_store_active(target_store)

    def _side_store(self, target_store: str) -> tuple[str, object]:
        """Label and handler for a site with no store module of its own."""
        table = {
            "fanatical": ("Fanatical giveaway", self._claim_fanatical_game),
            "alienware": ("Alienware Arena", self._claim_alienware_game),
            "itchio": ("Itch.io giveaway", self._claim_itchio_game),
            "indiegala": ("IndieGala giveaway", self._claim_indiegala_game),
        }
        # No handler for an unknown site, all we can do is open it for a human.
        return table.get(target_store, ("Unknown site", None))

    async def _process_side_store(self, target_store: str, game: dict) -> None:
        """Claim one giveaway on a side site, in the browser this claimer already opened."""
        title = game.get("title", "Unknown")
        label, handler = self._side_store(target_store)

        try:
            if handler:
                logger.info("🎮 [GamerPower] '%s' → %s", title, label)
                await handler(game)
            else:
                domain = urlparse(game.get("final_url", "")).netloc.replace("www.", "")
                logger.info("❓ [GamerPower] '%s' → Unknown site (%s). Opening for manual review via VNC.",
                            title, domain)
                await self.page.get(game.get("giveaway_url", ""))
                await self.sleep(10)
        except Exception:
            logger.exception("[GamerPower] Error processing '%s'", title)

    async def _itch_logged_in(self) -> bool:
        """Signed in when itch.io offers a logout link and no login link. Verified live."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const hrefs = [...document.querySelectorAll('a[href]')]
                        .map(a => a.getAttribute('href') || '');
                    return hrefs.some(h => h.includes('/logout')) && !hrefs.some(h => h.includes('/login'));
                })()
            """))
        except Exception as e:
            logger.debug("[Itch.io] Sign-in check failed: %s", e)
            return False

    async def _ig_logged_in(self) -> bool:
        """Signed in when IndieGala offers a way out and none in. Verified live signed out."""
        try:
            raw = await self.page.evaluate("""
                JSON.stringify((() => {
                    const hrefs = [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href') || '');
                    const labels = [...document.querySelectorAll('a, button')]
                        .map(b => (b.textContent || '').trim().toLowerCase());
                    return {
                        logout: hrefs.some(h => h.includes('logout')),
                        login: hrefs.some(h => h.includes('/login'))
                            || labels.some(t => t === 'login' || t === 'log in' || t === 'sign in')
                    };
                })())
            """)
            state = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[IndieGala] Sign-in check failed: %s", e)
            return False
        # Signed out always offers a way in, on indiegala.com and on freebies.indiegala.com alike.
        return bool(state.get("logout")) or not state.get("login")

    def _log_side_signed_in(self, label: str, account: str | None) -> None:
        """The line every store prints. `log_signed_in()` would also overwrite `self.user`,
        which this claimer keeps as the database key for all its side stores."""
        self.logger.info("🔓 [bold green]Signed in as:[/bold green] %s (%s)",
                         mask_account(account) or "unknown", label)

    async def _resubmit_login(self, label: str, email: str, password: str) -> bool:
        """Send the sign-in form again. A human check interrupts the first attempt, it does not undo it."""
        js_email = json.dumps(email)
        js_password = json.dumps(password)
        try:
            sent = await self.page.evaluate(f'''
                (() => {{
                    const vis = el => {{ const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }};
                    const inputs = [...document.querySelectorAll('input')].filter(vis);
                    const user = inputs.find(i => /email|user|login/i.test(i.name + ' ' + i.id + ' ' + i.type));
                    const pass = inputs.find(i => (i.type || '').toLowerCase() === 'password');
                    if (!user || !pass) return false;
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                    setter.call(user, {js_email});
                    user.dispatchEvent(new Event("input", {{bubbles: true}}));
                    setter.call(pass, {js_password});
                    pass.dispatchEvent(new Event("input", {{bubbles: true}}));
                    const submit = document.querySelector('button[type="submit"], input[type="submit"]')
                        || [...document.querySelectorAll('button')].filter(vis)
                            .find(b => /log ?in|sign ?in/i.test(b.textContent || ''));
                    if (!submit) return false;
                    submit.click();
                    return true;
                }})()
            ''')
        except Exception as e:
            logger.debug("[%s] Could not send the sign-in form again: %s", label, e)
            return False
        if sent:
            logger.info("[%s] The check is done, sending your sign-in again.", label)
            await self.sleep(6)
        return bool(sent)

    async def _resubmit_when_form_returns(self, label: str, email: str, password: str) -> bool:
        """Wait for the sign-in form to come back after a human check, then send it.

        The check disappears a moment before the page reloads, and one look at that moment
        finds no form at all.
        """
        for _ in range(6):
            if await self._resubmit_login(label, email, password):
                return True
            await self.sleep(3)
        logger.debug("[%s] No sign-in form came back after the check.", label)
        return False

    async def _confirm_side_login(self, label: str, check_fn, backup_codes: list | None = None,
                                 backup_file: str = "", otp_key: str | None = None,
                                 credentials: tuple | None = None) -> bool:
        """Finish a side-store login: answer a code screen if one shows, else hand over via VNC."""
        if await check_fn():
            return True

        # A captcha is not a login failure the bot can retype its way out of: Fanatical answers
        # a correct password with "we just need to check that you're a real person".
        if await self._human_challenge_present():
            logger.warning("[%s] The site is showing a human check, handing over.", label)
            if await self._wait_out_challenge(label, store_key=side_store_key(label)):
                if await check_fn():
                    return True
                # The check interrupted a sign-in that was already sent, so send it again
                # rather than asking you to type a password the bot already has.
                if credentials and await self._resubmit_when_form_returns(label, *credentials)                         and await check_fn():
                    return True

        state = {}
        tried_backup = False
        try:
            raw = await self.page.evaluate(OTP_STATE_JS)
            state = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[%s] Could not read the login screen: %s", label, e)

        if needs_otp(state):
            logger.debug("[%s] Two-factor screen detected: %s", label, state)
            for attempt in range(OTP_KEY_ATTEMPTS if otp_key else 0):
                if attempt:
                    logger.warning("[%s] The code was not accepted, trying once more with a fresh one.", label)
                if await self._fill_totp(label, otp_key) and await check_fn():
                    return True
                if not await self._code_screen_still_up(label):
                    break
            if backup_codes:
                tried_backup = await self._fill_backup_code(label, backup_codes, backup_file)
                if tried_backup and await check_fn():
                    return True
            elif not otp_key:
                logger.info("[%s] The site is asking for a two-factor code, handing over to you.", label)

        code_screen = needs_otp(state)
        msg = self._vnc_notice(
            f"{label}: 2FA code needed" if code_screen else f"{label}: login needs you",
            login_help_message(label, code_screen, tried_backup))
        if await self._wait_for_vnc_login(check_fn, custom_msg=msg, store_key=side_store_key(label)):
            return True
        logger.warning("[%s] Still not signed in, skipping this giveaway.", label)
        needs_you(side_store_key(label))
        return False

    async def _type_otp(self, label: str, code: str) -> bool:
        """Type a code into whatever box OTP_STATE_JS marked, then press the site's submit button."""
        try:
            field = await self.page.select(OTP_FIELD, timeout=8)
            if not field:
                return False
            await field.click()
            await self.sleep(0.4)
            await field.send_keys(code)
            await self.sleep(0.6)
            await self.page.evaluate("""
                (() => {
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                    const b = [...document.querySelectorAll('button, input[type="submit"]')].filter(vis)
                        .find(x => /^(log ?in|verify|continue|submit|sign ?in)$/i
                            .test(((x.innerText || x.value || '')).trim()));
                    if (b) b.click();
                })()
            """)
            await self.sleep(6)
            return True
        except Exception as e:
            logger.debug("[%s] Could not enter the two-factor code: %s", label, e)
            return False

    async def _code_screen_still_up(self, label: str) -> bool:
        """True when the code box is still there, which means the code was turned down."""
        try:
            raw = await self.page.evaluate(OTP_STATE_JS)
            return needs_otp(json.loads(raw) if isinstance(raw, str) else {})
        except Exception as e:
            logger.debug("[%s] Could not re-read the code screen: %s", label, e)
            return False

    async def _fill_totp(self, label: str, otp_key: str) -> bool:
        """Auto-enter the authenticator (TOTP) code from the store's own secret."""
        logger.debug("[%s] Entering the two-factor code from your authenticator secret.", label)
        self._last_totp = await self._fresh_totp(otp_key, self._last_totp)
        return await self._type_otp(label, self._last_totp)

    async def _fill_backup_code(self, label: str, codes: list, used_name: str) -> bool:
        """Spend one recovery code, the way epic.py and gog.py do: first unused, then remember it."""
        code = self._next_unused_code(codes, used_name)
        if not code:
            logger.warning("[%s] Every recovery code has been used already.", label)
            return False

        # Some sites keep recovery behind its own link next to the authenticator field.
        await self.page.evaluate("""
            (() => {
                const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const link = [...document.querySelectorAll('a, button')].filter(vis)
                    .find(x => /recovery|backup/i.test((x.textContent || '').trim()));
                if (link) link.click();
            })()
        """)
        await self.sleep(3)
        try:
            raw = await self.page.evaluate(OTP_STATE_JS)
            if not needs_otp(json.loads(raw) if isinstance(raw, str) else {}):
                logger.debug("[%s] No code box after opening the recovery screen.", label)
                return False
        except Exception as e:
            logger.debug("[%s] Could not read the recovery screen: %s", label, e)
            return False
        if not await self._type_otp(label, code.replace("-", "").replace(" ", "")):
            return False
        self._mark_code_used(code, used_name, codes)
        return True

    async def _clear_challenge(self, label: str) -> bool:
        """Let a captcha that shows up mid-claim be solved, instead of failing quietly."""
        if not await self._human_challenge_present():
            return True
        logger.warning("[%s] A human check appeared during the claim.", label)
        return await self._wait_out_challenge(label, store_key=side_store_key(label))

    async def _fanatical_signed_in(self) -> bool:
        """Signed in when no Sign in control is left on the page.

        Verified both ways: the button shows on /, /orders and /account while signed out and
        on none of them once signed in, where those pages show the account overview instead.
        """
        try:
            return not bool(await self.page.evaluate(FAN_SIGNED_OUT_JS))
        except Exception as e:
            logger.debug("[Fanatical] Sign-in check failed: %s", e)
            return False

    async def _fanatical_login(self, email: str, password: str) -> None:
        """Open the header modal and fill it. Fanatical has no login page, /en/login is a 404."""
        if not await self.page.evaluate(FAN_OPEN_LOGIN_JS):
            logger.debug("[Fanatical] No Sign in button to open the login modal.")
            return
        await self.sleep(5)
        if not await self.page.evaluate(FAN_MARK_FIELDS_JS):
            logger.debug("[Fanatical] The login modal did not offer both fields.")
            return
        for selector, value in (("[data-fgc-mail]", email), ("[data-fgc-pass]", password)):
            field = await self.page.select(selector, timeout=8)
            if not field:
                return
            await field.click()
            await self.sleep(0.4)
            await field.send_keys(value)
            await self.sleep(0.6)
        await self.page.evaluate(FAN_SUBMIT_JS)
        await self.sleep(8)

    async def _itch_owns_this(self) -> bool:
        """True when the open game page shows itch.io's own-this banner. Language independent."""
        try:
            return bool(await self.page.evaluate(ITCH_OWNED_JS))
        except Exception as e:
            logger.debug("[Itch.io] Ownership check failed: %s", e)
            return False

    async def _remember_itchio(self, game_id: str, title: str, url: str, status: str) -> bool:
        """Store an itch.io outcome. True when this run is the first to see that status."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="itchio", user=self.user,
                game_id=game_id, title=title, url=url, status=status,
            )
            first_time = created or obj.status != status
            obj.status = status
            await session.commit()
        return first_time

    async def _itch_run_claim(self, title: str) -> str:
        """Walk itch.io's claim chain. Returns "clicked", "download-only" or "blocked"."""
        purchase = await self.page.evaluate("""
            (() => {
                const a = document.querySelector('a.buy_btn[href], a.button.buy_btn[href]');
                return a ? a.href : '';
            })()
        """)
        if not purchase:
            logger.warning("[Itch.io] '%s' has no claim button on its page.", title)
            return "blocked"

        await self.page.get(str(purchase))
        await self.sleep(5)
        if not await self._clear_challenge("Itch.io"):
            return "blocked"
        # Only a free or pay-what-you-want game offers the direct download link. Anything
        # else wants real money, and the bot has no business there.
        went_free = await self.page.evaluate("""
            (() => {
                const b = document.querySelector('a.direct_download_btn');
                if (!b) return false;
                b.click();
                return true;
            })()
        """)
        if not went_free:
            logger.warning("[Itch.io] '%s' is not free right now, refusing to go further.", title)
            return "blocked"
        await self.sleep(6)

        # The download page carries the one control that puts the game in your library.
        clicked = await self.page.evaluate("""
            (() => {
                const clean = s => (s || '').replace(/[\\s]+/g, ' ').trim();
                const el = [...document.querySelectorAll('a, button')]
                    .filter(x => x.querySelectorAll('a, button').length === 0)
                    .find(x => /^claim( game)?$/i.test(clean(x.textContent)));
                if (!el) return false;
                el.click();
                return true;
            })()
        """)
        if not clicked:
            logger.debug("[Itch.io] '%s' offers a download but no claim control.", title)
            return "download-only"
        await self.sleep(7)
        return "clicked"

    async def _claim_fanatical_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("final_url") or game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)
        game_id = fanatical_game_id(url) or giveaway_url

        notify_game = {"title": f"{title} (Fanatical)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # The host alone is not enough: staying on the previous game's page would judge
            # this one by that page.
            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            # Check already claimed
            body_text = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
            if "already claimed" in body_text or "you have claimed" in body_text:
                logger.info("[Fanatical] '%s' already claimed.", title)
                if cfg.dryrun:
                    notify_game["status"] = "existed"
                    return
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="fanatical", user=self.user,
                        game_id=game_id, title=title, url=url, status="existed",
                    )
                    obj.status = "existed"
                    await session.commit()
                notify_game["status"] = "existed"
                return

            needs_login = await self.page.evaluate("""
                (() => {
                    const body = (document.body?.innerText || '');
                    const btns = [...document.querySelectorAll('button, a')];
                    const hasSignIn = btns.some(b => {
                        const t = (b.textContent || '').trim().toLowerCase();
                        return t === 'sign in' || t.includes('sign in to a fanatical');
                    });
                    return hasSignIn || body.includes('Create or Sign in to a Fanatical account');
                })()
            """)

            if needs_login:
                # The cookie wall covers the header, so it goes first either way.
                await self.page.evaluate("""
                    (() => {
                        const b = [...document.querySelectorAll('button, a')].find(x =>
                            (x.textContent || '').includes('Reject All Non-Essential') ||
                            (x.textContent || '').includes('Reject All'));
                        if (b) b.click();
                    })()
                """)
                await self.sleep(2)

                email = cfg.fanatical_email
                password = cfg.fanatical_password
                if email and password:
                    logger.info("[Fanatical] Logging in as %s…", mask_account(email))
                    await self._fanatical_login(email, password)
                    # Two-factor codes are typed by you over VNC: Fanatical hands out no
                    # recovery codes, so there is nothing worth keeping in .env.
                    if not await self._confirm_side_login("Fanatical", self._fanatical_signed_in,
                                                          credentials=(email, password)):
                        return
                    self._log_side_signed_in("Fanatical", email)
                else:
                    logger.warning("[Fanatical] No credentials set (FANATICAL_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(self._fanatical_signed_in, store_key="fanatical"):
                        return

            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            if not await self._clear_challenge("Fanatical"):
                notify_game["status"] = "failed:challenge"
                return

            clicked = False
            for _ in range(5):
                clicked = await self.page.evaluate("""
                    (() => {
                        const btns = [...document.querySelectorAll('button, a')];
                        const claim = btns.find(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return t === 'claim this game' || t === 'claim game';
                        });
                        if (claim && !claim.disabled) {
                            claim.click(); return true;
                        }
                        return false;
                    })()
                """)
                if clicked:
                    break
                await self.sleep(2)

            # The old code reported a win whether or not the page agreed with it.
            claimed = False
            if clicked:
                await self.sleep(6)
                claimed = bool(await self.page.evaluate("""
                    (() => {
                        const body = (document.body?.innerText || '').toLowerCase();
                        const stillOffered = [...document.querySelectorAll('button, a')].some(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return t === 'claim this game' || t === 'claim game';
                        });
                        const says = /already claimed|you have claimed|successfully claimed|in your library/.test(body);
                        return says || !stillOffered;
                    })()
                """))

            if claimed:
                logger.info("✓ [Fanatical] Claimed '%s'!", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="fanatical", user=self.user,
                        game_id=game_id, title=title, url=url, status="claimed",
                    )
                    obj.status = "claimed"
                    await session.commit()
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"fanatical_{filenamify(title)}")
            else:
                logger.warning("[Fanatical] '%s' was not confirmed as claimed (clicked: %s).", title, clicked)
                notify_game["status"] = "failed:unconfirmed"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")

        except Exception:
            logger.exception("[Fanatical] Error claiming '%s'", title)

    async def _claim_alienware_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)

        notify_game = {"title": f"{title} (Alienware)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            # Check if we already notified about this game to prevent spam
            async with async_session() as session:
                # We use status="notified" to distinctly mark these
                existing, created = await get_or_create(
                    session, store="alienware", user=self.user,
                    game_id=giveaway_url, title=title, url=url, status="notified"
                )
                
                if not created:
                    logger.info("⏭️ [Alienware] '%s', already notified before.", title)
                    notify_game["status"] = "existed"
                    return

                # If it's new, we just notify
                logger.info("🔔 [Alienware] '%s': Please claim manually (requires ARP points): %s", title, url)
                existing.status = "notified"
                await session.commit()

            # Alienware keys need ARP points and solve a captcha, so this one is yours to finish.
            notify_game["status"] = "notified, claim it yourself 🔔"

        except Exception:
            logger.exception("[Alienware] Error processing notification for '%s'", title)

    # ─────────────────────────────────────────────────────────────────────
    # Itch.io
    # ─────────────────────────────────────────────────────────────────────
    async def _submit_itch_credentials(self, email: str, password: str) -> None:
        """Fill itch.io's sign-in form and send it."""
        js_email = json.dumps(email)
        js_password = json.dumps(password)
        await self.page.evaluate(f'''
            (() => {{
                const emailInp = document.querySelector('input[name="username"], input[type="email"]');
                const passInp = document.querySelector('input[name="password"], input[type="password"]');
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                if (emailInp) {{
                    setter.call(emailInp, {js_email});
                    emailInp.dispatchEvent(new Event("input", {{bubbles: true}}));
                }}
                if (passInp) {{
                    setter.call(passInp, {js_password});
                    passInp.dispatchEvent(new Event("input", {{bubbles: true}}));
                }}
                const submit = document.querySelector('button[type="submit"]')
                    || [...document.querySelectorAll('button')]
                        .find(b => (b.textContent || '').toLowerCase().includes('log in'));
                if (submit) submit.click();
            }})()
        ''')
        await self.sleep(5)

    async def _itch_session_ready(self) -> bool:
        """Sign in to itch.io once per run, judged on itch.io itself.

        A creator's own subdomain shows no sign-in link, so asking the game page whether a
        login is needed answered "no" while signed out, and every check after it was wrong.
        """
        if self._itch_session_ok:
            return True

        await self.page.get("https://itch.io/")
        await self.sleep(3)
        if await self._itch_logged_in():
            self._itch_session_ok = True
            return True

        email = cfg.itchio_email
        password = cfg.itchio_password
        if not (email and password):
            logger.warning("[Itch.io] No credentials set (ITCHIO_EMAIL/PASSWORD). Waiting for VNC...")
            self._itch_session_ok = await self._wait_for_vnc_login(self._itch_logged_in, store_key="itchio")
            return self._itch_session_ok

        logger.info("[Itch.io] Logging in as %s…", mask_account(email))
        await self.page.get("https://itch.io/login")
        await self.sleep(3)
        await self._submit_itch_credentials(email, password)

        # A code screen or a rejected password used to pass silently from here.
        if not await self._confirm_side_login(
                "Itch.io", self._itch_logged_in,
                backup_codes=cfg.itchio_otp_codes,
                backup_file="used_itchio_codes.txt", otp_key=cfg.itchio_otp_key,
                credentials=(email, password)):
            return False
        self._log_side_signed_in("Itch.io", email)
        self._itch_session_ok = True
        return True

    async def _claim_itchio_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("final_url") or game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)
        game_id = itch_game_id(url) or giveaway_url

        notify_game = {"title": f"{title} (Itch.io)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # Signing in is decided on itch.io itself and done once per run.
            if not await self._itch_session_ready():
                return

            # Staying on the previous game's page made every later giveaway inherit its banner.
            await self.page.get(url)
            await self.sleep(4)

            # A signed-out page never shows the ownership banner, so this waits for the session.
            if await self._itch_owns_this():
                logger.info("[Itch.io] '%s' already owned.", title)
                if cfg.dryrun:
                    notify_game["status"] = "existed"
                    return
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="itchio", user=self.user,
                        game_id=game_id, title=title, url=url, status="existed",
                    )
                    obj.status = "existed"
                    await session.commit()
                notify_game["status"] = "existed"
                return

            # Try to claim: click "Download or Claim" or "Claim" button
            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            walked = await self._itch_run_claim(title)

            # The claim only counts when itch.io says the game is on the account. Clicking
            # through the downloads without claiming leaves you with a file and nothing else.
            await self.page.get(url)
            await self.sleep(5)
            owned = await self._itch_owns_this()

            if owned:
                logger.info("✓ [Itch.io] Claimed '%s'!", title)
                await self._remember_itchio(game_id, title, url, "claimed")
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"itchio_{filenamify(title)}")
            elif walked == "download-only":
                # Plenty of itch.io giveaways are a file and nothing else: no control puts
                # them on the account, so this is not a failed claim, it is all there is.
                first_time = await self._remember_itchio(game_id, title, url, "skipped:download-only")
                if first_time:
                    logger.info("[Itch.io] '%s' is handed out as a download only, there is nothing to "
                                "claim onto the account. Saying so once, later runs stay quiet.", title)
                else:
                    logger.debug("[Itch.io] '%s' is still download only, already reported.", title)
                notify_game["status"] = download_only_status(first_time)
            else:
                logger.warning("[Itch.io] '%s' is not on the account after the claim walk "
                               "(claim step: %s).", title, walked)
                notify_game["status"] = "failed:unconfirmed"
                await self.take_screenshot(f"itchio_fail_{filenamify(title)}")

        except Exception:
            logger.exception("[Itch.io] Error claiming '%s'", title)

    # ─────────────────────────────────────────────────────────────────────
    # IndieGala
    # ─────────────────────────────────────────────────────────────────────
    async def _claim_indiegala_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)

        notify_game = {"title": f"{title} (IndieGala)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # The host alone is not enough: staying on the previous game's page would judge
            # this one by that page.
            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            # Check if already owned
            body_text = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
            if "already in your library" in body_text or "in your library" in body_text:
                logger.info("[IndieGala] '%s' already owned.", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="indiegala", user=self.user,
                        game_id=giveaway_url, title=title, url=url, status="existed",
                    )
                    obj.status = "existed"
                    await session.commit()
                notify_game["status"] = "existed"
                return

            # Check if login needed
            # One check decides, so detection and confirmation cannot drift apart (issue #47).
            if not await self._ig_logged_in():
                email = cfg.indiegala_email
                password = cfg.indiegala_password
                if email and password:
                    logger.info("[IndieGala] Logging in as %s…", mask_account(email))
                    await self.page.get("https://www.indiegala.com/login")
                    await self.sleep(4)

                    js_email = json.dumps(email)
                    js_password = json.dumps(password)
                    await self.page.evaluate(f'''
                        (() => {{
                            const emailInp = document.querySelector('input[name="email"], input[type="email"]');
                            const passInp = document.querySelector('input[name="password"], input[type="password"]');
                            if (emailInp) {{
                                let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                if(setter) {{ setter.call(emailInp, {js_email}); emailInp.dispatchEvent(new Event("input", {{bubbles: true}})); }}
                            }}
                            if (passInp) {{
                                let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                if(setter) {{ setter.call(passInp, {js_password}); passInp.dispatchEvent(new Event("input", {{bubbles: true}})); }}
                            }}
                            const submit = document.querySelector('button[type="submit"], input[type="submit"]') ||
                                [...document.querySelectorAll('button')].find(b => (b.textContent || '').toLowerCase().includes('log in'));
                            if (submit) submit.click();
                        }})()
                    ''')
                    await self.sleep(6)

                    if not await self._confirm_side_login("IndieGala", self._ig_logged_in,
                                                          credentials=(email, password)):
                        return
                    self._log_side_signed_in("IndieGala", email)

                    # Navigate back to game page
                    await self.page.get(url)
                    await self.sleep(4)
                else:
                    logger.warning("[IndieGala] No credentials set (INDIEGALA_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(self._ig_logged_in, store_key="indiegala"):
                        return

            # Try to click claim / add-to-library button
            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            claimed = False
            for _ in range(5):
                clicked = await self.page.evaluate("""
                    (() => {
                        const btns = [...document.querySelectorAll('button, a, div[role="button"]')];
                        const claim = btns.find(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return t.includes('add to library') || t.includes('claim')
                                || t.includes('get it free') || t.includes('grab it');
                        });
                        if (claim && !claim.disabled) { claim.click(); return true; }
                        return false;
                    })()
                """)
                if clicked:
                    await self.sleep(4)
                    body_after = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
                    if "library" in body_after or "success" in body_after or "claimed" in body_after:
                        claimed = True
                        break
                    claimed = True
                    break
                await self.sleep(2)

            if claimed:
                logger.info("✓ [IndieGala] Claimed '%s'!", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="indiegala", user=self.user,
                        game_id=giveaway_url, title=title, url=url, status="claimed",
                    )
                    obj.status = "claimed"
                    await session.commit()
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"indiegala_{filenamify(title)}")
            else:
                logger.warning("[IndieGala] Could not claim '%s'", title)
                await self.take_screenshot(f"indiegala_fail_{filenamify(title)}")

        except Exception:
            logger.exception("[IndieGala] Error claiming '%s'", title)


async def claim_side_stores(routed: dict | None = None) -> dict:
    """Entry point for the sites that have no store module of their own."""
    claimer = GamerPowerClaimer()
    await claimer.run(routed)
    return {"store": "GamerPower", "user": None, "games": claimer.notify_games}
