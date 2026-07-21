"""Base claimer – the foundation that all store modules build on.

This file contains the BaseClaimer class which provides shared functionality
that every store claimer (Steam, Epic, Prime Gaming, GOG) inherits:

  - Browser management: Launches Google Chrome with stealth anti-detection patches
    so websites don't realise they're talking to an automated bot.
  - Session persistence: Each store gets its own browser profile directory, so
    cookies and login sessions survive Docker container restarts.
  - Screenshot capture: Can save screenshots for debugging or notifications.
  - VNC login fallback: If automatic login fails, waits for you to log in
    manually through the VNC web interface.

The stealth JavaScript patches (injected before any page loads) spoof:
  - navigator.webdriver (hides automation flag)
  - WebGL renderer (fakes a real GPU to avoid captcha)
  - Browser plugins, languages, hardware specs (mimics a real desktop PC)
  - Passkeys (prevents passkey dialogs from blocking login forms)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import nodriver as uc

from src.core.config import cfg

logger = logging.getLogger("fgc.claimer")


def now_str() -> str:
    """Return a human-readable UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def filenamify(s: str) -> str:
    """Sanitise a string for use as a filename."""
    import re
    return re.sub(r'[^a-zA-Z0-9 _\-.]', '_', s.replace(":", "."))


class BaseClaimer:
    """Abstract base for all store claimers.

    Subclasses must implement:
        ``store_name``  – class-level string (e.g. ``"epic"``)
        ``run()``       – main claiming coroutine
    """

    store_name: str = "base"

    # Off by default: the hand-rolled desktop _STEALTH_JS mismatches the real container and triggered captchas (see CHANGELOG 1.4).
    inject_base_stealth: bool = False

    @property
    def logger(self):
        return logging.getLogger(f"fgc.{self.store_name}")

    def __init__(self) -> None:
        self.browser: uc.Browser | None = None
        self.page: uc.Tab | None = None
        self.user: str | None = None
        self.notify_games: list[dict] = []

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    # Stealth JS injected BEFORE every page load via CDP
    # (addScriptToEvaluateOnNewDocument ensures it runs before any site JS)
    _STEALTH_JS = """
    // navigator.webdriver: only patch when truly true, and spoof false (real Chrome never reports undefined).
    if (navigator.webdriver === true) {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => false,
        });
    }

    // All navigator spoofs use configurable:true so per-store modules can override them without a TypeError.
    Object.defineProperty(navigator, 'plugins', {
        configurable: true,
        get: () => [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ],
    });

    // --- Languages (sometimes empty in headless) ---
    Object.defineProperty(navigator, 'languages', {
        configurable: true,
        get: () => ['en-US', 'en'],
    });

    // --- Hardware concurrency (must be realistic) ---
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        configurable: true,
        get: () => 4,
    });

    // --- Device memory (must be realistic) ---
    Object.defineProperty(navigator, 'deviceMemory', {
        configurable: true,
        get: () => 8,
    });

    // --- Platform (must match a real desktop) ---
    Object.defineProperty(navigator, 'platform', {
        configurable: true,
        get: () => 'Win32',
    });

    // --- WebGL vendor/renderer spoofing ---
    // Epic's anti-bot checks WebGL capabilities; software renderers trigger captcha.
    const _getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        // UNMASKED_VENDOR_WEBGL
        if (param === 0x9245) return 'Google Inc. (NVIDIA)';
        // UNMASKED_RENDERER_WEBGL
        if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return _getParameter.call(this, param);
    };
    if (typeof WebGL2RenderingContext !== 'undefined') {
        const _getParameter2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {
            if (param === 0x9245) return 'Google Inc. (NVIDIA)';
            if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return _getParameter2.call(this, param);
        };
    }

    // --- Disable passkeys (prevents passkey dialogs blocking login) ---
    if (navigator.credentials) {
        navigator.credentials.create = async () => Promise.reject(
            new Error('Passkeys disabled')
        );
        navigator.credentials.get = async () => Promise.reject(
            new Error('Passkeys disabled')
        );
    }

    // --- Permissions API (hide "denied" automation fingerprint) ---
    const _query = window.Permissions?.prototype?.query;
    if (_query) {
        window.Permissions.prototype.query = function(params) {
            if (params?.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return _query.call(this, params);
        };
    }
    """

    async def start_browser(
        self,
        *,
        force_headful: bool = False,
        extra_args: list[str] | None = None,
    ) -> uc.Browser:
        """Launch a nodriver browser instance with full stealth.

        Args:
            force_headful: If True, always run with a visible window
                           (Epic needs this to avoid captcha).
            extra_args: Additional Chromium flags.
        """
        import shutil

        # Ensure persistent browser profile directory exists (per store)
        store_browser_dir = cfg.browser_dir / self.store_name
        store_browser_dir.mkdir(parents=True, exist_ok=True)

        # Disable Chrome's "Save password?" popup by setting profile preferences
        prefs_dir = store_browser_dir / "Default"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        prefs_file = prefs_dir / "Preferences"
        try:
            import json as _json
            prefs = {}
            if prefs_file.exists():
                prefs = _json.loads(prefs_file.read_text(encoding="utf-8"))
            prefs["credentials_enable_service"] = False
            prefs["credentials_enable_autosignin"] = False
            prefs.setdefault("profile", {})
            prefs["profile"]["password_manager_enabled"] = False
            prefs.setdefault("protocol_handler", {})
            prefs["protocol_handler"]["excluded_schemes"] = {
                "aliexpress": True,
                "aliexpresshd": True,
                "aecmd": True,
                "alibaba": True,
                "intent": True,
                "market": True,
                "android-app": True,
                "alipay": True,
                "alipays": True,
                "tmall": True,
                "taobao": True,
            }
            prefs_file.write_text(_json.dumps(prefs), encoding="utf-8")
        except Exception:
            pass  # Non-critical

        # Remove stale singleton lock files that a crashed instance leaves behind (session data untouched).
        self._clear_profile_locks(store_browser_dir)

        # Auto-detect chrome binary
        chrome_path = (
            shutil.which("google-chrome-stable")
            or shutil.which("google-chrome")
            or shutil.which("chromium-browser")
            or shutil.which("chromium")
        )
        self.logger.debug("Chrome: %s", chrome_path)

        headless = False if force_headful else (not cfg.show)

        # --- Browser args ---
        # IMPORTANT: Do NOT add `--disable-blink-features=AutomationControlled`
        # nodriver already handles this internally, and the flag itself is a
        # well-known signal that sophisticated anti-bot systems detect.
        args = [
            f"--window-size={cfg.width},{cfg.height}",
            "--hide-crash-restore-bubble",
            "--restore-last-session",
            "--lang=en-US",
            "--accept-lang=en-US,en;q=0.9",
            "--disable-dev-shm-usage",     # Docker shared memory fix
            "--disable-smooth-scrolling",  # CPU optimization
            "--disable-extensions",        # CPU optimization
            "--mute-audio",                # CPU optimization
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-features=AudioServiceOutOfProcess,Translate,TranslateUI",
            "--disable-translate",
            "--disable-ipc-flooding-protection",
            "--disable-renderer-backgrounding",
            "--metrics-recording-only",
            "--no-first-run",
            "--password-store=basic",
            "--use-mock-keychain",
        ]
        # Only disable GPU when running headless (non-Epic).
        # For headful mode (Epic), GPU must stay enabled so that WebGL reports
        # hardware-accelerated rendering, which is checked by hCaptcha/anti-bot.
        if not force_headful:
            args.append("--disable-gpu")

        if extra_args:
            args.extend(extra_args)

        # Forcefully disable Google Translate at the Chromium profile level
        try:
            import json
            store_browser_dir.mkdir(parents=True, exist_ok=True)
            default_dir = store_browser_dir / "Default"
            default_dir.mkdir(exist_ok=True)
            prefs_file = default_dir / "Preferences"
            prefs = {}
            if prefs_file.exists():
                prefs = json.loads(prefs_file.read_text("utf-8"))
            if "translate" not in prefs:
                prefs["translate"] = {}
            prefs["translate"]["enabled"] = False
            prefs_file.write_text(json.dumps(prefs), "utf-8")
        except Exception as e:
            self.logger.debug("Failed to seed Chrome preferences: %s", e)

        # Launch with retries; sweep orphaned Chrome + locks between attempts (issue #19).
        launch_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self.browser = await uc.start(
                    headless=headless,
                    sandbox=False,  # required when running as root in Docker
                    browser_executable_path=chrome_path,
                    browser_args=args,
                    user_data_dir=str(store_browser_dir),
                )
                launch_error = None
                break
            except Exception as e:
                launch_error = e
                self.logger.warning("Chrome launch attempt %d/3 failed: %s", attempt, e)
                self._sweep_orphan_chrome(store_browser_dir)
                self._clear_profile_locks(store_browser_dir)
                if attempt < 3:
                    await asyncio.sleep(2 * attempt)
        if launch_error is not None:
            raise RuntimeError(
                f"Chrome failed to start after 3 attempts (a container restart may help): {launch_error}"
            ) from launch_error

        # Get the main tab
        self.page = await self.browser.get("about:blank")

        # --- Inject stealth patches via CDP (runs BEFORE any page JS) ---
        # Unlike page.evaluate(), addScriptToEvaluateOnNewDocument ensures
        # our patches are active when the WAF/anti-bot first evaluates the
        # browser fingerprint on navigation.
        try:
            # Page domain MUST be enabled first, else addScriptToEvaluateOnNewDocument is silently ignored.
            await self.page.send(uc.cdp.page.enable())
            if self.inject_base_stealth:
                await self.page.send(
                    uc.cdp.page.add_script_to_evaluate_on_new_document(
                        source=self._STEALTH_JS,
                    )
                )
                self.logger.debug("Stealth JS injected via CDP.")
            else:
                self.logger.debug(
                    "Base stealth JS skipped (store injects its own fingerprint)."
                )
        except Exception:
            # Fallback: inject directly on current page
            self.logger.debug("CDP injection failed, using evaluate fallback.")
            if self.inject_base_stealth:
                await self.page.evaluate(self._STEALTH_JS)

        self.log_browser_ready()
        return self.browser

    def log_browser_ready(self) -> None:
        """Standardised log for browser ready state."""
        self.logger.info("🌐 [bold yellow]Browser ready[/bold yellow]")

    def log_signed_in(self, username: str | None = None) -> None:
        """Standardised log for successful login."""
        user = username or self.user or "unknown"
        self.user = user
        self.logger.info("🔓 [bold green]Signed in as:[/bold green] %s", user)

    async def close_browser(self) -> None:
        """Close the browser and kill its whole process tree (issue #19)."""
        if not self.browser:
            return
        pid = getattr(self.browser, "_process_pid", None) \
            or getattr(getattr(self.browser, "_process", None), "pid", None)
        try:
            self.browser.stop()
        except Exception as e:
            self.logger.debug("browser.stop() raised (ignored): %s", e)
        # stop() only ends the parent; kill leftover children so they can't pile up.
        self._kill_process_tree(pid)
        self.browser = None
        self.page = None

    def _clear_profile_locks(self, store_browser_dir: Path) -> None:
        """Remove Chrome singleton lock files only (not cookies/session)."""
        # SingletonCookie is Chrome's singleton token, NOT the login cookies.
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = store_browser_dir / lock_file
            try:
                if lock_path.is_symlink() or lock_path.exists():
                    lock_path.unlink()
                    self.logger.debug("Removed stale %s", lock_file)
            except Exception as e:
                self.logger.debug("Failed to remove %s: %s", lock_file, e)

    def _kill_process_tree(self, pid) -> None:
        """Terminate a process and all its children."""
        if not pid:
            return
        try:
            import psutil
            parent = psutil.Process(pid)
        except Exception:
            return
        try:
            procs = parent.children(recursive=True)
            procs.append(parent)
            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
            _, alive = psutil.wait_procs(procs, timeout=5)
            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass
        except Exception as e:
            self.logger.debug("Process-tree cleanup skipped: %s", e)

    def _sweep_orphan_chrome(self, store_browser_dir: Path) -> int:
        """Kill orphaned Chrome tied to this store's profile (matched by --user-data-dir)."""
        try:
            import psutil
        except Exception:
            return 0
        needle = str(store_browser_dir)
        killed = 0
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "chrome" not in name and "chromium" not in name:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if needle in cmdline:
                    self._kill_process_tree(proc.pid)
                    killed += 1
            except Exception:
                continue
        if killed:
            self.logger.warning("Swept %d orphaned Chrome process(es) for this profile.", killed)
        return killed

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    def screenshot_path(self, *parts: str) -> Path:
        """Build a screenshot path inside ``data/screenshots/<store>/``."""
        p = cfg.screenshots_dir / self.store_name
        p.mkdir(parents=True, exist_ok=True)
        return p.joinpath(*parts)

    async def take_screenshot(self, name: str) -> Path | None:
        """Take a screenshot and return its path."""
        if not self.page:
            return None
        p = self.screenshot_path(f"{filenamify(name)}.png")
        try:
            await self.page.save_screenshot(str(p))
            self.logger.debug("Screenshot saved: %s", p)
            return p
        except Exception:
            self.logger.exception("Failed to save screenshot")
            return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    async def wait_for(self, selector: str, timeout: int | None = None) -> uc.Element | None:
        """Wait for an element matching the CSS selector to appear."""
        timeout = timeout or (cfg.timeout // 1000)
        try:
            element = await self.page.find(selector, timeout=timeout)
            return element
        except Exception:
            return None

    def _vnc_notice(self, title: str, body: str, timeout: int | None = None) -> str:
        """Build a consistent 'do X via VNC' notification with the autoconnect link."""
        timeout = timeout or cfg.vnc_login_timeout
        return (
            f"🔐 **{title}**\n\n{body}\n"
            f"🌐 **Open VNC:** {cfg.vnc_url}\n"
            f"⏱️ **Timeout:** waiting {timeout}s"
        )

    @property
    def notify_enabled(self) -> bool:
        """False when this store's notifications are silenced via NOTIFY_SKIP_STORES."""
        return cfg.store_notify_enabled(self.store_name)

    async def notify(self, message: str, **kwargs) -> None:
        """Send a notification unless this store is silenced (NOTIFY_SKIP_STORES)."""
        if not self.notify_enabled:
            self.logger.debug("Notifications silenced for '%s' — skipping.", self.store_name)
            return
        from src.core.notifier import notify as _notify
        await _notify(message, **kwargs)

    async def _wait_for_vnc_login(self, check_fn, *, timeout: int | None = None, interval: int = 5, log_interval: int = 60, custom_msg: str | None = None) -> bool:
        """Wait for manual VNC login.

        Polls every `interval` seconds, but only logs a waiting message every `log_interval` seconds.
        """
        timeout = timeout or cfg.vnc_login_timeout
        from src.core.notifier import notify

        if custom_msg:
            msg = custom_msg
        else:
            msg = self._vnc_notice(
                f"{self.store_name} — manual login needed",
                "Finish signing in in the browser.",
                timeout,
            )
        self.logger.info("Open %s to finish manually (waiting %ds).", cfg.vnc_url, timeout)

        if cfg.notify_login_request and self.notify_enabled:
            await notify(msg)

        elapsed = 0
        last_log = 0
        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            if await check_fn():
                return True
            
            if elapsed - last_log >= log_interval:
                last_log = elapsed
                remaining = timeout - elapsed
                if remaining > 0:
                    self.logger.info("Still waiting for login… %ds left.", remaining)
        return False

    async def _human_challenge_present(self) -> bool:
        """Return True when the current page is a Cloudflare / captcha human-check.

        Covers the Cloudflare "Just a moment…" / Turnstile interstitial that can
        gate a whole domain before login (e.g. store.epicgames.com, steamdb.info)
        as well as hCaptcha / Arkose (FunCaptcha) login challenges. None of these
        can be solved automatically, so detecting one lets a store notify the
        user for a one-time manual solve via VNC. Matches only known providers /
        markers to avoid false positives on normal pages.
        """
        if not self.page:
            return False
        try:
            return bool(await self.page.evaluate(r"""
                (() => {
                    const t = (document.title || '').toLowerCase();
                    if (t.includes('just a moment') || t.includes('attention required') || t.includes('one more step')) return true;
                    if (document.querySelector('#challenge-form, #challenge-running, #cf-challenge-running, .cf-turnstile, iframe[src*="challenges.cloudflare.com"]')) return true;
                    const rx = /hcaptcha|arkoselabs|funcaptcha|arkose|px-captcha|geetest|turnstile/i;
                    const frames = [...document.querySelectorAll('iframe')];
                    if (frames.some(f => rx.test((f.getAttribute('src') || '') + ' ' + (f.getAttribute('title') || '')))) return true;
                    if (document.querySelector('#h_captcha, #talon_frame_login_prod, #FunCaptcha, [id*="arkose" i]')) return true;
                    const b = (document.body ? (document.body.innerText || '') : '').toLowerCase();
                    if (b.includes('verify you are human') || b.includes('checking your browser') || b.includes('complete a security check') || b.includes('needs to review the security of your connection')) return true;
                    return false;
                })()
            """))
        except Exception:
            return False

    async def _wait_out_challenge(self, label: str, settle: int = 12) -> bool:
        """Clear a human-check: let it auto-pass, else alert the user to solve via VNC.

        First waits up to ``settle`` seconds for a managed/invisible challenge to
        clear on its own (so we don't ping the user needlessly). If it's still
        blocking, sends a single VNC alert and polls until it's gone or the login
        timeout hits. Returns True if the challenge cleared, False on timeout.
        """
        waited = 0
        while waited < settle:
            if not await self._human_challenge_present():
                return True
            await asyncio.sleep(2)
            waited += 2

        self.logger.warning("%s is behind a Cloudflare / captcha human-check – requesting manual solve via VNC.", label)
        custom_msg = self._vnc_notice(
            f"{label} — security check",
            "A Cloudflare / captcha human-check is blocking the bot. Open the browser and complete it to continue.",
        )

        async def _cleared() -> bool:
            return not await self._human_challenge_present()

        return await self._wait_for_vnc_login(_cleared, custom_msg=custom_msg)

    async def sleep(self, seconds: float) -> None:
        """Async sleep wrapper."""
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------
    # Entry point (to be overridden)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Override in subclasses to implement the claiming logic."""
        raise NotImplementedError
