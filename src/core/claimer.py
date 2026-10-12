"""Base claimer – the foundation that all store modules build on.

This file contains the BaseClaimer class which provides shared functionality
that every store claimer (Steam, Epic, Prime Gaming, GOG) inherits:

  - Browser management: Launches Google Chrome through nodriver, which hides the
    automation flags itself.
  - Session persistence: Each store gets its own browser profile directory, so
    cookies and login sessions survive Docker container restarts.
  - Screenshot capture: Can save screenshots for debugging or notifications.
  - VNC login fallback: If automatic login fails, waits for you to log in
    manually through the VNC web interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

import nodriver as uc
import pyotp

from src.core.browser_timezone import chrome_timezone
from src.core.config import cfg
from src.core.display import (
    free_memory,
    restart_screen,
    running_as,
    screen_is_alive,
    screen_is_managed,
    screen_state,
)
from src.core.run_state import mark_answered, mark_unanswered, waits_for_nobody

logger = logging.getLogger("fgc.claimer")


def now_str() -> str:
    """Return a human-readable UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def mask_account(name) -> str:
    """Account label safe for a log someone pastes into a bug report: a.n.other@mail.com -> a***@mail.com."""
    text = str(name or "")
    local, at, domain = text.partition("@")
    if not at:
        return text          # a nickname is not personal data, leave it readable
    return f"{local[:1]}***@{domain}"


def mask_code(code) -> str:
    """A game code cut for the log, like Steam keys: the whole code is only in your notification."""
    text = str(code or "").strip()
    return f"{text[:5]}…" if text else ""


def chrome_binary() -> str | None:
    """The Chrome or Chromium the bot starts, None when neither is installed."""
    import shutil

    return (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
            or shutil.which("chromium-browser") or shutil.which("chromium"))


def chrome_version(path: str | None = None) -> str:
    """The full version of that browser, e.g. "155.0.8059.39", empty when it cannot tell."""
    import subprocess

    path = path or chrome_binary()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""
    match = re.search(r"\d+\.\d+\.\d+\.\d+", out or "")
    return match.group(0) if match else ""


async def open_first_tab(browser, url: str = "about:blank", attempts: int = 10, delay: float = 1.0):
    """Hand back a usable tab, even when Chrome has not registered one yet (issue #38).

    nodriver reads the target list once at startup, so a slow first tab crashes its get().
    """
    for _ in range(attempts):
        if browser.tabs:
            try:
                return await browser.get(url)
            except RuntimeError as exc:
                # StopIteration inside a coroutine: the tab list changed between the check and the call.
                logger.debug("First tab went away before it could be used: %s", exc)
        if delay:
            await asyncio.sleep(delay)
        await browser.update_targets()
    logger.debug("Chrome still has no page target, asking for a fresh tab.")
    try:
        return await browser.get(url, new_tab=True)
    except Exception as exc:
        # "no browser is open": there is no window to put a tab in, so open one.
        logger.debug("New tab refused (%s), opening a window instead.", exc)
        return await browser.get(url, new_window=True)


WHEEL_NOTCH = 120

# A DLC whose base game you lack is told once, then again only if it is still free this many days later (#78).
MISSING_BASE_REPEAT_DAYS = 21


def missing_base_due(previous_status: str | None, reported_at: str | None, now: datetime | None = None,
                     days: int = MISSING_BASE_REPEAT_DAYS) -> bool:
    """True when a DLC without its base game is worth telling you about: new, newly in this state, or told weeks ago."""
    if previous_status != "failed:missing_base" or not reported_at:
        return True
    try:
        told = datetime.fromisoformat(str(reported_at))
    except ValueError:
        return True
    if told.tzinfo is None:
        told = told.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - told >= timedelta(days=days)


def missing_base_status(due: bool) -> str:
    """The summary status: shown when due, else "skipped:missing_base", which the summary always leaves out."""
    return "failed:missing_base" if due else "skipped:missing_base"


