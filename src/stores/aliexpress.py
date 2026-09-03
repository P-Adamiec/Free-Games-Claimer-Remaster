"""AliExpress store module – automated authentication and daily check-in coin collection.

Uses a cached, coherent browserforge Android fingerprint (injected via CDP) to stay
undetected, and reads the coin balance from the mtop API rather than the DOM.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re

import nodriver as uc

from browserforge.fingerprints import FingerprintGenerator
from browserforge.injectors.utils import InjectFunction

from src.core.claimer import BaseClaimer
from src.core.config import cfg

logger = logging.getLogger("fgc.aliexpress")

URL_LOGIN = "https://www.aliexpress.com/p/ug-login-page/login.html?fromMsite=true"
URL_COINS = "https://m.aliexpress.com/p/coin-index/index.html"
URL_HOME = "https://www.aliexpress.com/"
URL_MHOME = "https://m.aliexpress.com/"

# Coin balance comes from this mtop API (the DOM shows only animated digits).
COIN_API_PREFIX = "https://acs.aliexpress.com/h5/mtop.aliexpress.coin.execute/"

# In-page fetch/XHR interceptor stashing coin/check-in mtop responses in window.__fgcCoin (CDP handlers don't fire here).
_COIN_CAPTURE_JS = r"""
(function () {
  try {
    window.__fgcCoin = window.__fgcCoin || [];
    const want = (u) => { u = String(u || '').toLowerCase(); return u.includes('mtop') && (u.includes('coin') || u.includes('checkin') || u.includes('sign')); };
    const push = (url, text) => { try { if (window.__fgcCoin.length < 40) window.__fgcCoin.push({ url: String(url).slice(0, 220), body: String(text).slice(0, 30000) }); } catch (e) {} };
    const of = window.fetch;
    if (of) {
      window.fetch = function (...args) {
        const url = (args[0] && args[0].url) || args[0];
        const p = of.apply(this, args);
        try { if (want(url)) p.then(r => { try { r.clone().text().then(t => push(url, t)).catch(() => {}); } catch (e) {} }).catch(() => {}); } catch (e) {}
        return p;
      };
    }
    const oOpen = XMLHttpRequest.prototype.open;
    const oSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) { this.__fgcUrl = u; return oOpen.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function () {
      try { this.addEventListener('load', function () { try { if (want(this.__fgcUrl)) push(this.__fgcUrl, this.responseText); } catch (e) {} }); } catch (e) {}
      return oSend.apply(this, arguments);
    };
  } catch (e) {}
})();
"""

# Filename (inside the AliExpress browser profile dir) where the generated
# Android fingerprint is cached so the bot presents the SAME device every day.
FINGERPRINT_CACHE = "fgc_fingerprint.json"

# Field names the check-in APIs use for the day streak and tomorrow's reward.
# Deliberately strict: showing the wrong number is worse than showing none, so a
# key must say what it counts (days of a streak, coins for the next day).
_STREAK_KEY_RE = re.compile(
    r"(continuous|consecutive|streak|(sign|signin|checkin|check_in|serial)_?days?)", re.I)
_TOMORROW_KEY_RE = re.compile(
    r"(tomorrow|next_?day|next_?sign).{0,12}?(coin|reward|amount|point|num)"
    r"|(coin|reward|amount|point|num).{0,12}?(tomorrow|next_?day)", re.I)


def _flatten_payload(obj, prefix: str = "", depth: int = 0, out: dict | None = None) -> dict:
    """Flatten a mtop JSON payload into {path: scalar}.

    Handles both shapes AliExpress uses: the ``[{"name": …, "value": …}]`` lists of
    ``coin.execute`` and the plain nested objects of ``coin.channel.sign.*``. Values
    that are themselves JSON strings are parsed and walked into.
    """
    if out is None:
        out = {}
    if depth > 6 or len(out) > 400:
        return out

    if isinstance(obj, dict):
        # A {"name": …, "value": …} entry becomes a single key, like the old parser produced.
        if "name" in obj and "value" in obj and isinstance(obj.get("name"), str):
            return _flatten_payload(obj["value"], f"{prefix}.{obj['name']}" if prefix else obj["name"], depth, out)
        for key, val in obj.items():
            _flatten_payload(val, f"{prefix}.{key}" if prefix else str(key), depth + 1, out)
        return out

    if isinstance(obj, list):
        for i, item in enumerate(obj):
            # {"name": …, "value": …} entries are named already, don't index them.
            named = isinstance(item, dict) and isinstance(item.get("name"), str) and "value" in item
            _flatten_payload(item, prefix if named else f"{prefix}[{i}]", depth + 1, out)
        return out

    if isinstance(obj, str):
        text = obj.strip()
        if text.startswith(("{", "[")) and len(text) < 20000:
            try:
                return _flatten_payload(json.loads(text), prefix, depth + 1, out)
            except (json.JSONDecodeError, ValueError):
                pass

    if prefix:
        out[prefix] = obj
    return out


def _field_by_leaf(fields: dict, leaf: str):
    """Look a field up by its last path segment (paths are nested, e.g. data.userCoinsNum)."""
    for key, value in fields.items():
        if key.rsplit(".", 1)[-1] == leaf:
            return value
    return None


def _as_int(value) -> int | None:
    """Read an int out of an API value ('5', 5, '5 days'), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"\d{1,4}", value)
        if m:
            return int(m.group(0))
    return None

# Neutralise aliexpress:// / intent:// links so Chrome's 'Open xdg-open?' dialog can't pop inside VNC.
_APP_BLOCK_JS = r"""
    window.__fgcBlockedApp = window.__fgcBlockedApp || [];
    const APP_SCHEMES = ['aliexpress:', 'aliexpresshd:', 'aecmd:', 'alibaba:', 'intent:', 'market:', 'android-app:', 'alipay:', 'alipays:', 'tmall:', 'taobao:'];
    const isAppUrl = (url) => {
        if (!url) return false;
        const u = String(url).toLowerCase().trim();
        const hit = APP_SCHEMES.some(s => u.startsWith(s)) || u.includes('xdg-open');
        if (hit && window.__fgcBlockedApp.length < 20) window.__fgcBlockedApp.push(String(url).slice(0, 120));
        return hit;
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

    const neutralise = (node) => {
        try {
            if (node.tagName === 'A' && isAppUrl(node.getAttribute('href'))) {
                node.setAttribute('href', 'javascript:void(0)');
            }
            if (node.tagName === 'IFRAME' && isAppUrl(node.getAttribute('src'))) {
                node.remove();
            }
        } catch (e) {}
    };
    const sweepAll = () => {
        try { document.querySelectorAll('a[href], iframe[src]').forEach(neutralise); } catch (e) {}
    };
    try {
        const mo = new MutationObserver((muts) => {
            for (const m of muts) {
                for (const n of m.addedNodes || []) {
                    if (n.nodeType !== 1) continue;
                    neutralise(n);
                    if (n.querySelectorAll) n.querySelectorAll('a[href], iframe[src]').forEach(neutralise);
                }
                if (m.type === 'attributes' && m.target) neutralise(m.target);
            }
        });
        mo.observe(document, {
            childList: true, subtree: true, attributes: true,
            attributeFilter: ['href', 'src']
        });
        sweepAll();
        document.addEventListener('DOMContentLoaded', sweepAll);
        setTimeout(sweepAll, 600);
        setTimeout(sweepAll, 1800);
    } catch (e) {}
"""


