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
    // --- navigator.webdriver ---
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
    });

    // --- Realistic plugins ---
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ],
    });

    // --- Languages (sometimes empty in headless) ---
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // --- Hardware concurrency (must be realistic) ---
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => 4,
    });

    // --- Device memory (must be realistic) ---
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8,
    });

    // --- Platform (must match a real desktop) ---
    Object.defineProperty(navigator, 'platform', {
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
            prefs_file.write_text(_json.dumps(prefs), encoding="utf-8")
        except Exception:
            pass  # Non-critical

        # Cleanup Chrome lock files to prevent crashes when container hostname changes
        # Note: We must use unlink(missing_ok=True) because these are often broken
        # symlinks pointing to non-existent PIDs, and .exists() evaluates to False for them!
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = store_browser_dir / lock_file
            try:
                if lock_path.is_symlink() or lock_path.exists():
                    lock_path.unlink()
                    self.logger.debug("Removed old %s", lock_file)
            except Exception as e:
                self.logger.warning("Failed to remove %s: %s", lock_file, e)

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
            "--disable-dev-shm-usage",     # Docker shared memory fix
            "--disable-smooth-scrolling",  # CPU optimization
            "--disable-extensions",        # CPU optimization
            "--mute-audio",                # CPU optimization
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-features=AudioServiceOutOfProcess,Translate",
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

        self.browser = await uc.start(
            headless=headless,
            sandbox=False,  # required when running as root in Docker
            browser_executable_path=chrome_path,
            browser_args=args,
            user_data_dir=str(store_browser_dir),
        )

        # Get the main tab
        self.page = await self.browser.get("about:blank")

        # --- Inject stealth patches via CDP (runs BEFORE any page JS) ---
        # Unlike page.evaluate(), addScriptToEvaluateOnNewDocument ensures
        # our patches are active when the WAF/anti-bot first evaluates the
        # browser fingerprint on navigation.
        try:
            await self.page.send(
                uc.cdp.page.add_script_to_evaluate_on_new_document(
                    source=self._STEALTH_JS,
                )
            )
            self.logger.debug("Stealth JS injected via CDP.")
        except Exception:
            # Fallback: inject directly on current page
            self.logger.debug("CDP injection failed, using evaluate fallback.")
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
        """Gracefully close the browser."""
        if self.browser:
            self.browser.stop()

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

    async def _wait_for_vnc_login(self, check_fn, *, timeout: int | None = None, interval: int = 5, log_interval: int = 60) -> bool:
        """Wait for manual VNC login.
        
        Polls every `interval` seconds, but only logs a waiting message every `log_interval` seconds.
        """
        timeout = timeout or cfg.vnc_login_timeout
        from src.core.notifier import notify
        if cfg.novnc_port:
            self.logger.info("Open http://localhost:%s to login manually (waiting %ds).", cfg.novnc_port, timeout)
            msg = f"**{self.store_name}** requires manual login! Open http://localhost:{cfg.novnc_port} to login via VNC (waiting {timeout}s)."
        else:
            self.logger.info("Please login via VNC (waiting %ds).", timeout)
            msg = f"**{self.store_name}** requires manual login via VNC (waiting {timeout}s)."

        if cfg.notify_login_request:
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

    async def sleep(self, seconds: float) -> None:
        """Async sleep wrapper."""
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------
    # Entry point (to be overridden)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Override in subclasses to implement the claiming logic."""
        raise NotImplementedError