def track_missing_base(row, now: datetime | None = None, remember: bool = True) -> str:
    """Mark a database row as a DLC without its base game and return its summary status, keeping when it was told."""
    try:
        extra = json.loads(row.extra or "{}")
    except (TypeError, ValueError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    due = missing_base_due(row.status, extra.get("missing_base_reported"), now)
    # A dry run shows it without using up the one report the real run will make.
    if due and remember:
        extra["missing_base_reported"] = (now or datetime.now(timezone.utc)).isoformat()
        row.extra = json.dumps(extra)
    row.status = "failed:missing_base"
    return missing_base_status(due)


def mouse_path(start: tuple, end: tuple, rng=random) -> list:
    """The points a hand passes from start to end: a slight curve, slow at both ends, ending exactly on end."""
    (sx, sy), (tx, ty) = start, end
    steps = max(8, min(40, int(math.hypot(tx - sx, ty - sy) / 18)))
    # One control point beside the straight line bends the path a little, a different way every time.
    bend = rng.uniform(-0.25, 0.25)
    cx = (sx + tx) / 2 - (ty - sy) * bend
    cy = (sy + ty) / 2 + (tx - sx) * bend
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        points.append(((1 - e) ** 2 * sx + 2 * (1 - e) * e * cx + e * e * tx,
                       (1 - e) ** 2 * sy + 2 * (1 - e) * e * cy + e * e * ty))
    return points


def filenamify(s: str) -> str:
    """Sanitise a string for use as a filename."""
    return re.sub(r'[^a-zA-Z0-9 _\-.]', '_', s.replace(":", "."))


# Two goes with the authenticator secret, then a recovery code if there is one, then you over VNC.
OTP_KEY_ATTEMPTS = 2

# A human check in whatever document it runs in: the page itself, or a checkout frame inside it.
CHALLENGE_JS = r"""
(() => {
    const t = (document.title || '').toLowerCase();
    if (t.includes('just a moment') || t.includes('attention required') || t.includes('one more step')) return true;
    if (document.querySelector('#challenge-form, #challenge-running, #cf-challenge-running')) return true;
    // Only a widget you could actually click counts. Epic keeps a full size hCaptcha
    // frame on every sign-in page, hidden by style, until it is really needed.
    const seen = el => {
        const r = el.getBoundingClientRect();
        if (r.width < 60 || r.height < 40) return false;
        for (let n = el; n; n = n.parentElement) {
            const st = getComputedStyle(n);
            if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.1) return false;
        }
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return false;
        const at = document.elementFromPoint(cx, cy);
        return !!at && (at === el || el.contains(at) || at.contains(el));
    };
    // reCAPTCHA's picture challenge is the bframe; an invisible reCAPTCHA's anchor is only its corner badge.
    const rx = /hcaptcha|arkoselabs|funcaptcha|arkose|px-captcha|geetest|turnstile|recaptcha\/(api2|enterprise)\/(anchor|bframe)|datadome|captcha-delivery/i;
    const badge = f => /recaptcha\/(api2|enterprise)\/anchor/i.test(f.getAttribute('src') || '') && /[?&]size=invisible/i.test(f.getAttribute('src') || '');
    const frames = [...document.querySelectorAll('iframe')];
    if (frames.some(f => rx.test((f.getAttribute('src') || '') + ' ' + (f.getAttribute('title') || '')) && !badge(f) && seen(f))) return true;
    const widgets = [...document.querySelectorAll('.cf-turnstile, #h_captcha, #talon_frame_login_prod, #FunCaptcha, [id*="arkose" i], #datadome')];
    if (widgets.some(seen)) return true;
    const b = (document.body ? (document.body.innerText || '') : '').toLowerCase();
    if (b.includes('verify you are human') || b.includes('checking your browser') || b.includes('complete a security check') || b.includes('needs to review the security of your connection')) return true;
    if (b.includes("check that you're a real person") || b.includes('check that you are a real person') || b.includes('geo.captcha-delivery.com')) return true;
    return false;
})()
"""


class BaseClaimer:
    """Abstract base for all store claimers.

    Subclasses must implement:
        ``store_name``  – class-level string (e.g. ``"epic"``)
        ``run()``       – main claiming coroutine
    """

    store_name: str = "base"

    # Profile to reuse when a store rides another's session (Fab on Epic); empty means own profile.
    profile_name: str = ""

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
        # Ensure persistent browser profile directory exists (per store)
        store_browser_dir = cfg.browser_dir / (self.profile_name or self.store_name)
        store_browser_dir.mkdir(parents=True, exist_ok=True)

        # Seed the profile in one write: no password prompts, no Translate, no app-scheme dialogs.
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
            # Chrome is killed by its process tree, so it never writes these itself and the next
            # start goes through session recovery, which is what delays the first tab (issue #38).
            prefs["profile"]["exit_type"] = "Normal"
            prefs["profile"]["exited_cleanly"] = True
            prefs.setdefault("translate", {})
            prefs["translate"]["enabled"] = False
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
        except Exception as e:
            self.logger.debug("Failed to seed Chrome preferences: %s", e)

        # Close a browser left over from an earlier run first, or it and the new one write the same
        # profile at once (issue #38). Locks come off after that, when nothing holds them any more.
        self._sweep_orphan_chrome(store_browser_dir)
        # Remove stale singleton lock files that a crashed instance leaves behind (session data untouched).
        self._clear_profile_locks(store_browser_dir)

        chrome_path = chrome_binary()
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
            "--accept-lang=en-US,en",  # no q-values: Chrome copies this into navigator.languages
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

        # A window needs the virtual screen, and once that died every attempt below fails the same
        # way while noVNC goes quiet too (issue #52), so deal with it here instead of retrying into it.
        if not headless and screen_is_managed() and not screen_is_alive():
            if not restart_screen():
                raise RuntimeError(
                    f"The virtual screen is gone and would not start again ({screen_state()}). "
                    "TurboVNC.log in your data folder says why, and a container restart is the usual cure."
                )

        # Launch with retries; sweep orphaned Chrome + locks between attempts (issue #19).
        launch_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with chrome_timezone():
                    self.browser = await uc.start(
                        headless=headless,
                        sandbox=False,  # required when running as root in Docker
                        browser_executable_path=chrome_path,
                        browser_args=args,
                        user_data_dir=str(store_browser_dir),
                    )
                self.logger.debug("Chrome started (headless=%s, profile=%s, extra args=%s)",
                                  headless, store_browser_dir, extra_args or [])
                # A missing first tab is a launch failure too, so it gets the same sweep and retry.
                self.page = await open_first_tab(self.browser)
                launch_error = None
                break
            except Exception as e:
                launch_error = e
                self.logger.warning("Chrome launch attempt %d/3 failed: %s", attempt, e)
                if attempt == 1:
                    # Cheap facts only, so a run that recovers on a later attempt still leaves evidence.
                    self.logger.debug("State at the first failure: screen %s | user %s | memory %s",
                                      screen_state(), running_as(), free_memory())
                await self.close_browser()
                self._sweep_orphan_chrome(store_browser_dir)
                self._clear_profile_locks(store_browser_dir)
                if attempt < 3:
                    await asyncio.sleep(2 * attempt)
        if launch_error is not None:
            self._log_launch_diagnostics(store_browser_dir, chrome_path)
            raise RuntimeError(
                f"Chrome failed to start after 3 attempts (a container restart may help): {launch_error}"
            ) from launch_error

        # Store scripts added later (AliExpress) need the Page domain on, or they are silently ignored.
        try:
            await self.page.send(uc.cdp.page.enable())
        except Exception as e:
            self.logger.debug("Page.enable failed: %s", e)

        self.log_browser_ready()
        return self.browser

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Strip non-alphanumeric chars and lowercase for fuzzy matching."""
        return re.sub(r'[^a-z0-9]', '', str(title).lower())

    def log_browser_ready(self) -> None:
        """Standardised log for browser ready state."""
        self.logger.info("🌐 [bold yellow]Browser ready[/bold yellow]")

    def log_signed_in(self, username: str | None = None) -> None:
        """Standardised log for successful login. The e-mail is masked, self.user keeps it whole."""
        user = username or self.user or "unknown"
        self.user = user
        self.logger.info("🔓 [bold green]Signed in as:[/bold green] %s", mask_account(user))

    async def close_browser(self) -> None:
        """Close the browser and kill its whole process tree (issue #19)."""
        if not self.browser:
            return
        # A clean close lets Chrome write its cookies to disk before the process tree is killed.
        if self.page:
            try:
                await self.page.send(uc.cdp.browser.close())
                await asyncio.sleep(1)
            except Exception as e:
                self.logger.debug("Clean browser close failed, killing it instead: %s", e)
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

    def _log_launch_diagnostics(self, profile_dir: Path, chrome_path: str | None) -> None:
        """Say what the machine looked like when Chrome refused to start, so a bug report can be answered."""
        import os
        import shutil
        import subprocess

        try:
            usage = shutil.disk_usage(profile_dir if profile_dir.exists() else profile_dir.parent)
            free = f"{usage.free / 1_000_000_000:.1f} GB free"
        except Exception as exc:
            free = f"unknown ({exc})"

        version = "not checked, nodriver picked the binary"
        if chrome_path:
            try:
                done = subprocess.run([chrome_path, "--version"], capture_output=True, text=True, timeout=15)
                version = (done.stdout or done.stderr or "no output").strip()
            except Exception as exc:
                version = f"could not run it ({exc})"

        # nodriver swallows Chrome's own output, and that is where the real reason lives.
        startup = "not attempted, nodriver picked the binary"
        if chrome_path:
            try:
                done = subprocess.run(
                    [chrome_path, "--headless=new", "--no-sandbox", "--disable-gpu",
                     "--disable-dev-shm-usage", f"--user-data-dir={profile_dir}",
                     "--dump-dom", "about:blank"],
                    capture_output=True, text=True, timeout=30,
                )
                said = [line for line in (done.stderr or "").strip().splitlines() if line.strip()]
                # A healthy Chrome still writes noise to stderr, so only quote it when it actually died.
                startup = ("exit=0, so Chrome itself runs fine here" if done.returncode == 0
                           else f"exit={done.returncode}, " + (" / ".join(said[-3:]) if said else "said nothing"))
            except subprocess.TimeoutExpired:
                startup = "hung for 30s instead of starting"
            except Exception as exc:
                startup = f"could not be started at all ({exc})"

        self.logger.warning(
            "Chrome would not start. binary=%s (%s) | profile=%s exists=%s writable=%s | disk %s | "
            "memory %s | user %s | screen %s | started by hand: %s",
            chrome_path or "auto", version, profile_dir, profile_dir.exists(),
            profile_dir.exists() and os.access(profile_dir, os.W_OK), free,
            free_memory(), running_as(), screen_state(), startup,
        )

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
    # Cross-origin frames (a page cannot read into one, CDP can)
    # ------------------------------------------------------------------

    def _walk_nodes(self, node):
        """Every node of a pierced DOM tree, iframe documents included."""
        yield node
        for child in (node.children or []):
            yield from self._walk_nodes(child)
        content = getattr(node, "content_document", None)
        if content is not None:
            yield from self._walk_nodes(content)

    @staticmethod
    def _node_attrs(node) -> dict:
        """An element's attributes as a dict, CDP hands them over as a flat list."""
        raw = node.attributes or []
        return {raw[i]: raw[i + 1] for i in range(0, len(raw) - 1, 2)}

    async def _pierced_document(self):
        """The whole page as one tree, the documents of other-origin frames included."""
        try:
            return await self.page.send(uc.cdp.dom.get_document(depth=-1, pierce=True))
        except Exception as exc:
            self.logger.debug("Could not read the pierced DOM: %s", exc)
            return None

    async def _frame_document(self, matches):
        """The document of the first iframe whose attributes `matches` accepts."""
        doc = await self._pierced_document()
        if doc is None:
            return None
        for node in self._walk_nodes(doc):
            if node.node_name == "IFRAME" and matches(self._node_attrs(node)):
                return getattr(node, "content_document", None)
        return None

    async def _frame_eval(self, document, function_declaration: str):
        """Run JS inside a frame document and parse what it hands back."""
        try:
            handle = await self.page.send(uc.cdp.dom.resolve_node(node_id=document.node_id))
            result = await self.page.send(uc.cdp.runtime.call_function_on(
                function_declaration=function_declaration,
                object_id=handle.object_id,
                return_by_value=True,
            ))
            if isinstance(result, tuple):
                result = result[0]
            raw = getattr(result, "value", None)
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            self.logger.debug("Could not evaluate inside the frame: %s", exc)
            return None

    async def _target_by_url(self, needle: str, wait: int = 0):
        """A window Chrome runs in its own process, found by part of its address."""
        for attempt in range(max(1, wait // 4)):
            try:
                await self.browser.update_targets()
                found = [t for t in self.browser.targets
                         if needle.lower() in str(getattr(getattr(t, "target", None), "url", "") or "").lower()]
                if found:
                    return found[-1]
            except Exception as exc:
                self.logger.debug("Could not list browser windows: %s", exc)
            if attempt + 1 < max(1, wait // 4):
                await self.sleep(4)
        return None

    async def _type_into_node(self, node_id, text: str) -> bool:
        """Focus a field, inside a frame as well, and type into it the way a keyboard would."""
        try:
            await self.page.send(uc.cdp.dom.focus(node_id=node_id))
            await self.page.send(uc.cdp.input_.insert_text(text=text))
            return True
        except Exception as exc:
            self.logger.debug("Could not type into the field: %s", exc)
            return False

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
            self.logger.debug("Notifications silenced for '%s', skipping.", self.store_name)
            return
        from src.core.notifier import notify as _notify
        await _notify(message, **kwargs)

    # The last code sent from an authenticator secret, so a retry never repeats it.
    _last_totp: str = ""

    # Where the cursor was left, so the next move starts there the way a real pointer does.
    _mouse_xy: tuple = ()

    async def _move_mouse(self, tx: int, ty: int) -> None:
        """Move the cursor to (tx, ty) along a curve from where it was, in whole pixels like a real mouse."""
        if self._mouse_xy:
            sx, sy = self._mouse_xy
        else:
            try:
                vw, vh = json.loads(await self.page.evaluate("JSON.stringify([innerWidth, innerHeight])"))
            except Exception:
                vw, vh = 1280, 720
            sx, sy = random.uniform(0.2, 0.8) * vw, random.uniform(0.2, 0.8) * vh
        last = None
        for px, py in mouse_path((sx, sy), (tx, ty)):
            point = (round(px), round(py))
            if point == last:
                continue
            last = point
            await self.page.send(uc.cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=point[0], y=point[1]))
            await asyncio.sleep(random.uniform(0.006, 0.02))
        self._mouse_xy = (tx, ty)

    async def _wheel_scroll(self, x: float, y: float, dy: float) -> None:
        """Scroll by about dy pixels with the mouse wheel over (x, y), notch by notch at a hand's uneven pace."""
        x, y = round(x), round(y)
        await self._move_mouse(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        # A real wheel under TurboVNC gives exactly 120 px a notch (measured with xdotool on 2026-10-11).
        notch = int(math.copysign(WHEEL_NOTCH, dy))
        for _ in range(max(1, round(abs(dy) / WHEEL_NOTCH))):
            await self.page.send(uc.cdp.input_.dispatch_mouse_event(
                type_="mouseWheel", x=x, y=y, delta_x=0, delta_y=notch))
            await asyncio.sleep(random.uniform(0.06, 0.18))
        # Smooth scrolling keeps moving for a moment after the last notch.
        await asyncio.sleep(random.uniform(0.3, 0.5))

    async def _human_click(self, x: float, y: float, w: float = 0, h: float = 0) -> None:
        """Click a box centred on (x, y) like a hand: a curved move to a random spot of it, then an uneven press."""
        # A real mouse reports whole pixels and a pressure of 0.5 while pressed; CDP sends fractions and 0 otherwise.
        tx = round(x + random.uniform(-0.3, 0.3) * w)
        ty = round(y + random.uniform(-0.3, 0.3) * h)
        await self._move_mouse(tx, ty)
        await asyncio.sleep(random.uniform(0.08, 0.25))
        left = uc.cdp.input_.MouseButton("left")
        await self.page.send(uc.cdp.input_.dispatch_mouse_event(
            type_="mousePressed", x=tx, y=ty, button=left, click_count=1, force=0.5))
        await asyncio.sleep(random.uniform(0.05, 0.14))
        await self.page.send(uc.cdp.input_.dispatch_mouse_event(
            type_="mouseReleased", x=tx, y=ty, button=left, click_count=1))
        self._mouse_xy = (tx, ty)

    async def _fresh_totp(self, secret: str, previous: str = "") -> str:
        """A code from the authenticator secret, never the same one twice in a row.

        Codes come from 30-second windows, so a refused code stays refused until the window
        turns over. Waiting for a new one is what makes a second attempt worth making.
        """
        # Sites show the key in groups of four, and pyotp refuses the spaces (TODO 44).
        totp = pyotp.TOTP("".join(str(secret or "").split()).upper())
        code = totp.now()
        waited = 0
        while previous and code == previous and waited < 35:
            await self.sleep(3)
            waited += 3
            code = totp.now()
        if previous and code == previous:
            self.logger.debug("The authenticator code did not change in %ds, sending it anyway.", waited)
        return code

    def _next_unused_code(self, codes: list, used_name: str) -> str | None:
        """First recovery code that has not been spent yet, None once they are gone."""
        used_file = cfg._data_dir / used_name
        used = used_file.read_text("utf-8").splitlines() if used_file.exists() else []
        return next((c for c in codes if c not in used), None)

    def _mark_code_used(self, code: str, used_name: str, codes: list) -> None:
        """Written even when the login still fails: the site has seen the code either way."""
        used_file = cfg._data_dir / used_name
        with used_file.open("a", encoding="utf-8") as fh:
            print(code, file=fh)
        spent = len(used_file.read_text("utf-8").splitlines())
        self.logger.info("Used one recovery code, %d left.", max(0, len(codes) - spent))

    async def _remember_this_browser(self) -> bool:
        """Tick the store's "do not ask for a code here again" box, when it offers one."""
        try:
            clicked = await self.page.evaluate(r"""
                (() => {
                    const boxes = [...document.querySelectorAll('input[type="checkbox"]')];
                    const box = boxes.find(b => {
                        const named = ((b.id || '') + ' ' + (b.name || '')).toLowerCase();
                        const label = (b.closest('label')?.textContent
                            || document.querySelector('label[for="' + b.id + '"]')?.textContent
                            || b.parentElement?.textContent || '').toLowerCase();
                        return /remember|trust/.test(named)
                            || /require code|remember this|trust this/.test(label);
                    });
                    if (!box || box.checked) return false;
                    box.click();
                    return true;
                })()
            """)
        except Exception as exc:
            self.logger.debug("Could not tick the remember-this-browser box: %s", exc)
            return False
        if clicked:
            self.logger.debug("Asked the store not to require a code on this browser again.")
        return bool(clicked)

    async def _wait_for_vnc_login(self, check_fn, *, timeout: int | None = None, interval: int = 5, log_interval: int = 60, custom_msg: str | None = None, store_key: str | None = None) -> bool:
        """Wait for manual VNC login.

        Polls every `interval` seconds, but only logs a waiting message every `log_interval` seconds.
        A store whose last prompt went unanswered is not asked again in this run.
        """
        timeout = timeout or cfg.vnc_login_timeout
        key = store_key or self.store_name
        from src.core.notifier import notify

        if waits_for_nobody(key):
            self.logger.info("Nobody answered the last prompt, so this step is skipped.")
            return False

        if custom_msg:
            msg = custom_msg
        else:
            msg = self._vnc_notice(
                f"{self.store_name}: manual login needed",
                "Finish signing in in the browser.",
                timeout,
            )
        self.logger.info("Open %s to finish manually (waiting %ds).", cfg.vnc_url, timeout)

        # GamerPower's sites are silenced by their own name (store_key), not by "gamerpower".
        if cfg.notify_login_request and self.notify_enabled and cfg.store_notify_enabled(key):
            await notify(msg)

        elapsed = 0
        last_log = 0
        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            if await check_fn():
                # You acted, so the next screen in this store gets the full wait again.
                mark_answered(key)
                return True
            
            if elapsed - last_log >= log_interval:
                last_log = elapsed
                remaining = timeout - elapsed
                if remaining > 0:
                    self.logger.info("Still waiting for login… %ds left.", remaining)
        mark_unanswered(key)
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
            return bool(await self.page.evaluate(CHALLENGE_JS))
        except Exception:
            return False

    async def _page_brief(self) -> str:
        """The page's address and title, for a debug line about a page the bot could not read."""
        try:
            # No query string: a redirect can carry an e-mail address there.
            return str(await self.page.evaluate("location.origin + location.pathname + ' (' + document.title + ')'"))[:160]
        except Exception as e:
            return f"unreadable page ({e})"

    async def _wait_out_challenge(self, label: str, settle: int = 12, store_key: str | None = None,
                                  present_fn=None) -> bool:
        """Clear a human-check: let it auto-pass, else alert the user to solve via VNC.

        First waits up to ``settle`` seconds for a managed/invisible challenge to
        clear on its own (so we don't ping the user needlessly). If it's still
        blocking, sends a single VNC alert and polls until it's gone or the login
        timeout hits. Returns True if the challenge cleared, False on timeout.
        ``present_fn`` looks somewhere other than the page itself, e.g. inside a checkout frame.
        """
        present = present_fn or self._human_challenge_present
        waited = 0
        while waited < settle:
            if not await present():
                return True
            await asyncio.sleep(2)
            waited += 2

        self.logger.warning("%s is behind a Cloudflare / captcha human-check – requesting manual solve via VNC.", label)
        custom_msg = self._vnc_notice(
            f"{label}: security check",
            "A Cloudflare / captcha human-check is blocking the bot. Open the browser and complete it to continue.",
        )

        async def _cleared() -> bool:
            return not await present()

        return await self._wait_for_vnc_login(_cleared, custom_msg=custom_msg, store_key=store_key)

    async def sleep(self, seconds: float) -> None:
        """Async sleep wrapper."""
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------
    # Entry point (to be overridden)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Override in subclasses to implement the claiming logic."""
        raise NotImplementedError