class AliExpressClaimer(BaseClaimer):
    store_name = "aliexpress"

    # Ships its own full browserforge fingerprint, so the desktop base stealth must NOT layer on top.
    inject_base_stealth = False

    def __init__(self) -> None:
        super().__init__()
        # Wallet balance from the coin mtop API (DOM shows only animated digits); set by the network handler.
        self._user_coins: int | None = None
        self._coin_reqs: dict = {}  # requestId -> url, for coin/check-in mtop responses
        self._coin_payloads: list[dict] = []  # flattened coin/check-in responses (streak, tomorrow, balance)

    async def run(self) -> None:
        """Main entry point for the AliExpress daily check-in flow."""
        logger.debug("Starting AliExpress daily check-in flow")
        try:
            # Step 1: Launch the browser with a coherent Android fingerprint
            await self._setup_mobile_browser()

            # Step 2: warm up on the mobile home with organic activity before touching anything sensitive.
            self.logger.debug("Warming up session on mobile home page...")
            await self.page.get(URL_MHOME)
            await self._human_pause(3, 6)
            await self._dismiss_cookie_banner()
            await self._simulate_human_activity()

            # Step 3: go to the coin page...
            self.logger.debug("Navigating to mobile coins check-in page...")
            await self._goto_coins_organically()
            await self._human_pause(2, 3)

            # Step 3.5: Ensure we aren't trapped in a skeleton loading page or redirected before checking login
            await self._handle_skeleton_loading()

            # Step 4: Ensure we are actually logged in.
            if await self._is_logged_in():
                self.log_signed_in(cfg.ae_email or "AliExpress User")
            else:
                self.logger.info("Not logged in (login form shown on coin page) – authenticating...")
                if not await self._ensure_logged_in():
                    logger.error("Aborting AliExpress flow due to login failure.")
                    return
                # Return to the coin page after a successful login.
                await self._goto_coins_organically()
                await self._human_pause(4, 7)

            # Step 5: Diagnose what the anti-bot layer sees (helps tune stealth)
            await self._diagnose_page()

            # Step 6: Verify and report daily check-in status
            await self._verify_check_in()

        except Exception as exc:
            logger.exception("Fatal error during AliExpress check-in flow")
            if cfg.notify_errors:
                await self.notify(f"aliexpress failed: {exc}")
        finally:
            await self.close_browser()

    async def _setup_mobile_browser(self) -> None:
        """Launch the browser with a coherent Android mobile fingerprint.

        AliExpress' anti-bot cross-checks the UA string, Sec-CH-UA client-hint
        headers, navigator properties and the WebGL renderer – any mismatch
        between them can trigger the bot flag (1-coin check-in state).
        """
        # Load/generate one coherent fake device (UA, client-hints, screen, navigator/WebGL JS).
        fp = self._load_or_make_fingerprint()
        mobile_ua = fp["ua"]
        await self.start_browser(extra_args=[
            f"--user-agent={mobile_ua}",
        ])

        # CDP mobile device metrics + client-hints so emitted Sec-CH-UA-* agree with the fingerprint.
        self.logger.debug("Enabling CDP mobile device metrics emulation...")
        try:
            # Keep the viewport inside the physical VNC window so bottom drawers aren't cut off.
            viewport_height = (
                min(int(fp["screen_h"]), cfg.height - 40)
                if cfg.height > 100 else int(fp["screen_h"])
            )
            await self.page.send(uc.cdp.emulation.set_device_metrics_override(
                width=int(fp["screen_w"]),
                height=int(viewport_height),
                device_scale_factor=float(fp["dpr"]),
                mobile=True,
            ))
            # Client-hint metadata from the same fingerprint, forcing mobile=True (browserforge sometimes reports mobile:false).
            md = fp["ua_metadata"]
            brands = [
                uc.cdp.emulation.UserAgentBrandVersion(brand=b["brand"], version=b["version"])
                for b in md.get("brands", [])
            ]
            full_versions = [
                uc.cdp.emulation.UserAgentBrandVersion(brand=b["brand"], version=b["version"])
                for b in md.get("fullVersionList", [])
            ]
            ua_metadata = uc.cdp.emulation.UserAgentMetadata(
                platform=md.get("platform", "Android"),
                platform_version=md.get("platformVersion", ""),
                architecture=md.get("architecture", ""),
                model=md.get("model", ""),
                mobile=True,
                brands=brands,
                full_version_list=full_versions,
                full_version=md.get("uaFullVersion", ""),
                bitness=md.get("bitness", ""),
                wow64=False,
            )
            await self.page.send(uc.cdp.emulation.set_user_agent_override(
                user_agent=mobile_ua,
                # Raw language list (no q-values); Chrome appends the q-factors.
                accept_language=fp["accept_language"],
                platform=fp["platform"],
                user_agent_metadata=ua_metadata,
            ))
        except Exception as e:
            self.logger.debug("CDP emulation override exception: %s", e)

        # Block app-scheme requests (aliexpress://, intent://…) that pop 'Open xdg-open?' dialogs.
        try:
            await self.page.send(uc.cdp.network.enable())
            await self.page.send(uc.cdp.network.set_blocked_urls(urls=[
                "*aliexpress://*", "*aliexpresshd://*", "*aecmd://*", "*alibaba://*",
                "*intent://*", "*market://*", "*android-app://*",
                "*alipay://*", "*alipays://*", "*tmall://*", "*taobao://*",
                "aliexpress:*", "aliexpresshd:*", "aecmd:*", "alibaba:*",
                "intent:*", "market:*", "android-app:*",
                "alipay:*", "alipays:*", "tmall:*", "taobao:*",
            ]))
        except Exception as e:
            self.logger.debug("CDP set_blocked_urls exception: %s", e)

        # Inject fingerprint + app-block + coin-capture at document-start (Page.enable first, else it's a no-op).
        try:
            await self.page.send(uc.cdp.page.enable())
            await self.page.send(
                uc.cdp.page.add_script_to_evaluate_on_new_document(
                    source=fp["inject_js"],
                )
            )
            await self.page.send(
                uc.cdp.page.add_script_to_evaluate_on_new_document(
                    source=_APP_BLOCK_JS,
                )
            )
            await self.page.send(
                uc.cdp.page.add_script_to_evaluate_on_new_document(
                    source=_COIN_CAPTURE_JS,
                )
            )
        except Exception as e:
            self.logger.debug("Fingerprint / app-block JS injection exception: %s", e)

        # Start listening for the coin balance API response before we navigate.
        self._install_coin_listener()

    # ------------------------------------------------------------------
    # Anti-detection helpers
    # ------------------------------------------------------------------

    def _load_or_make_fingerprint(self) -> dict:
        """Return a stable Android-phone fingerprint bundle for this profile.

        The bundle (UA string, client-hint metadata, screen size and the
        browserforge injection JS) is cached on disk inside the AliExpress
        browser profile, so the bot presents the SAME phone on every run, a
        device whose identity changes between visits is itself a bot signal.
        Regenerates only when the cache is missing or unreadable.
        """
        cache_path = cfg.browser_dir / self.store_name / FINGERPRINT_CACHE
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("inject_js") and data.get("ua"):
                    self.logger.debug("Loaded cached AliExpress fingerprint (%s).", data.get("ua"))
                    return data
        except Exception as e:
            self.logger.debug("Fingerprint cache read failed (%s), regenerating.", e)

        # Fresh Android mobile Chrome fingerprint; browserforge keeps UA/headers/screen/navigator/WebGL consistent.
        fp = FingerprintGenerator().generate(
            browser=("chrome",), os=("android",), device=("mobile",),
        )

        # Force userAgentData.mobile true so it agrees with the mobile UA (browserforge sometimes reports false).
        ua_data = dict(fp.navigator.userAgentData or {})
        ua_data["mobile"] = True
        fp.navigator.userAgentData = ua_data

        # Raw language list (no q-values) for CDP's accept_language override.
        languages = [str(l).strip() for l in (fp.navigator.languages or ["en-US", "en"])]
        accept_language = ",".join(languages) if languages else "en-US,en"

        data = {
            "ua": fp.navigator.userAgent,
            "platform": fp.navigator.platform,
            "accept_language": accept_language,
            "screen_w": fp.screen.width,
            "screen_h": fp.screen.height,
            "dpr": fp.screen.devicePixelRatio,
            "ua_metadata": ua_data,
            # The full navigator/screen/WebGL override script for this device.
            "inject_js": InjectFunction(fp),
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data), encoding="utf-8")
            self.logger.debug("Generated new AliExpress fingerprint: %s", data["ua"])
        except Exception as e:
            self.logger.debug("Fingerprint cache write failed: %s", e)
        return data

    # ------------------------------------------------------------------
    # Coin-balance API interception
    # ------------------------------------------------------------------

    def _install_coin_listener(self) -> None:
        """Capture the wallet balance from the coin mtop API response.

        The coin page shows the balance only as rotating digit animations, so
        the DOM can't be read reliably. Instead we watch the network for the
        POST to ``mtop.aliexpress.coin.execute`` (which the page itself fires)
        and parse ``userCoinsNum`` out of its JSON body, the same source the
        original free-games-claimer reads.
        """
        try:
            self.page.add_handler(uc.cdp.network.ResponseReceived, self._on_coin_response)
            self.page.add_handler(uc.cdp.network.LoadingFinished, self._on_coin_loading_finished)
        except Exception as e:
            self.logger.debug("Could not install coin API listener: %s", e)

    async def _on_coin_response(self, event) -> None:
        """Record coin / check-in mtop responses so their body can be read on finish.

        Broad match (not just the US coin.execute prefix) because the real
        endpoint differs by region/mobile, the diagnostic dump below reveals it.
        """
        try:
            url = getattr(getattr(event, "response", None), "url", "") or ""
            u = url.lower()
            if (url.startswith(COIN_API_PREFIX)
                    or ("mtop" in u and ("coin" in u or "checkin" in u or "sign" in u))
                    or ("acs." in u and "coin" in u)):
                self._coin_reqs[event.request_id] = url
        except Exception:
            pass

    async def _on_coin_loading_finished(self, event) -> None:
        """Read a coin/check-in mtop body: keep the latest userCoinsNum and the
        flattened response, same as the in-page capture path."""
        try:
            rid = event.request_id
            url = self._coin_reqs.pop(rid, None)
            if url is None:
                return
            body, b64 = await self.page.send(uc.cdp.network.get_response_body(rid))
            if b64 and isinstance(body, str):
                body = base64.b64decode(body).decode("utf-8", "ignore")
            payload = json.loads(body)
            api = payload.get("api") if isinstance(payload, dict) else None
            fields = _flatten_payload(payload.get("data") if isinstance(payload, dict) else payload)
            if fields:
                self._coin_payloads.append({"api": api or url, "url": url, "fields": fields, "body": body})
            coins = _as_int(_field_by_leaf(fields, "userCoinsNum"))
            if coins is not None:
                self._user_coins = coins
                self.logger.debug("🪙 Wallet balance (userCoinsNum): %s", self._user_coins)
            self.logger.debug("🔬 Coin API captured: api=%s fields=%s", api, sorted(fields.keys())[:40])
        except Exception as e:
            self.logger.debug("Coin API body parse failed: %s", e)

    async def _read_coin_api(self) -> None:
        """Read coin/check-in mtop responses captured in-page by _COIN_CAPTURE_JS.

        Sets the wallet balance (userCoinsNum) and keeps every payload flattened in
        ``self._coin_payloads`` so the streak / tomorrow fields can be read from the
        API instead of the animated DOM. Must be called while still on the coin page
        (window.__fgcCoin resets on navigation).
        """
        try:
            raw = await self.page.evaluate("JSON.stringify(window.__fgcCoin || [])")
            items = json.loads(raw) if isinstance(raw, str) else []
        except Exception as e:
            self.logger.debug("Coin capture read failed: %s", e)
            return
        if not items:
            self.logger.debug("🔬 Coin API: nothing captured by in-page interceptor.")
            return
        for it in items:
            url = it.get("url", "")
            body = it.get("body", "")
            try:
                payload = json.loads(body)
            except Exception:
                self.logger.debug("🔬 Coin API (non-JSON): url=%s body=%s", url, body[:200])
                continue
            api = payload.get("api") if isinstance(payload, dict) else None
            fields = _flatten_payload(payload.get("data") if isinstance(payload, dict) else payload)
            # __fgcCoin keeps every response, so the same one is re-read on the next call.
            if fields and not any(p["api"] == (api or url) and p["body"] == body for p in self._coin_payloads):
                self._coin_payloads.append({"api": api or url, "url": url, "fields": fields, "body": body})
            self.logger.debug(
                "🔬 Coin API: api=%s fields=%s",
                api,
                json.dumps(fields, ensure_ascii=False, default=str)[:1500],
            )
            coins = _as_int(_field_by_leaf(fields, "userCoinsNum"))
            if coins is not None and self._user_coins is None:
                self._user_coins = coins
                self.logger.debug("🪙 Wallet balance (userCoinsNum): %s", self._user_coins)
        self._dump_coin_payloads()

    def _dump_coin_payloads(self) -> None:
        """Write the captured check-in/coin responses to data/ae_coin_api.json (last run only)."""
        if not self._coin_payloads:
            return
        try:
            dump = [
                {"api": p["api"], "url": p["url"], "field_names": sorted(p["fields"].keys()), "body": p["body"]}
                for p in self._coin_payloads
            ]
            path = cfg._data_dir / "ae_coin_api.json"
            path.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            self.logger.debug("Coin API dump write failed: %s", e)

    def _extract_checkin_calendar(self) -> dict:
        """Read the streak and tomorrow's coins from the check-in calendar.

        ``coin.channel.sign.list`` returns ``dailySignNodeList``: one entry per day,
        ``calendarDayDistance`` 0 for today and 1 for tomorrow, each carrying its own
        prize and a ``sequenceNumber`` that counts the day of the running streak.
        """
        info: dict = {"streak": None, "tomorrow": None}
        for payload in self._coin_payloads:
            if "sign.list" not in str(payload.get("api") or ""):
                continue
            try:
                data = (json.loads(payload["body"]).get("data") or {}).get("data") or {}
                nodes = [n for seq in (data.get("signQuerySequenceNodeList") or [])
                         for n in (seq.get("dailySignNodeList") or [])]
            except Exception as e:
                self.logger.debug("Check-in calendar unreadable: %s", e)
                continue

            past_all_signed = True
            for node in nodes:
                distance = node.get("calendarDayDistance")
                result = (node.get("signResultList") or [{}])[0]
                coins = next((_as_int(p.get("prizeAmount")) for p in (result.get("prizeInfoList") or [])
                              if p.get("prizeType") == "coins"), None)
                if distance == 0:
                    info["streak"] = _as_int(result.get("sequenceNumber"))
                elif distance == 1 and coins is not None:
                    info["tomorrow"] = coins
                elif isinstance(distance, int) and distance < 0 and not result.get("signSuccess"):
                    past_all_signed = False

            # A missed day in the visible window means sequenceNumber is not the streak.
            if info["streak"] is not None and not past_all_signed:
                self.logger.debug("Check-in calendar shows a missed day, streak counter not trusted")
                info["streak"] = None
            if info["streak"] is not None or info["tomorrow"] is not None:
                self.logger.debug("🔬 Check-in calendar: streak=%s tomorrow=%s (%d day node(s))",
                                  info["streak"], info["tomorrow"], len(nodes))
                return info
        return info

    def _extract_checkin_info_from_api(self) -> dict:
        """Find the day streak and tomorrow's reward in the captured check-in payloads."""
        info = self._extract_checkin_calendar()
        if info["streak"] is not None and info["tomorrow"] is not None:
            return info
        if not self._coin_payloads:
            return info

        # sign.execute answers the collect itself, so it carries the freshest streak.
        def rank(p: dict) -> int:
            api = str(p.get("api") or "")
            if "sign.execute" in api:
                return 0
            return 1 if "sign" in api else 2

        streak_key = "calendar" if info["streak"] is not None else None
        tomorrow_key = "calendar" if info["tomorrow"] is not None else None
        for payload in sorted(self._coin_payloads, key=rank):
            for key, value in payload["fields"].items():
                leaf = key.rsplit(".", 1)[-1]
                num = _as_int(value)
                if num is None:
                    continue
                if info["streak"] is None and _STREAK_KEY_RE.search(leaf) and 0 <= num <= 999:
                    info["streak"], streak_key = num, f"{payload['api']}:{key}"
                if info["tomorrow"] is None and _TOMORROW_KEY_RE.search(leaf) and 0 < num <= 9999:
                    info["tomorrow"], tomorrow_key = num, f"{payload['api']}:{key}"
            if info["streak"] is not None and info["tomorrow"] is not None:
                break

        # One counter matching both slots means the pattern hit a single field twice.
        if streak_key and streak_key != "calendar" and streak_key == tomorrow_key:
            info["tomorrow"], tomorrow_key = None, None

        self.logger.debug(
            "🔬 Check-in info from API: streak=%s (%s) tomorrow=%s (%s)",
            info["streak"], streak_key or "not found", info["tomorrow"], tomorrow_key or "not found",
        )
        if info["streak"] is None or info["tomorrow"] is None:
            self._log_rejected_candidates()
        return info

    def _log_rejected_candidates(self) -> None:
        """List numeric fields that look related but did not pass the strict match."""
        hint = re.compile(r"(day|sign|streak|tomorrow|next|coin|reward)", re.I)
        for payload in self._coin_payloads:
            near = {k: v for k, v in payload["fields"].items()
                    if hint.search(k.rsplit(".", 1)[-1]) and _as_int(v) is not None}
            if near:
                self.logger.debug("🔬 Check-in candidates not used (%s): %s", payload["api"], near)

    async def _human_pause(self, lo: float, hi: float) -> None:
        """Sleep a random, human-like amount of time (fixed robotic delays are a bot signal)."""
        await self.sleep(random.uniform(lo, hi))

    async def _simulate_human_activity(self) -> None:
        """Generate organic mouse-move / scroll / touch signals.

        Alibaba's behavioural collector scores a session partly on real
        pointer activity gathered over time. A browser that never moves the
        mouse or scrolls before acting looks automated, which contributes to
        the low-trust '1 coin' state. This produces a few realistic events.
        """
        try:
            width = 450
            height = min(800, cfg.height - 40) if cfg.height > 100 else 680
            for _ in range(random.randint(2, 4)):
                x = random.randint(30, width - 30)
                y = random.randint(80, height - 120)
                try:
                    await self.page.send(uc.cdp.input_.dispatch_mouse_event(
                        type_="mouseMoved", x=float(x), y=float(y),
                    ))
                except Exception:
                    pass
                await self._human_pause(0.3, 0.9)
            for _ in range(random.randint(1, 3)):
                await self.page.scroll_down(random.randint(15, 40))
                await self._human_pause(0.6, 1.5)
            await self.page.scroll_up(random.randint(10, 25))
            await self._human_pause(0.5, 1.2)
        except Exception as e:
            self.logger.debug("Human activity simulation exception: %s", e)

    async def _dismiss_cookie_banner(self) -> None:
        """Accept the cookie consent banner if present (native click)."""
        for label in ("Accept cookies", "Accept all", "Akceptuj", "Zaakceptuj wszystko", "Allow all"):
            try:
                btn = await self.page.find(label, timeout=1.5)
                if btn:
                    await self._human_pause(0.4, 1.0)
                    await btn.click()
                    self.logger.debug("Accepted cookie banner via '%s'", label)
                    await self._human_pause(0.6, 1.2)
                    return
            except Exception:
                pass

    async def _goto_coins_organically(self) -> None:
        """Reach the coin page by tapping an in-page link when possible.

        A real user taps their way to the coins page; a direct URL load is
        cheaper to fingerprint. We try to click a coins/rewards entry point and
        only fall back to a direct navigation if none is found.
        """
        try:
            clicked = await self.page.evaluate(r"""
                (() => {
                    const links = [...document.querySelectorAll('a[href]')];
                    const hit = links.find(a => /coin-index|\/coin|coins/i.test(a.getAttribute('href') || ''));
                    if (hit && hit.offsetParent !== null) { hit.scrollIntoView(); return true; }
                    return false;
                })()
            """)
            if clicked:
                await self._human_pause(0.6, 1.4)
                link = await self.page.find("Coins", timeout=2)
                if link:
                    await link.click()
                    await self._human_pause(3, 5)
        except Exception as e:
            self.logger.debug("Organic coins navigation exception: %s", e)

        # Ensure we actually ended up on the coin page (fallback to direct load)
            try:
                url = str(await self.page.evaluate("window.location.href"))
            except Exception:
                url = ""
            if "/p/coin-index/" not in url:
                self.logger.debug("Organic tap did not reach the coin page (url=%s), loading it directly", url or "?")
                await self.page.get(URL_COINS)
                # since the skeleton loading function was added I removed the pause here.

            await self._handle_skeleton_loading()

    async def _handle_skeleton_loading(self, max_retries: int = 3) -> None:
        """Wait out the login-pending screen, or reload if stuck/redirected."""
        self.logger.debug("Checking for login-pending container...")
        for attempt in range(max_retries):
            is_pending = False
            is_coin_page = False

            # Poll once per second for up to 4 seconds to see if it clears naturally
            for _ in range(4):
                try:
                    raw = await self.page.evaluate(r"""
                        (() => {
                            const pending = document.querySelector('.login-pending-container');
                            return JSON.stringify({
                                pending: !!pending,
                                url: location.href.toLowerCase()
                            });
                        })()
                    """)
                    import json
                    info = json.loads(raw) if isinstance(raw, str) else {}
                except Exception:
                    info = {"pending": False, "url": ""}

                is_coin_page = "/p/coin-index/" in info.get("url", "")
                is_pending = info.get("pending")

                # The coin page loaded and the pending overlay is gone
                if is_coin_page and not is_pending:
                    self.logger.debug("Coin page ready (login-pending cleared). Settling DOM...")
                    await self._human_pause(1.5, 2.5)
                    return

                await self.sleep(1)

            # Still stuck with the pending container after 4 seconds
            if is_pending:
                self.logger.warning("⏳ Stuck on login-pending container. Refreshing (%d/%d)...", attempt + 1,
                                    max_retries)
                await self.page.reload()

                # Progressive backoff: adds 1.5s to the wait time for each consecutive failure incase this is a potato server
                # Attempt 1: 2-4s | Attempt 2: 3.5-5.5s | Attempt 3: 5-7s
                extra_wait = attempt * 1.5
                await self._human_pause(2.0 + extra_wait, 4.0 + extra_wait)
                continue

            # Bounced off to the homepage or somewhere else
            if not is_coin_page:
                self.logger.warning("Redirected off coin page to %s. Reloading coin page...",
                                    info.get("url", "unknown"))
                await self.page.get(URL_COINS)

                #shorter pause if we just need to re-navigate. It only navigates it the bot sat too long on the loading page
                # so the bot should refresh sooner before Aliexpress sends it back to home page
                await self._human_pause(1.0, 2.0)
                continue

    async def _diagnose_page(self) -> None:
        """Log what an anti-bot layer can observe. Diagnostic aid while tuning stealth.

        Reveals automation leaks (navigator.webdriver, cdc_ globals), whether
        AliExpress issued a security challenge (x5sec cookie / punish page),
        and which trust cookies exist – so failures produce actionable data
        instead of guesswork.
        """
        try:
            raw = await self.page.evaluate(r"""
                (() => {
                    const cookies = document.cookie || '';
                    const names = cookies.split(';').map(c => c.trim().split('=')[0]).filter(Boolean);
                    const cdcKeys = Object.keys(window).filter(k => /cdc_|\$cdc|selenium|driver|webdriver|__nightmare|domAutomation/i.test(k));
                    // Real challenge = x5sec cookie / punish URL / baxia container, NOT the words "slider"/"captcha" (benign promos).
                    const punishUrl = /punish|x5referer|_____tmd_____|\/_____|sec\.aliexpress/i.test(location.href);
                    const challengeEl = document.querySelector(
                        '#baxia-dialog, .baxia-dialog, [id^="nc_"], .nc-container, .nc_wrapper, #nocaptcha, .nocaptcha, .J_MIDDLEWARE_FRAME_WIDGET'
                    );
                    return JSON.stringify({
                        webdriver: navigator.webdriver,
                        cdcLeaks: cdcKeys,
                        hasX5sec: names.includes('x5sec'),
                        hasM_h5_tk: names.some(n => n.startsWith('_m_h5_tk')),
                        hasCna: names.includes('cna'),
                        hasXmanT: names.includes('xman_t'),
                        cookieCount: names.length,
                        challenge: names.includes('x5sec') || punishUrl || !!challengeEl,
                        pluginsLen: navigator.plugins.length,
                        touchPoints: navigator.maxTouchPoints,
                        blockedAppUrls: (window.__fgcBlockedApp || []),
                        url: location.href
                    });
                })()
            """)
            data = json.loads(raw) if isinstance(raw, str) else {}
            self.logger.debug(
                "🔎 Anti-bot diagnostics: webdriver=%s cdcLeaks=%s x5sec=%s challenge=%s "
                "cookies=%d (m_h5_tk=%s cna=%s xman_t=%s) touchPoints=%s plugins=%s",
                data.get("webdriver"), data.get("cdcLeaks"), data.get("hasX5sec"),
                data.get("challenge"), data.get("cookieCount", 0), data.get("hasM_h5_tk"),
                data.get("hasCna"), data.get("hasXmanT"), data.get("touchPoints"),
                data.get("pluginsLen"),
            )
            blocked = data.get("blockedAppUrls") or []
            if blocked:
                self.logger.debug(
                    "🚧 Blocked %d in-page app-launch attempt(s) (schemes AliExpress tried to open): %s",
                    len(blocked), blocked)
            if data.get("hasX5sec") or data.get("challenge"):
                self.logger.warning(
                    "⚠️ AliExpress issued a security challenge (x5sec/punish) – "
                    "this session is being risk-scored. This is the root cause of the 1-coin cap.")
        except Exception as e:
            self.logger.debug("Diagnostics probe failed: %s", e)

    # ------------------------------------------------------------------
    # Login & Authentication
    # ------------------------------------------------------------------

    async def _is_logged_in(self) -> bool:
        """Return True only on a POSITIVE logged-in signal.

        AliExpress renders its login form INLINE on the coin URL (the URL stays
        `/p/coin-index/index.html`) when the session is invalid, with a
        'Kontynuuj'/'Continue' button rather than 'Sign in'. The old check
        defaulted to "logged in" for any aliexpress.com URL that wasn't
        literally `/login`, so it false-positived on that inline form (and on
        the logged-out home page) and skipped login entirely. This version
        detects the login form explicitly and, when uncertain, returns False so
        login is attempted, a false negative self-corrects because login.html
        just redirects away when we're already signed in.
        """
        try:
            res = await self.page.evaluate(r"""
                (() => {
                    const url = window.location.href.toLowerCase();
                    const text = (document.body ? (document.body.textContent || '') : '').toLowerCase();
                    const visible = (el) => !!el && el.offsetParent !== null;

                    // Explicit login page URL
                    if (url.includes('/login') || url.includes('login.html') || url.includes('ug-login-page')) return false;

                    // Inline login form (email/phone input, or a welcome prompt + Continue/Sign-in button).
                    const hasLoginInput = !!document.querySelector(
                        'input[type="email"], input[placeholder*="mail" i], input[placeholder*="phone" i], input[placeholder*="telefon" i]'
                    );
                    const loginPrompt = /witamy na aliexpress|welcome to aliexpress|problemy z logowaniem|trouble signing in|szybki dost[eę]p|quick access|sign in with/i.test(text);
                    const hasLoginBtn = [...document.querySelectorAll('button, div[role="button"], a')].some(el =>
                        /^(kontynuuj|continue|log in|sign in|zaloguj( si[eę])?)$/i.test((el.textContent || '').trim()) && visible(el)
                    );
                    if (hasLoginInput || (loginPrompt && hasLoginBtn)) return false;

                    // Positive authenticated coin-page signals
                    if (/day streak|seria|coins tomorrow|check-in coins|monety za zameldowanie|moje monety|earn more coins|zdob[aą]d[źz] wi[eę]cej/i.test(text)) return true;

                    // A visible Collect / check-in button also implies an authenticated coin page
                    const collectBtn = [...document.querySelectorAll('button, div[role="button"], span, a')].some(el =>
                        /^(collect|odbierz|check[- ]?in|zamelduj)/i.test((el.textContent || '').trim()) && visible(el)
                    );
                    if (collectBtn) return true;

                    // Signed-in store homepage: only sign-out / "My AliExpress" labels (never "my orders"/"wishlist", which show logged-out too).
                    if (location.hostname.toLowerCase().endsWith('aliexpress.com') &&
                        /wyloguj|sign out|log out|moje aliexpress|my aliexpress/i.test(text)) {
                        return true;
                    }

                    // Uncertain → treat as NOT logged in so authentication is attempted.
                    return false;
                })()
            """)
            return bool(res)
        except Exception as e:
            self.logger.debug("Error checking login state: %s", e)
            return False

    async def _left_login_for_store(self) -> bool:
        """Login-flow success signal: redirected OFF the login/passport page onto
        an aliexpress.com store page with no login form still present.

        A successful sign-in lands on e.g. ``pl.aliexpress.com/?gatewayAdapt=glo2pol``,
        which carries none of the coin-page's logged-in markers, so
        ``_is_logged_in()`` alone reported a false "verification required" and
        fired a needless VNC alert. This mirrors the upstream project's
        ``waitForURL(startsWith('https://www.aliexpress.com/'))`` success check.
        Only meaningful right after a login attempt: on the bare login page the
        email/password inputs are present, so this returns False there.
        """
        try:
            info = await self.page.evaluate(r"""
                (() => {
                    const url = location.href.toLowerCase();
                    const onLogin = url.includes('/login') || url.includes('login.html') ||
                        url.includes('ug-login-page') || url.includes('passport') || url.includes('/register');
                    const onAli = location.hostname.toLowerCase().endsWith('aliexpress.com');
                    const hasPwd = !!document.querySelector('input[type="password"]');
                    const hasEmail = !!document.querySelector(
                        'input[type="email"], input[placeholder*="mail" i], input[placeholder*="telefon" i], input[placeholder*="phone" i]');
                    return JSON.stringify({ onLogin, onAli, hasPwd, hasEmail });
                })()
            """)
            d = json.loads(info) if isinstance(info, str) else {}
            return bool(d.get("onAli") and not d.get("onLogin") and not d.get("hasPwd") and not d.get("hasEmail"))
        except Exception as e:
            self.logger.debug("Left-login-for-store check failed: %s", e)
            return False

    async def _login_ok(self) -> bool:
        """Signed-in if EITHER a positive coin/account signal shows, OR we've been
        redirected off the login page onto the aliexpress.com store."""
        return await self._is_logged_in() or await self._left_login_for_store()

    async def _find_first_login_input(self):
        """Return a handle to the email/phone field on AliExpress' login step.

        The field is a plain `type=text` input with a LOCALIZED placeholder
        (e.g. Polish "Adres e-mail lub numer telefonu"), so English /
        `type=email` selectors miss it entirely, which is why automated login
        silently failed and fell back to a bogus "6-digit code" prompt. We try
        locale-agnostic selectors, then a JS fallback that marks the first
        visible non-password input and selects it back.
        """
        selectors = [
            'input[type="email"]', 'input[type="tel"]',
            'input[placeholder*="mail" i]', 'input[placeholder*="phone" i]',
            'input[placeholder*="telefon" i]', 'input[name*="email" i]',
            'input[id*="email" i]', 'input[name*="account" i]',
        ]
        for sel in selectors:
            try:
                el = await self.page.select(sel, timeout=1.2)
                if el:
                    return el
            except Exception:
                pass
        try:
            marked = await self.page.evaluate(r"""
                (() => {
                    const skip = ['password','hidden','checkbox','radio','submit','button','file'];
                    const inputs = [...document.querySelectorAll('input')].filter(i =>
                        !skip.includes((i.type || 'text').toLowerCase()) && i.offsetParent !== null);
                    if (inputs.length) { inputs[0].setAttribute('data-fgc-login', '1'); return true; }
                    return false;
                })()
            """)
            if marked:
                return await self.page.select('input[data-fgc-login="1"]', timeout=1.5)
        except Exception as e:
            self.logger.debug("JS login-input fallback failed: %s", e)
        return None

    async def _click_button_by_text(self, texts: list[str]) -> bool:
        """Trusted-click the visible button/link whose exact text matches one of
        `texts`. Marks the real element (closest button/[role=button]/a) in JS,
        then clicks it via nodriver, so the click lands on the button element
        (not a child text node, which AliExpress' 'Kontynuuj' handler ignores)
        and is a trusted event.
        """
        try:
            payload = json.dumps([t.lower() for t in texts])
            marked = await self.page.evaluate(
                "(() => { const targets = %s;"
                " const els = [...document.querySelectorAll('button, div[role=\"button\"], a, span')];"
                " for (const el of els) { const t = (el.textContent || '').trim().toLowerCase();"
                "   if (targets.includes(t) && el.offsetParent !== null) {"
                "     const btn = el.closest('button, div[role=\"button\"], a') || el;"
                "     btn.setAttribute('data-fgc-btn', '1'); return true; } }"
                " return false; })()" % payload
            )
            if not marked:
                return False
            el = await self.page.select('[data-fgc-btn="1"]', timeout=2)
            if el:
                await el.click()
                try:
                    await self.page.evaluate(
                        "document.querySelectorAll('[data-fgc-btn]').forEach(e => e.removeAttribute('data-fgc-btn'))")
                except Exception:
                    pass
                return True
        except Exception as e:
            self.logger.debug("Button-by-text click failed: %s", e)
        return False

    async def _dump_login_state(self) -> None:
        """Log the login page's visible inputs/buttons (and save HTML) when
        automated login stalls, so the real DOM can be diagnosed instead of
        guessing which selector/click failed.
        """
        try:
            raw = await self.page.evaluate(r"""
                (() => {
                    const vis = (el) => !!el && el.offsetParent !== null;
                    const inputs = [...document.querySelectorAll('input')].map(i => ({
                        type: i.type, name: i.name, id: i.id, placeholder: i.placeholder,
                        value: (i.type === 'password' ? '***' : (i.value || '').slice(0, 40)),
                        visible: vis(i)
                    }));
                    const buttons = [...document.querySelectorAll('button, div[role="button"], a')]
                        .filter(vis).map(b => (b.textContent || '').trim().slice(0, 40)).filter(Boolean).slice(0, 30);
                    return JSON.stringify({ url: location.href, inputs: inputs, buttons: buttons });
                })()
            """)
            info = json.loads(raw) if isinstance(raw, str) else {}
            self.logger.warning("🧪 Login-stall DOM: url=%s", info.get("url"))
            self.logger.warning("🧪 Login-stall inputs=%s", info.get("inputs"))
            self.logger.warning("🧪 Login-stall buttons=%s", info.get("buttons"))
        except Exception as e:
            self.logger.debug("Login-state probe failed: %s", e)
        try:
            html = await self.page.evaluate("document.documentElement.outerHTML")
            if isinstance(html, str):
                (cfg._data_dir / "ae_login_fail.html").write_text(html, encoding="utf-8")
                self.logger.warning("🧪 Saved login page HTML to data/ae_login_fail.html")
        except Exception as e:
            self.logger.debug("Login HTML dump failed: %s", e)
        try:
            await self.take_screenshot("ae_login_fail")
        except Exception:
            pass

    async def _ensure_logged_in(self) -> bool:
        """Verify login status via direct login link, attempt automated login, or fall back to VNC for OTP code."""
        await self.sleep(2)

        self.logger.debug("Opening direct login link to check/perform authentication...")
        await self.page.get(URL_LOGIN)
        await self.sleep(4)

        # 1. If already logged in, AliExpress automatically redirects away from login.html
        if await self._login_ok():
            self.logger.info("Session verified: already logged in (redirected from login page)!")
            self.log_signed_in(cfg.ae_email or "AliExpress User")
            return True

        self.logger.debug("On login page. Proceeding with authentication...")

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
                    # Fresh login screen: find and enter the email/phone first.
                    email_el = await self._find_first_login_input()

                    if email_el:
                        self.logger.debug("Email input found. Entering email...")
                        await email_el.click()
                        await self.sleep(0.5)
                        await email_el.send_keys(cfg.ae_email)
                        await self.sleep(0.8)

                        # Submit email via Enter + a trusted click on the real button (plain text click didn't advance the form).
                        self.logger.debug("Submitting email (Enter + Continue button)...")
                        try:
                            await email_el.send_keys("\r")
                        except Exception:
                            pass
                        await self.sleep(2)
                        if not await self._click_button_by_text(["Continue", "Kontynuuj", "Next", "Dalej", "Weiter"]):
                            cont_btn = await self.page.find("Continue", timeout=2)
                            if not cont_btn:
                                cont_btn = await self.page.find("Kontynuuj", timeout=2)
                            if cont_btn:
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
                    self.logger.debug("ℹ️ AliExpress remembered account! (Password input available directly without entering email)")

                if pass_el:
                    self.logger.debug("Entering password...")
                    await pass_el.click()
                    await self.sleep(0.5)
                    await pass_el.send_keys(cfg.ae_password)
                    await self.sleep(0.8)

                    # Submit password via Enter + a trusted click on the real button (same fix as the email step).
                    self.logger.debug("Submitting password (Enter + Sign-in button)...")
                    try:
                        await pass_el.send_keys("\r")
                    except Exception:
                        pass
                    await self.sleep(2)
                    if not await self._click_button_by_text(
                        ["Sign in", "Sign In", "Zaloguj", "Zaloguj się", "Log in", "Anmelden"]
                    ):
                        sign_btn = None
                        for label in ("Sign in", "Zaloguj", "Log in"):
                            try:
                                sign_btn = await self.page.find(label, timeout=2)
                            except Exception:
                                sign_btn = None
                            if sign_btn:
                                await sign_btn.click()
                                break
                    await self.sleep(6)
            except Exception as e:
                self.logger.debug("Automated login steps encountered an exception: %s", e)

            if await self._login_ok():
                self.log_signed_in(cfg.ae_email or "AliExpress User")
                return True

        # 3. Fallback to VNC manual login if 6-digit verification code or CAPTCHA is required
        await self._dump_login_state()
        self.logger.warning("⚠️ Verification required (e.g., 6-digit email verification code or CAPTCHA)!")
        
        custom_msg = self._vnc_notice(
            "AliExpress: verification required",
            "Enter the 6-digit verification code from your email, or complete manual login in the browser.",
        )
        if await self._wait_for_vnc_login(self._login_ok, custom_msg=custom_msg):
            self.log_signed_in(cfg.ae_email or "AliExpress User")
            return True

        self.logger.error("Timed out waiting for AliExpress login.")
        return False

    # ------------------------------------------------------------------
    # Check-in Verification
    # ------------------------------------------------------------------

    async def _dismiss_overlays(self) -> None:
        """Dismiss double-coin or promotional modals if present (.hideDoubleButton)."""
        try:
            hide_btn = await self.page.select('.hideDoubleButton', timeout=1.5)
            if hide_btn:
                self.logger.debug("🧹 Dismissing double-coin / promotional overlay button...")
                await hide_btn.click()
                await self.sleep(1)
        except Exception:
            pass

    async def _read_checkin_state(self) -> dict:
        """Read today's check-in state from the coin page.

        Returns a dict with:
            claimed    – True when today's coins were already collected
            btnText    – text of the visible Collect/Check-in button (or null)
            todayCoins – how many coins today's check-in offers (or null if unknown)

        When AliExpress bot-flags the session it offers only 1 coin instead of
        the full daily amount, so the caller uses todayCoins to decide whether
        collecting is safe.
        """
        try:
            res = await self.page.evaluate(r"""
                (() => {
                    const isVisible = (el) => !!el && el.offsetParent !== null;
                    const els = [...document.querySelectorAll('button, div[role="button"], span, a, div')];
                    // Match only real check-in button labels like "Collect", "Collect 70",
                    // "Odbierz monety" – NOT promo texts like "Odbierz kupon 5$".
                    const collectRe = /^(collect|odbierz|claim|check[- ]?in|zamelduj si[eę])(\s+\+?\d+)?(\s+(coins?|monet\w*))?$/i;
                    const earnRe = /^(earn more coins|zdob[aą]d[źz] wi[eę]cej)/i;

                    let btnText = null;
                    let earnText = null;
                    let todayCoins = null;

                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        if (!t || t.length > 40 || !isVisible(el)) continue;
                        if (btnText === null && collectRe.test(t)) {
                            btnText = t;
                            const m = t.match(/(\d+)/);
                            if (m) todayCoins = parseInt(m[1], 10);
                        }
                        if (earnText === null && earnRe.test(t)) earnText = t;
                    }

                    // Fallback: read today's amount from the check-in calendar chip
                    // marked "Today" / "Dziś" (its container shows the coin value).
                    if (todayCoins === null) {
                        const todayRe = /^(today|dzi[śs]|dzisiaj)$/i;
                        for (const el of document.querySelectorAll('*')) {
                            const own = (el.childElementCount === 0 ? (el.textContent || '') : '').trim();
                            if (!todayRe.test(own) || !isVisible(el)) continue;
                            let node = el;
                            for (let up = 0; up < 3 && node.parentElement; up++) {
                                node = node.parentElement;
                                const m = (node.textContent || '').match(/\+?\s*(\d+)/);
                                if (m) { todayCoins = parseInt(m[1], 10); break; }
                            }
                            if (todayCoins !== null) break;
                        }
                    }

                    // nodriver does not serialise JS objects into Python dicts,
                    // so return a JSON string and parse it on the Python side.
                    return JSON.stringify({
                        claimed: btnText === null && earnText !== null,
                        btnText: btnText,
                        earnText: earnText,
                        todayCoins: todayCoins
                    });
                })()
            """)
            if isinstance(res, str):
                parsed = json.loads(res)
                if isinstance(parsed, dict):
                    return parsed
        except Exception as e:
            self.logger.debug("Failed to read check-in state: %s", e)
        return {}

    async def _wait_for_checkin_state(self, timeout: int = 15) -> dict:
        """Poll the coin page until the check-in widget actually renders.

        The coin page (immersive mode) loads its Collect button and streak
        calendar asynchronously, so a single read right after navigation often
        sees nothing. Poll until we detect either a collect button or the
        already-claimed ('Earn more coins') state, or until timeout.
        """
        elapsed = 0
        interval = 2
        last: dict = {}
        while elapsed < timeout:
            last = await self._read_checkin_state()
            if last.get("btnText") or last.get("claimed"):
                self.logger.debug("Check-in widget rendered after %ds: %s", elapsed, last)
                return last
            self.logger.debug("Check-in widget not rendered yet (%ds/%ds): %s", elapsed, timeout, last)
            await self.sleep(interval)
            elapsed += interval
        return last

    async def _click_collect(self, btn_text: str) -> bool:
        """Click the exact check-in button detected by ``_read_checkin_state``.

        Uses the real button text from the DOM (e.g. 'Collect 70'), so it works
        for labels that carry the coin amount. The previous JS fallback matched
        only the exact string 'collect' and silently skipped 'Collect 70', the
        bug that let a check-in report success while collecting nothing. Native
        (trusted) click first; a JS click on the same exact label as last resort.
        """
        try:
            el = await self.page.find(btn_text, timeout=3)
            if el:
                await self._human_pause(0.7, 1.6)
                await el.click()
                self.logger.debug("Collect clicked natively ('%s')", btn_text)
                return True
        except Exception as e:
            self.logger.debug("Native collect click failed: %s", e)
        try:
            clicked = await self.page.evaluate(
                "(() => { const target = %s;"
                " const els = [...document.querySelectorAll('button, div[role=\"button\"], span, a')];"
                " for (const b of els) { if ((b.textContent||'').trim() === target && b.offsetParent !== null) { b.click(); return true; } }"
                " return false; })()" % json.dumps(btn_text)
            )
            return bool(clicked)
        except Exception as e:
            self.logger.debug("JS collect click failed: %s", e)
            return False

    async def _dump_visible_buttons(self) -> None:
        """Log the visible clickable labels on the page (diagnostic on failure)."""
        try:
            raw = await self.page.evaluate(r"""
                (() => {
                    const out = [];
                    for (const b of document.querySelectorAll('button, div[role="button"], a, span')) {
                        const t = (b.textContent || '').trim();
                        if (t && t.length <= 40 && b.offsetParent !== null) out.push(t);
                    }
                    return JSON.stringify([...new Set(out)].slice(0, 30));
                })()
            """)
            labels = json.loads(raw) if isinstance(raw, str) else []
            self.logger.warning("🔎 No check-in button matched. Visible clickable labels: %s", labels)
        except Exception as e:
            self.logger.debug("Button dump failed: %s", e)

    async def _read_checkin_info(self) -> dict:
        """Read the day-streak count and tomorrow's bonus.

        Returns ``{"streak": int|None, "tomorrow": int|None}``.

        The check-in APIs are the reliable source: on the page both numbers are
        rotating digit animations and localized text, so the DOM is only used to
        fill in whatever the API didn't provide.
        """
        info = self._extract_checkin_info_from_api()
        if info.get("streak") is not None and info.get("tomorrow") is not None:
            return info

        dom = await self._read_checkin_info_dom()
        return {
            "streak": info.get("streak") if info.get("streak") is not None else dom.get("streak"),
            "tomorrow": info.get("tomorrow") if info.get("tomorrow") is not None else dom.get("tomorrow"),
        }

    async def _read_checkin_info_dom(self) -> dict:
        """DOM fallback for the streak / tomorrow numbers.

        The streak number and its "day streak"/"seria" label live in SEPARATE
        DOM elements (the number is often a `<div><span>N</span></div>` next to
        an `<h3>day streak</h3>`), so a plain body-text regex misses it. We first
        locate the label element and pull the nearest standalone integer from its
        container, then fall back to a whole-page regex.
        """
        raw = await self.page.evaluate(r"""
            (() => {
                const isVisible = (el) => !!el && el.offsetParent !== null;
                const labelRe = /^(day streak|days? in a row|seria|seria dni|dni serii|dni z rz[eę]du|streak)$/i;

                let streak = null;
                // Strategy 1: find the "day streak"/"seria" label, then read the
                // nearest standalone integer from up to 2 ancestors.
                const labels = [...document.querySelectorAll('h1,h2,h3,h4,span,div,p')]
                    .filter(el => el.childElementCount === 0 && labelRe.test((el.textContent || '').trim()) && isVisible(el));
                for (const label of labels) {
                    let node = label;
                    for (let up = 0; up < 3 && node.parentElement && streak === null; up++) {
                        node = node.parentElement;
                        // Prefer a dedicated number element, else the container text.
                        const candidates = [...node.querySelectorAll('span,div,b,strong')]
                            .map(e => (e.childElementCount === 0 ? (e.textContent || '').trim() : ''))
                            .filter(t => /^\d{1,3}$/.test(t));
                        if (candidates.length) { streak = parseInt(candidates[0], 10); break; }
                        const m = (node.textContent || '').replace(/(day streak|seria|dni)/ig, ' ').match(/\b(\d{1,3})\b/);
                        if (m) { streak = parseInt(m[1], 10); break; }
                    }
                    if (streak !== null) break;
                }

                // Strategy 2: whole-page regex fallback.
                if (streak === null) {
                    const text = document.body ? (document.body.textContent || '') : '';
                    const m = text.match(/(\d{1,3})\s*(?:day streak|days? in a row|dni z rz[eę]du)/i)
                        || text.match(/(?:seria|streak)[^\d]{0,12}(\d{1,3})/i);
                    if (m) streak = parseInt(m[1], 10);
                }

                // Reject implausible values (rotating-digit animation artefacts).
                if (streak !== null && (streak < 0 || streak > 999)) streak = null;

                const text = document.body ? (document.body.textContent || '') : '';
                // Only phrasings that tie the number to coins: a bare number near the
                // word "tomorrow" is usually the day counter, not the reward.
                const tomMatch = text.match(/Get\s*(\d{1,4})\s*check-in coins tomorrow/i)
                    || text.match(/(\d{1,4})\s*(?:check-in )?coins tomorrow/i)
                    || text.match(/tomorrow[^\d]{0,20}(\d{1,4})\s*(?:coins?|monet)/i)
                    || text.match(/jutro[^\d]{0,20}(\d{1,4})\s*monet/i)
                    || text.match(/(\d{1,4})\s*monet\w*\s*jutro/i);
                const tomorrow = tomMatch ? parseInt(tomMatch[1], 10) : null;

                return JSON.stringify({ streak: streak, tomorrow: tomorrow });
            })()
        """)
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        return {"streak": info.get("streak"), "tomorrow": info.get("tomorrow")}

    def _format_checkin_status(self, claimed_coins, info: dict, total) -> str:
        """Build the notification status string for a successful check-in.

        Example: ``claimed 70 🪙, streak 5 days, tomorrow 72 🪙, balance 1,234 🪙``.
        Every number is labelled and anything unknown is left out (no fake "active").
        """
        parts: list[str] = [f"claimed {claimed_coins} 🪙" if claimed_coins else "claimed today 🪙"]
        streak = (info or {}).get("streak")
        tomorrow = (info or {}).get("tomorrow")
        if streak is not None:
            parts.append(f"streak {streak} day{'s' if streak != 1 else ''}")
        if tomorrow is not None:
            parts.append(f"tomorrow {tomorrow} 🪙")
        if total is not None:
            parts.append(f"balance {total:,} 🪙")
        return ", ".join(parts)

    def _report(self, status: str) -> None:
        """Append the AliExpress check-in result to the notification list."""
        self.notify_games.append({
            "title": "AliExpress Daily Check-in",
            "url": URL_COINS,
            "status": status,
        })

    async def _rewarm_to_coins(self) -> None:
        """Re-approach the coin page organically (home → activity → coins).

        A cold `page.get(URL_COINS)` on every retry is exactly the kind of
        cold-jump-to-sensitive-URL that raises AliExpress' risk score. When we
        retry a suspected bot-flag, we instead re-do the human-like warm-up that
        earns a healthier trust score on the next coin-page load.
        """
        try:
            await self.page.get(URL_MHOME)
            await self._human_pause(3, 6)
            await self._simulate_human_activity()
        except Exception as e:
            self.logger.debug("Re-warm navigation failed: %s", e)
        await self._goto_coins_organically()
        await self._human_pause(5, 9)
        await self._dismiss_overlays()

    async def _dump_failure_state(self) -> None:
        """Persist real page structure when a check-in fails, so a recurring
        failure can be diagnosed from actual DOM instead of guesswork: iframe
        count, body size, coin-related text, plus a full HTML + screenshot dump.
        """
        try:
            raw = await self.page.evaluate(r"""
                (() => {
                    const coinText = (document.body ? (document.body.innerText || '') : '')
                        .split('\n').map(s => s.trim())
                        .filter(s => /coin|check[- ]?in|streak|odbierz|monet|seria/i.test(s))
                        .slice(0, 20);
                    return JSON.stringify({
                        iframes: document.querySelectorAll('iframe').length,
                        bodyLen: document.body ? document.body.innerHTML.length : 0,
                        coinText: coinText,
                        url: location.href
                    });
                })()
            """)
            info = json.loads(raw) if isinstance(raw, str) else {}
            self.logger.warning(
                "🧪 Failure structure: iframes=%s bodyLen=%s url=%s coinText=%s",
                info.get("iframes"), info.get("bodyLen"), info.get("url"), info.get("coinText"))
        except Exception as e:
            self.logger.debug("Failure structure probe failed: %s", e)
        try:
            html = await self.page.evaluate("document.documentElement.outerHTML")
            if isinstance(html, str):
                (cfg._data_dir / "ae_checkin_fail.html").write_text(html, encoding="utf-8")
                self.logger.warning("🧪 Saved failing coin page HTML to data/ae_checkin_fail.html")
        except Exception as e:
            self.logger.debug("HTML dump failed: %s", e)
        try:
            await self.take_screenshot("ae_checkin_fail")
        except Exception:
            pass

    async def _verify_check_in(self) -> None:
        """Verify the coin page, guard the bot-flag state, collect coins, and report honestly.

        Reports a real failure (and offers manual VNC collection) when no
        check-in button can be found or the click can't be confirmed, instead
        of silently logging success, which previously masked missed check-ins
        and cost the user their streak.
        """
        current_url = await self.page.evaluate("window.location.href")
        if "/p/coin-index/" not in str(current_url):
            self.logger.debug("Navigating to coins page to trigger daily check-in...")
            await self.page.get(URL_COINS)
            await self._human_pause(2, 3)
        # adjusted timing here and added the loading check
        await self._handle_skeleton_loading()

        await self._dismiss_overlays()

        # Read the coin/check-in API captured in-page (balance + diagnostic fields).
        await self._read_coin_api()

        if cfg.dryrun:
            self.logger.info("DRYRUN – skipped AliExpress coin check-in.")
            self._report("available (dry run)")
            return

        # Bot-flag guard: never collect under min_coins; re-warm and retry (empty/unrendered page treated the same).
        min_coins = max(1, cfg.ae_min_coins)
        attempts = max(1, cfg.ae_flag_retries + 1)
        state: dict = {}
        collect_ready = False
        for attempt in range(1, attempts + 1):
            state = await self._wait_for_checkin_state(timeout=20)
            coins = state.get("todayCoins")

            if state.get("claimed"):
                self.logger.info(
                    "✨ Daily check-in already claimed today ('%s' detected).",
                    state.get("earnText") or "Earn more coins")
                self._report("already claimed today ✨")
                return

            if state.get("btnText") and (coins is None or coins >= min_coins):
                self.logger.info(
                    "🪙 Today's check-in offers %s coins (>= AE_MIN_COINS=%d), collecting.",
                    coins if coins is not None else "?", min_coins)
                collect_ready = True
                break

            # Not collectable yet: either a 1-coin bot-flag state or an
            # unrendered/empty widget. Both get the same retry treatment.
            reason = (f"only {coins} coin(s) offered (min {min_coins})"
                      if state.get("btnText") else "check-in widget did not render (empty page)")
            if attempt < attempts:
                wait_s = int(cfg.ae_flag_wait * random.uniform(0.9, 1.3))
                self.logger.warning(
                    "🚫 Not collecting: %s. Waiting %ds and re-approaching organically, "
                    "then retrying (%d/%d)...", reason, wait_s, attempt, attempts - 1)
                await self.sleep(wait_s)
                await self._rewarm_to_coins()
            else:
                self.logger.error("🚫 Gave up after %d retries: %s.", attempts - 1, reason)

        # --- Collect -------------------------------------------------------
        if collect_ready:
            # Capture how many coins today's check-in offers BEFORE clicking,
            # the "Collect 70" button disappears once collected. Snapshot the
            # wallet balance too, so we can report the post-collect total.
            claimed_coins = state.get("todayCoins")
            bal_before = self._user_coins

            # A little human activity, then click the exact detected button.
            try:
                await self.page.scroll_down(random.randint(10, 20))
                await self._human_pause(0.6, 1.4)
                await self.page.scroll_up(random.randint(8, 16))
                await self._human_pause(0.5, 1.2)
            except Exception:
                pass

            self.logger.info("🎯 Clicking check-in button '%s'...", state["btnText"])
            if await self._click_collect(state["btnText"]):
                await self._human_pause(2.5, 4.5)
                after = await self._read_checkin_state()
                if after.get("claimed") or not after.get("btnText"):
                    await self._read_coin_api()  # refresh balance after collect
                    info = await self._read_checkin_info()
                    # Prefer the freshest balance from the API; if it didn't
                    # refetch, estimate the new total from the pre-collect
                    # balance plus what we just collected.
                    total = self._user_coins
                    if total is not None and bal_before is not None and total == bal_before and claimed_coins:
                        total = bal_before + claimed_coins
                    status = self._format_checkin_status(claimed_coins, info, total)
                    self.logger.info("✅ AliExpress coins collected! (%s)", status)
                    self._report(status)
                    await self.sleep(3)
                    return
                self.logger.warning("Clicked collect but a collect button is still present, treating as not collected.")

        # --- Not collected: two distinct terminal states -------------------
        await self._dump_failure_state()

        if state.get("btnText"):
            # A collect button existed but the reward was capped at < min_coins:
            # a persistent bot-flag. Skip per user policy (never collect 1 coin).
            coins = state.get("todayCoins")
            self.logger.error(
                "🚫 Session flagged as low-trust: only %s coin(s) offered instead of the "
                "full amount, NOT collecting (policy). Collect on your phone to keep the streak.", coins)
            self._report(f"⚠️ flagged, only {coins} coin(s) offered, not collected 🚫")
            if cfg.notify_claim_fails:
                await self.notify(
                    f"⚠️ **AliExpress check-in flagged**\n\n"
                    f"The bot's session is being risk-scored: only **{coins} coin(s)** were "
                    f"offered instead of the full amount, so it did NOT collect.\n"
                    f"👉 **Collect on your phone / the AliExpress app today** to keep your streak. "
                    f"The bot will try again on the next scheduled run.")
            return

        # The widget never rendered → let the user collect manually via VNC in
        # case the real (collectable) page is there but the bot read it empty.
        await self._dump_visible_buttons()
        self.logger.error("⚠️ AliExpress check-in widget did not render, offering manual VNC collection.")

        async def _collected_manually() -> bool:
            st = await self._read_checkin_state()
            return bool(st.get("claimed"))

        custom_msg = self._vnc_notice(
            "AliExpress: collect coins manually",
            "The coin page rendered empty for the bot. Open the browser and tap Collect if the button is there.",
        )
        if await self._wait_for_vnc_login(_collected_manually, custom_msg=custom_msg):
            info = await self._read_checkin_info()
            status = self._format_checkin_status(state.get("todayCoins"), info, self._user_coins)
            self.logger.info("✅ Collected manually via VNC. (%s)", status)
            self._report(status.replace("claimed", "claimed manually via VNC", 1))
        else:
            self.logger.error("⚠️ Still not collected after VNC wait, streak may break.")
            self._report("⚠️ NOT collected, widget did not render")


async def claim_aliexpress() -> dict:
    """Convenience entry point for AliExpress daily check-in."""
    claimer = AliExpressClaimer()
    await claimer.run()
    return {"store": "AliExpress", "user": claimer.user, "games": claimer.notify_games}
