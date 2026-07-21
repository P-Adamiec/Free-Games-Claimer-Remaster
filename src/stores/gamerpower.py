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
from src.core.url_security import url_has_allowed_host
import logging
from src.core.claimer import filenamify

logger = logging.getLogger("fgc.gamerpower")

GAMERPOWER_API_URL = "https://www.gamerpower.com/api/giveaways"

class GamerPowerClaimer(BaseClaimer):
    def __init__(self) -> None:
        super().__init__()
        self.user = "GamerPower"
        self._fanatical_games = []

    def _normalize_title(self, title: str) -> str:
        """Strip non-alphanumeric chars for loose title comparison."""
        return re.sub(r'[^a-z0-9]', '', str(title).lower())

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

            games = []
            for item in data:
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
                })

            # 2. Global deduplication against DB (already claimed in Steam, Epic, GOG, etc)
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

            # 3. Start browser for Fanatical / Other redirects
            await self.start_browser(force_headful=True)

            for game in unique_gp:
                await self._process_gamerpower_game(game)

        except Exception as exc:
            logger.exception("Fatal error in GamerPower")
            if cfg.notify_errors:
                await self.notify(f"gamerpower failed: {exc}")
        finally:
            # Summary notifications deferred to main.py
            await self.close_browser()

    async def _process_gamerpower_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        giveaway_url = game.get("giveaway_url", "")
        instructions = (game.get("instructions", "") or "").lower()

        logger.info("🔗 [GamerPower] Processing '%s'", title)

        # 1. Resolve redirect to get final destination URL without opening browser
        final_url = giveaway_url.lower()
        try:
            async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/114.0.0.0 Safari/537.36"}) as client:
                res = await client.get(giveaway_url)
                final_url = str(res.url).lower()
        except Exception as e:
            logger.debug("Failed to pre-resolve URL %s: %s", giveaway_url, e)

        # 2. Store Detection
        target_store = "unknown"
        
        if any(s in final_url for s in ("store.steampowered", "epicgames.com", "gog.com", "playstation", "xbox", "nintendo")):
            target_store = "major_store"
        elif url_has_allowed_host(final_url, "fanatical.com", allow_subdomains=True):
            target_store = "fanatical"
        elif url_has_allowed_host(final_url, "alienwarearena.com", allow_subdomains=True):
            target_store = "alienware"
        elif url_has_allowed_host(final_url, "itch.io", allow_subdomains=True):
            target_store = "itchio"
        elif url_has_allowed_host(final_url, "indiegala.com", allow_subdomains=True):
            target_store = "indiegala"

        # Special fallback instruction checks just in case the link itself doesn't contain the domain explicitly
        if target_store == "unknown":
            if "indiegala" in instructions: target_store = "indiegala"
            elif "alienware" in instructions: target_store = "alienware"
            elif "fanatical" in instructions: target_store = "fanatical"

        # 3. Routing
        game["url"] = giveaway_url  # Set explicit url for claimers to use
        domain = urlparse(final_url).netloc.replace("www.", "")
        
        try:
            if target_store == "fanatical":
                if cfg.fanatical_enable:
                    logger.info("🎮 [GamerPower] '%s' → Fanatical giveaway", title)
                    await self._claim_fanatical_game(game)
                else:
                    logger.info("⏭️ [GamerPower] '%s' → Fanatical giveaway "
                                "(skipped — disabled in config)", title)
                                
            elif target_store == "alienware":
                if cfg.alienware_enable:
                    logger.info("🎮 [GamerPower] '%s' → Alienware Arena", title)
                    await self._claim_alienware_game(game)
                else:
                    logger.info("⏭️ [GamerPower] '%s' → Alienware Arena "
                                "(skipped — disabled in config)", title)
                                
            elif target_store == "itchio":
                if cfg.itchio_enable:
                    logger.info("🎮 [GamerPower] '%s' → Itch.io giveaway", title)
                    await self._claim_itchio_game(game)
                else:
                    logger.info("⏭️ [GamerPower] '%s' → Itch.io giveaway "
                                "(skipped — disabled in config)", title)
                                
            elif target_store == "indiegala":
                if cfg.indiegala_enable:
                    logger.info("🎮 [GamerPower] '%s' → IndieGala giveaway", title)
                    await self._claim_indiegala_game(game)
                else:
                    logger.info("⏭️ [GamerPower] '%s' → IndieGala giveaway "
                                "(skipped — disabled in config)", title)
                                
            elif target_store == "major_store":
                logger.info("🎮 [GamerPower] '%s' → Points to major store (%s). Attempting direct claim.", title, domain)
                await self._claim_major_store_game(game, domain, final_url)
                
            else: # Unknown store
                if cfg.unknown_stores_enable:
                    logger.info("❓ [GamerPower] '%s' → Unknown site (%s). Opening for manual review via VNC.", title, domain)
                    await self.page.get(giveaway_url)
                    await self.sleep(10)
                else:
                    logger.info("⏭️ [GamerPower] '%s' → Unknown site (%s) — skipping", title, domain)

        except Exception:
            logger.exception("[GamerPower] Error processing '%s'", title)

    async def _claim_major_store_game(self, game: dict, domain: str, final_url: str) -> None:
        """Delegate claiming to the appropriate store claimer using the resolved store URL."""
        title = game.get("title", "Unknown")

        if "steampowered" in domain:
            # Validate: must be a Steam app/sub page, not a generic landing page
            if "/app/" not in final_url and "/sub/" not in final_url:
                logger.info("⏭️ [GamerPower] '%s' → Steam URL is not a game page (%s) — skipping", title, final_url)
                return

            from src.stores.steam import SteamClaimer
            claimer = SteamClaimer()
            claimer.user = cfg.steam_username or "shared_session"
            claimer.notify_games = self.notify_games
            
            try:
                # Launch a dedicated browser using the Steam profile to preserve cookies/auth
                await claimer.start_browser(
                    force_headful=True,
                    extra_args=["--ignore-gpu-blocklist", "--enable-unsafe-webgpu"]
                )
                steam_game = {**game, "url": final_url, "source": "gamerpower"}
                await claimer._claim_game(steam_game)
            except Exception:
                logger.exception("[GamerPower] Steam delegation failed for '%s'", title)
            finally:
                if claimer.browser:
                    claimer.browser.stop()
            
        elif "epicgames" in domain:
            # Validate: must be a product page (/p/ or /bundles/), not /mobile, /browse, etc.
            if "/p/" not in final_url and "/bundles/" not in final_url:
                logger.info("⏭️ [GamerPower] '%s' → Epic URL is not a game page (%s) — skipping", title, final_url)
                return

            from src.stores.epic import EpicGamesClaimer
            claimer = EpicGamesClaimer()
            claimer.user = cfg.eg_email or "shared_session"
            claimer.notify_games = self.notify_games
            
            try:
                # Launch a dedicated browser using the Epic profile to preserve cookies/auth
                await claimer.start_browser(force_headful=True)
                await claimer._ensure_logged_in()
                await claimer._claim_game(final_url)
            except Exception:
                logger.exception("[GamerPower] Epic delegation failed for '%s'", title)
            finally:
                if claimer.browser:
                    claimer.browser.stop()
            
        else:
            logger.info("⏭️ [GamerPower] Target major store '%s' is not supported for direct delegation — skipping", domain)

    async def _claim_fanatical_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)

        notify_game = {"title": f"{title} (Fanatical)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            current_url = await self.page.evaluate("window.location.href")
            if "fanatical.com" not in current_url:
                await self.page.get(url)
                await self.sleep(4)

            # Check already claimed
            body_text = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
            if "already claimed" in body_text or "you have claimed" in body_text:
                logger.info("[Fanatical] '%s' already claimed.", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="fanatical", user=self.user,
                        game_id=giveaway_url, title=title, url=url, status="existed",
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
                email = cfg.fanatical_email
                password = cfg.fanatical_password

                if email and password:
                    logger.info("[Fanatical] Logging in as %s…", email)
                    # Dismiss cookies
                    await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button, a')];
                            const reject = btns.find(b => {
                                const t = (b.textContent || '').trim();
                                return t.includes('Reject All Non-Essential') || t.includes('Reject All');
                            });
                            if (reject) reject.click();
                        })()
                    """)
                    await self.sleep(1)

                    await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button, a')];
                            const signIn = btns.find(b => {
                                const t = (b.textContent || '').trim().toLowerCase();
                                return t.includes('sign in');
                            });
                            if (signIn) signIn.click();
                        })()
                    """)
                    await self.sleep(4)
                    
                    # Dismiss cookies again
                    await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button, a')];
                            const reject = btns.find(b => {
                                const t = (b.textContent || '').trim();
                                return t.includes('Reject All Non-Essential') || t.includes('Reject All');
                            });
                            if (reject) reject.click();
                        })()
                    """)
                    await self.sleep(1)

                    # Fill email
                    await self.page.evaluate("""
                        ((email) => {
                            const labels = [...document.querySelectorAll('label')];
                            for (const label of labels) {
                                const t = (label.textContent || '').trim().toLowerCase();
                                if (t.includes('email')) {
                                    const forId = label.getAttribute('for');
                                    const input = forId ? document.getElementById(forId) : label.parentElement?.querySelector('input');
                                    if (input) {
                                        input.focus(); input.value = email;
                                        input.dispatchEvent(new Event('input', { bubbles: true }));
                                        input.dispatchEvent(new Event('change', { bubbles: true }));
                                        return;
                                    }
                                }
                            }
                            const inputs = document.querySelectorAll('input[type="email"], input[type="text"]');
                            for (const inp of inputs) {
                                if (inp.offsetParent !== null) {
                                    inp.focus(); inp.value = email;
                                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                                    return;
                                }
                            }
                        })""" + f'("{email}")')
                    await self.sleep(0.5)

                    # Fill password
                    await self.page.evaluate("""
                        ((pw) => {
                            const inp = document.querySelector('input[type="password"]');
                            if (inp) {
                                inp.focus(); inp.value = pw;
                                inp.dispatchEvent(new Event('input', { bubbles: true }));
                                inp.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        })""" + f'("{password}")')
                    await self.sleep(0.5)

                    await self.page.evaluate("""
                        (() => {
                            const btns = [...document.querySelectorAll('button')];
                            const submit = btns.find(b => {
                                const t = (b.textContent || '').trim().toLowerCase();
                                return t === 'sign in';
                            });
                            if (submit) submit.click();
                        })()
                    """)
                    await self.sleep(5)
                else:
                    logger.warning("[Fanatical] No credentials set (FANATICAL_EMAIL/PASSWORD). Waiting for VNC...")
                    async def _fanatical_logged_in() -> bool:
                        txt = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
                        return 'claim this game' in txt or 'already claimed' in txt
                    logged_in = await self._wait_for_vnc_login(_fanatical_logged_in)
                    if not logged_in:
                        return

            current_url = await self.page.evaluate("window.location.href")
            if "fanatical.com" not in current_url or url not in current_url:
                await self.page.get(url)
                await self.sleep(4)

            claimed = False
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
                    await self.sleep(4)
                    body_after = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
                    if "claimed" in body_after or "success" in body_after or "congratulations" in body_after:
                        claimed = True
                        break
                    claimed = True
                    break
                await self.sleep(2)

            if claimed:
                logger.info("✓ [Fanatical] Claimed '%s'!", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="fanatical", user=self.user,
                        game_id=giveaway_url, title=title, url=url, status="claimed",
                    )
                    obj.status = "claimed"
                    await session.commit()
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"fanatical_{filenamify(title)}")
            else:
                logger.warning("[Fanatical] Could not click 'Claim This Game' for '%s'", title)
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")

        except Exception:
            logger.exception("[Fanatical] Error claiming '%s'", title)

    # ─────────────────────────────────────────────────────────────────────
    # Alienware Arena (Notify-Only Mode)
    # ─────────────────────────────────────────────────────────────────────
    async def _claim_alienware_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)

        notify_game = {"title": f"{title} (Alienware - manual claim required)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # Check if we already notified about this game to prevent spam
            async with async_session() as session:
                # We use status="notified" to distinctly mark these
                existing, created = await get_or_create(
                    session, store="alienware", user=self.user,
                    game_id=giveaway_url, title=title, url=url, status="notified"
                )
                
                if not created:
                    logger.info("⏭️ [Alienware] '%s' — already notified before.", title)
                    notify_game["status"] = "existed"
                    return

                # If it's new, we just notify
                logger.info("🔔 [Alienware] '%s' — Please claim manually (requires ARP points): %s", title, url)
                existing.status = "notified"
                await session.commit()

            # Set status to "manual" so it gets picked up by the summary
            notify_game["status"] = "manual"

        except Exception:
            logger.exception("[Alienware] Error processing notification for '%s'", title)

    # ─────────────────────────────────────────────────────────────────────
    # Itch.io
    # ─────────────────────────────────────────────────────────────────────
    async def _claim_itchio_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)

        notify_game = {"title": f"{title} (Itch.io)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            current_url = await self.page.evaluate("window.location.href")
            if "itch.io" not in current_url:
                await self.page.get(url)
                await self.sleep(4)

            # Check if already owned
            body_text = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
            if "you own this" in body_text or "in library" in body_text:
                logger.info("[Itch.io] '%s' already owned.", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="itchio", user=self.user,
                        game_id=giveaway_url, title=title, url=url, status="existed",
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

                    # Navigate back to game page
                    await self.page.get(url)
                    await self.sleep(4)
                else:
                    logger.warning("[Itch.io] No credentials set (ITCHIO_EMAIL/PASSWORD). Waiting for VNC...")
                    async def _itch_logged_in() -> bool:
                        txt = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
                        return 'download' in txt or 'claim' in txt or 'you own this' in txt
                    logged_in = await self._wait_for_vnc_login(_itch_logged_in)
                    if not logged_in:
                        return

            # Try to claim: click "Download or Claim" or "Claim" button
            claimed = False
            for _ in range(5):
                clicked = await self.page.evaluate("""
                    (() => {
                        const btns = [...document.querySelectorAll('button, a')];
                        const claim = btns.find(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return t.includes('download or claim') || t === 'claim'
                                || t.includes('add to collection') || t.includes('get it free');
                        });
                        if (claim) { claim.click(); return true; }
                        return false;
                    })()
                """)
                if clicked:
                    await self.sleep(3)
                    # Handle the "No thanks, just take me to the downloads" link
                    await self.page.evaluate("""
                        (() => {
                            const links = [...document.querySelectorAll('a')];
                            const skip = links.find(a => (a.textContent || '').toLowerCase().includes('no thanks'));
                            if (skip) skip.click();
                        })()
                    """)
                    await self.sleep(2)
                    claimed = True
                    break
                await self.sleep(2)

            if claimed:
                logger.info("✓ [Itch.io] Claimed '%s'!", title)
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="itchio", user=self.user,
                        game_id=giveaway_url, title=title, url=url, status="claimed",
                    )
                    obj.status = "claimed"
                    await session.commit()
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"itchio_{filenamify(title)}")
            else:
                logger.warning("[Itch.io] Could not click 'Claim' for '%s'", title)
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
            current_url = await self.page.evaluate("window.location.href")
            if "indiegala.com" not in current_url:
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

                    # Navigate back to game page
                    await self.page.get(url)
                    await self.sleep(4)
                else:
                    logger.warning("[IndieGala] No credentials set (INDIEGALA_EMAIL/PASSWORD). Waiting for VNC...")
                    async def _ig_logged_in() -> bool:
                        txt = await self.page.evaluate("(document.body?.innerText || '').toLowerCase()")
                        return 'add to library' in txt or 'in your library' in txt
                    logged_in = await self._wait_for_vnc_login(_ig_logged_in)
                    if not logged_in:
                        return

            # Try to click claim / add-to-library button
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
