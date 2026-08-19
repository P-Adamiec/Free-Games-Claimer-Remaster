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

from src.core.claimer import BaseClaimer
from src.core.config import cfg
from src.core.database import async_session, ClaimedGame, get_or_create
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


class GamerPowerClaimer(BaseClaimer):
    store_name = "gamerpower"
    # The browser profile keeps its original folder name so existing side-store logins survive.
    profile_name = "base"

    def __init__(self) -> None:
        super().__init__()
        self.user = "GamerPower"
        self._fanatical_games = []

    async def _get_claimed_titles_from_db(self) -> set[str]:
        """Fetch all previously claimed/existed game titles from the DB."""
        titles = set()
        async with async_session() as session:
            stmt = select(ClaimedGame).where(ClaimedGame.status.in_(["claimed", "existed"]))
            result = await session.execute(stmt)
            for db_game in result.scalars().all():
                titles.add(self._normalize_title(db_game.title))
        return titles

    async def run(self) -> None:
        try:
            # 1. Fetch API
            logger.debug("Fetching giveaways from GamerPower API")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(GAMERPOWER_API_URL)
                resp.raise_for_status()
                data = resp.json()

            if not isinstance(data, list):
                logger.debug("GamerPower returned non-list data")
                return

            # Filter before resolving redirects: every entry kept costs one extra HTTP request.
            kept = [item for item in data if is_wanted(item, cfg.gp_claim_dlc)]
            dropped = len(data) - len(kept)
            if dropped:
                logger.debug("Skipped %d giveaway(s) of an unwanted type (GP_CLAIM_DLC=%s).",
                             dropped, cfg.gp_claim_dlc)

            games = []
            for item in kept:
                giveaway_url = item.get("open_giveaway_url", "")
                title = item.get("title", "Unknown")

                # Clean up dirty GamerPower titles
                title = re.sub(r'(?i)\s*\(\s*steam\s*\)\s*(?:key\s*)?giveaway\s*$', '', title)
                title = re.sub(r'(?i)\s*(?:steam\s*)?key\s*giveaway\s*$', '', title)
                title = re.sub(r'(?i)\s*giveaway\s*$', '', title)
                title = re.sub(r'(?i)\s*\(\s*steam\s*\)\s*key\s*$', '', title)
                title = re.sub(r'(?i)\s*steam\s*key\s*$', '', title)
                title = title.strip()

                games.append({
                    "title": title,
                    "url": giveaway_url,
                    "giveaway_url": giveaway_url,
                    # Carried through so routing can fall back on them, see classify_target().
                    "instructions": item.get("instructions", "") or "",
                    "type": item.get("type", "") or "",
                    "platforms": item.get("platforms", "") or "",
                })

            # 2. Global deduplication against DB (already claimed in Steam, Epic, GOG, etc)
            logger.debug("GamerPower API returned %d giveaway(s)", len(games))
            db_titles = await self._get_claimed_titles_from_db()
            unique_gp = []
            for gp in games:
                norm = self._normalize_title(gp["title"])
                is_dup = any(s in norm or norm in s for s in db_titles)
                if is_dup:
                    logger.debug("GamerPower duplicate (already processed globally): %s", gp["title"])
                else:
                    unique_gp.append(gp)

            if not unique_gp:
                logger.info("No unique GamerPower giveaways found.")
                return

            links = [f"  • [bold cyan]{g['title']}[/bold cyan] 🔗 {g.get('giveaway_url', '')}" for g in unique_gp]
            logger.info("🎮 [bold magenta]GamerPower: %d extra game(s):[/bold magenta]\n%s", 
                        len(unique_gp), "\n".join(links))

            # 3. Resolve and classify everything first, so browsers can be grouped per store.
            routed: dict[str, list[dict]] = {}
            for game in unique_gp:
                store, final_url = await self._route(game)
                game["final_url"] = final_url
                routed.setdefault(store, []).append(game)
            logger.debug("GamerPower routing: %s",
                         {store: len(items) for store, items in sorted(routed.items())})

            for store, items in routed.items():
                if store in COVERED_ELSEWHERE:
                    for game in items:
                        logger.info("⏭️ [GamerPower] '%s' → %s, already covered by the '%s' store, skipping",
                                    game.get("title", "Unknown"), store.title(), COVERED_ELSEWHERE[store])

            # 4. Side sites share this claimer's browser, so it only opens when one has work.
            side_work = []
            for store in ("fanatical", "alienware", "itchio", "indiegala", "unknown"):
                enabled, label, _ = self._side_store(store)
                for game in routed.get(store, []):
                    if enabled:
                        side_work.append((store, game))
                    else:
                        logger.info("⏭️ [GamerPower] '%s' → %s (skipped, disabled in config)",
                                    game.get("title", "Unknown"), label)
            if side_work:
                await self.start_browser(force_headful=True)
                for store, game in side_work:
                    await self._process_side_store(store, game)

            # 5. Big stores: one browser per store, not one per game.
            for store in ("steam", "epic", "gog"):
                if routed.get(store):
                    await self._claim_major_store_batch(store, routed[store])

        except Exception as exc:
            logger.exception("Fatal error in GamerPower")
            if cfg.notify_errors:
                await self.notify(f"gamerpower failed: {exc}")
        finally:
            # Summary notifications deferred to main.py
            await self.close_browser()

    async def _route(self, game: dict) -> tuple[str, str]:
        """Resolve a giveaway's redirect and work out which store it ends at. No browser."""
        title = game.get("title", "Unknown")
        giveaway_url = game.get("giveaway_url", "")
        game["url"] = giveaway_url  # Explicit url for the side-store claimers.

        final_url = giveaway_url.lower()
        try:
            async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/114.0.0.0 Safari/537.36"}) as client:
                res = await client.get(giveaway_url)
                final_url = str(res.url).lower()
        except Exception as e:
            logger.debug("Failed to pre-resolve URL %s: %s", giveaway_url, e)

        instructions = (game.get("instructions", "") or "").lower()
        target_store = classify_target(final_url, instructions)
        if target_store != "unknown" and classify_target(final_url) == "unknown":
            logger.debug("[GamerPower] '%s' routed to %s by its instructions, the URL gave nothing.",
                         title, target_store)
        logger.debug("[GamerPower] '%s' resolved to %s -> target_store=%s", title, final_url, target_store)
        return target_store, final_url

    def _side_store(self, target_store: str) -> tuple[bool, str, object]:
        """Switch, label and handler for a site with no store module of its own."""
        table = {
            "fanatical": (cfg.fanatical_enable, "Fanatical giveaway", self._claim_fanatical_game),
            "alienware": (cfg.alienware_enable, "Alienware Arena", self._claim_alienware_game),
            "itchio": (cfg.itchio_enable, "Itch.io giveaway", self._claim_itchio_game),
            "indiegala": (cfg.indiegala_enable, "IndieGala giveaway", self._claim_indiegala_game),
        }
        # No handler for an unknown site, all we can do is open it for a human.
        return table.get(target_store, (cfg.unknown_stores_enable, "Unknown site", None))

    async def _process_side_store(self, target_store: str, game: dict) -> None:
        """Claim one giveaway on a side site, in the browser this claimer already opened."""
        title = game.get("title", "Unknown")
        _, label, handler = self._side_store(target_store)

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

    @staticmethod
    def _product_pages(games: list[dict], markers: tuple[str, ...], label: str) -> list[dict]:
        """Keep only the entries that land on a real product page, not a storefront banner."""
        wanted = []
        for game in games:
            final_url = game.get("final_url", "")
            if any(marker in final_url for marker in markers):
                wanted.append(game)
            else:
                logger.info("⏭️ [GamerPower] '%s' → %s URL is not a game page (%s), skipping",
                            game.get("title", "Unknown"), label, final_url)
        return wanted

    async def _claim_major_store_batch(self, target_store: str, games: list[dict]) -> None:
        """Claim every giveaway landing on one big store in a single browser session."""
        if not is_store_active(target_store):
            for game in games:
                logger.info("⏭️ [GamerPower] '%s' → %s, which is not part of this run, skipping",
                            game.get("title", "Unknown"), target_store.title())
            return

        if target_store == "steam":
            wanted = self._product_pages(games, ("/app/", "/sub/"), "Steam")
            if not wanted:
                return

            from src.stores.steam import SteamClaimer
            claimer = SteamClaimer()
            claimer.user = cfg.steam_username or "shared_session"
            claimer.notify_games = self.notify_games

            try:
                # A dedicated browser on the Steam profile, so cookies and auth carry over.
                await claimer.start_browser(
                    force_headful=True,
                    extra_args=["--ignore-gpu-blocklist", "--enable-unsafe-webgpu"]
                )
                for game in wanted:
                    try:
                        await claimer._claim_game({**game, "url": game["final_url"], "source": "gamerpower"})
                    except Exception:
                        logger.exception("[GamerPower] Steam delegation failed for '%s'",
                                         game.get("title", "Unknown"))
            except Exception:
                logger.exception("[GamerPower] Steam delegation failed to start")
            finally:
                await claimer.close_browser()

        elif target_store == "epic":
            wanted = self._product_pages(games, ("/p/", "/bundles/"), "Epic")
            if not wanted:
                return

            from src.stores.epic import EpicGamesClaimer
            claimer = EpicGamesClaimer()
            claimer.user = cfg.eg_email or "shared_session"
            claimer.notify_games = self.notify_games

            try:
                # Same GPU flags epic.py uses: software rendering is what summons the captcha.
                await claimer.start_browser(
                    force_headful=True,
                    extra_args=["--ignore-gpu-blocklist", "--enable-unsafe-webgpu"]
                )
                if not await claimer._ensure_logged_in():
                    logger.warning("[GamerPower] Epic login failed, skipping %d game(s)", len(wanted))
                    return
                for game in wanted:
                    try:
                        await claimer._claim_game(game["final_url"])
                    except Exception:
                        logger.exception("[GamerPower] Epic delegation failed for '%s'",
                                         game.get("title", "Unknown"))
            except Exception:
                logger.exception("[GamerPower] Epic delegation failed to start")
            finally:
                await claimer.close_browser()

        elif target_store == "gog":
            # GOG's claimer takes no URL, it claims whatever giveaway gog.com is running,
            # so it runs once no matter how many entries point there.
            from src.stores.gog import GOGClaimer
            claimer = GOGClaimer()
            claimer.user = cfg.gog_email or "shared_session"
            claimer.notify_games = self.notify_games

            try:
                await claimer.start_browser()
                if not await claimer._ensure_logged_in():
                    logger.warning("[GamerPower] GOG login failed, skipping %d game(s)", len(games))
                    return
                await claimer._claim_giveaway()
            except Exception:
                logger.exception("[GamerPower] GOG delegation failed")
            finally:
                await claimer.close_browser()

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

    def _log_side_signed_in(self, label: str, account: str | None) -> None:
        """The line every store prints. `log_signed_in()` would also overwrite `self.user`,
        which this claimer keeps as the database key for all its side stores."""
        self.logger.info("🔓 [bold green]Signed in as:[/bold green] %s (%s)", account or "unknown", label)

    async def _confirm_side_login(self, label: str, check_fn, backup_codes: list | None = None,
                                 backup_file: str = "") -> bool:
        """Finish a side-store login: answer a code screen if one shows, else hand over via VNC."""
        if await check_fn():
            return True

        # A captcha is not a login failure the bot can retype its way out of: Fanatical answers
        # a correct password with "we just need to check that you're a real person".
        if await self._human_challenge_present():
            logger.warning("[%s] The site is showing a human check, handing over.", label)
            if await self._wait_out_challenge(label) and await check_fn():
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
            if backup_codes:
                tried_backup = await self._fill_backup_code(label, backup_codes, backup_file)
                if tried_backup and await check_fn():
                    return True
            else:
                logger.info("[%s] The site is asking for a two-factor code, handing over to you.", label)

        code_screen = needs_otp(state)
        msg = self._vnc_notice(
            f"{label}: 2FA code needed" if code_screen else f"{label}: login needs you",
            login_help_message(label, code_screen, tried_backup))
        if await self._wait_for_vnc_login(check_fn, custom_msg=msg):
            return True
        logger.warning("[%s] Still not signed in, skipping this giveaway.", label)
        return False

    async def _fill_backup_code(self, label: str, codes: list, used_name: str) -> bool:
        """Spend one recovery code, the way gog.py does: first unused, then remember it."""
        used_file = cfg._data_dir / used_name
        used = used_file.read_text("utf-8").splitlines() if used_file.exists() else []
        raw_code = next((c for c in codes if c not in used), None)
        if not raw_code:
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
            field = await self.page.select(OTP_FIELD, timeout=8)
            if not field:
                return False
            await field.click()
            await self.sleep(0.4)
            await field.send_keys(raw_code.replace("-", "").replace(" ", ""))
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
        except Exception as e:
            logger.debug("[%s] Could not enter the recovery code: %s", label, e)
            return False

        # Written even when the login still fails: the site has seen the code either way.
        with used_file.open("a", encoding="utf-8") as fh:
            print(raw_code, file=fh)
        logger.info("[%s] Used one recovery code, %d left.", label, max(0, len(codes) - len(used) - 1))
        return True

    async def _clear_challenge(self, label: str) -> bool:
        """Let a captcha that shows up mid-claim be solved, instead of failing quietly."""
        if not await self._human_challenge_present():
            return True
        logger.warning("[%s] A human check appeared during the claim.", label)
        return await self._wait_out_challenge(label)

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

    async def _itch_run_claim(self, title: str) -> bool:
        """Walk itch.io's real claim chain: purchase page, downloads, then Claim game."""
        purchase = await self.page.evaluate("""
            (() => {
                const a = document.querySelector('a.buy_btn[href], a.button.buy_btn[href]');
                return a ? a.href : '';
            })()
        """)
        if not purchase:
            logger.warning("[Itch.io] '%s' has no claim button on its page.", title)
            return False

        await self.page.get(str(purchase))
        await self.sleep(5)
        if not await self._clear_challenge("Itch.io"):
            return False
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
            return False
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
            logger.warning("[Itch.io] '%s' offered a download but no claim, so it would not stay "
                           "on the account.", title)
            return False
        await self.sleep(7)
        return True

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
                    logger.info("[Fanatical] Logging in as %s…", email)
                    await self._fanatical_login(email, password)
                    # Two-factor codes are typed by you over VNC: Fanatical hands out no
                    # recovery codes, so there is nothing worth keeping in .env.
                    if not await self._confirm_side_login("Fanatical", self._fanatical_signed_in):
                        return
                    self._log_side_signed_in("Fanatical", email)
                else:
                    logger.warning("[Fanatical] No credentials set (FANATICAL_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(self._fanatical_signed_in):
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
    async def _claim_itchio_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("final_url") or game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)
        game_id = itch_game_id(url) or giveaway_url

        notify_game = {"title": f"{title} (Itch.io)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # The host alone is not enough: staying on the previous game's page made every
            # later giveaway inherit its "you own this" banner.
            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            # Check if already owned
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

            # Check if login needed
            needs_login = await self.page.evaluate("""
                (() => {
                    const links = [...document.querySelectorAll('a')];
                    return links.some(a => {
                        const t = (a.textContent || '').trim().toLowerCase();
                        const href = (a.getAttribute('href') || '').toLowerCase();
                        return t === 'log in' || t === 'sign in' || href.includes('/login');
                    });
                })()
            """)

            if needs_login:
                email = cfg.itchio_email
                password = cfg.itchio_password
                if email and password:
                    logger.info("[Itch.io] Logging in as %s…", email)
                    await self.page.get("https://itch.io/login")
                    await self.sleep(3)

                    js_email = json.dumps(email)
                    js_password = json.dumps(password)
                    await self.page.evaluate(f'''
                        (() => {{
                            const emailInp = document.querySelector('input[name="username"], input[type="email"]');
                            const passInp = document.querySelector('input[name="password"], input[type="password"]');
                            if (emailInp) {{
                                let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                if(setter) {{ setter.call(emailInp, {js_email}); emailInp.dispatchEvent(new Event("input", {{bubbles: true}})); }}
                            }}
                            if (passInp) {{
                                let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                if(setter) {{ setter.call(passInp, {js_password}); passInp.dispatchEvent(new Event("input", {{bubbles: true}})); }}
                            }}
                            const submit = document.querySelector('button[type="submit"]') ||
                                [...document.querySelectorAll('button')].find(b => (b.textContent || '').toLowerCase().includes('log in'));
                            if (submit) submit.click();
                        }})()
                    ''')
                    await self.sleep(5)

                    # A code screen or a rejected password used to pass silently from here.
                    if not await self._confirm_side_login(
                            "Itch.io", self._itch_logged_in,
                            backup_codes=cfg.itchio_otp_codes if cfg.itchio_otp_enable else None,
                            backup_file="used_itchio_codes.txt"):
                        return
                    self._log_side_signed_in("Itch.io", email)

                    # Navigate back to game page
                    await self.page.get(url)
                    await self.sleep(4)
                else:
                    logger.warning("[Itch.io] No credentials set (ITCHIO_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(self._itch_logged_in):
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
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="itchio", user=self.user,
                        game_id=game_id, title=title, url=url, status="claimed",
                    )
                    obj.status = "claimed"
                    await session.commit()
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"itchio_{filenamify(title)}")
            else:
                logger.warning("[Itch.io] '%s' is not on the account after the claim walk (reached "
                               "the claim step: %s).", title, walked)
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
            needs_login = await self.page.evaluate("""
                (() => {
                    const btns = [...document.querySelectorAll('a, button')];
                    return btns.some(b => {
                        const t = (b.textContent || '').trim().toLowerCase();
                        return t === 'login' || t === 'sign in' || t === 'log in';
                    }) || !document.querySelector('.user-menu, .user-avatar, .profile-link');
                })()
            """)

            if needs_login:
                async def _ig_logged_in() -> bool:
                    txt = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
                    return 'add to library' in txt or 'in your library' in txt

                email = cfg.indiegala_email
                password = cfg.indiegala_password
                if email and password:
                    logger.info("[IndieGala] Logging in as %s…", email)
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

                    if not await self._confirm_side_login("IndieGala", _ig_logged_in):
                        return
                    self._log_side_signed_in("IndieGala", email)

                    # Navigate back to game page
                    await self.page.get(url)
                    await self.sleep(4)
                else:
                    logger.warning("[IndieGala] No credentials set (INDIEGALA_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(_ig_logged_in):
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


async def claim_gamerpower() -> dict:
    """Entry point for testing and execution."""
    claimer = GamerPowerClaimer()
    await claimer.run()
    return {"store": "GamerPower", "user": None, "games": claimer.notify_games}
