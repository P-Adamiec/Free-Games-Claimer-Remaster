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
import base64
import json
import logging
import math
import random
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
import nodriver as uc
import pyotp

from src.core.browser_timezone import chrome_timezone
from src.core.human_checks import record as record_check
from src.core.config import cfg
from src.core.display import (
    free_memory,
    restart_screen,
    running_as,
    screen_is_alive,
    screen_is_managed,
    screen_state,
)
from src.core.redact import hide_account, mask_account
from src.core.run_state import (CAPTCHA_UNSOLVED, lost_before_next_run, mark_answered,
                                mark_unanswered, needs_you, waits_for_nobody)

logger = logging.getLogger("fgc.claimer")


def now_str() -> str:
    """Return a human-readable UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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


async def page_brief(page) -> str:
    """A page's address and title, empty when there is no page or it cannot be read."""
    try:
        # No query string: a redirect can carry an e-mail address there.
        return str(await page.evaluate("location.origin + location.pathname + ' (' + document.title + ')'"))[:160]
    except Exception:
        return ""


# nodriver gives a new Chrome about 2.75 s to answer; one on a slow disk can need 15 (PR #99, #38).
CHROME_START_WAIT = 60


def abandoned_start(profile_dir: Path):
    """The browser nodriver gave up on for this profile while its Chrome still runs, or None."""
    for browser in list(uc.util.get_registered_instances()):
        process = getattr(browser, "_process", None)
        config = getattr(browser, "config", None)
        # info stays None until Chrome answered once, so a browser that did answer is never taken over.
        if (process is not None and process.returncode is None and getattr(browser, "info", None) is None
                and str(getattr(config, "user_data_dir", "")) == str(profile_dir)
                and getattr(config, "host", None) and getattr(config, "port", None)):
            return browser
    return None


async def wait_for_chrome(process, host: str, port: int, timeout: float) -> float:
    """Wait for a started Chrome to answer on its control port and return the seconds it took."""
    started = time.monotonic()
    deadline = started + timeout
    # A loopback request, never one for HTTP_PROXY.
    async with httpx.AsyncClient(trust_env=False) as client:
        while (left := deadline - time.monotonic()) > 0:
            if process.returncode is not None:
                raise RuntimeError(f"Chrome exited before it answered (exit {process.returncode}).")
            try:
                response = await client.get(f"http://{host}:{port}/json/version", timeout=min(1.0, left))
                if response.status_code == 200 and response.json().get("webSocketDebuggerUrl"):
                    return time.monotonic() - started
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(f"Chrome did not answer within {timeout:.0f}s.")


WHEEL_NOTCH = 120

# A DLC whose base game you lack is told once, then again only if it is still free this many days later (#78).
MISSING_BASE_REPEAT_DAYS = 21
# A page whose answer rarely changes (a game you have, a download-only giveaway) is read again only after this many days.
RECHECK_DAYS = 3


def older_than(stamp: str | None, days: float, now: datetime | None = None) -> bool:
    """True when an ISO date is at least `days` old; a missing or unreadable one counts as long ago."""
    if not stamp:
        return True
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - then >= timedelta(days=days)


def giveaway_end(value) -> datetime | None:
    """When a giveaway ends, from an API's ISO time or GamerPower's "2026-11-05 23:59:00" (read as UTC); None when unknown."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        when = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def account_key(signin: str | None, shown: str | None) -> str:
    """What a store's rows are saved under: the sign-in setting when set, since a name read from a page can be missing."""
    return (signin or "").strip() or shown or "unknown"


def row_extra(row) -> dict:
    """A database row's extra JSON as a dict, empty when there is none or it is unreadable."""
    try:
        extra = json.loads(row.extra or "{}")
    except (TypeError, ValueError):
        return {}
    return extra if isinstance(extra, dict) else {}


def read_lately(row, statuses: tuple, days: float = RECHECK_DAYS) -> bool:
    """True for a row in one of these states whose page was read less than `days` ago, so it is not opened again yet."""
    return row is not None and row.status in statuses and not older_than(row_extra(row).get("page_checked"), days)


def mark_page_checked(row, now: datetime | None = None) -> None:
    """Note in a row's extra JSON when its store page was read."""
    extra = row_extra(row)
    extra["page_checked"] = (now or datetime.now(timezone.utc)).isoformat()
    row.extra = json.dumps(extra)


def missing_base_due(previous_status: str | None, reported_at: str | None, now: datetime | None = None,
                     days: int = MISSING_BASE_REPEAT_DAYS) -> bool:
    """True when a DLC without its base game is worth telling you about: new, newly in this state, or told weeks ago."""
    return previous_status != "failed:missing_base" or older_than(reported_at, days, now)


def missing_base_status(due: bool) -> str:
    """The summary status: shown when due, else "skipped:missing_base", which the summary always leaves out."""
    return "failed:missing_base" if due else "skipped:missing_base"


def track_missing_base(row, now: datetime | None = None, remember: bool = True) -> str:
    """Mark a database row as a DLC without its base game and return its summary status, keeping when it was told."""
    extra = row_extra(row)
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


# Shift and Control as the keys themselves, by their CDP modifier flag.
MODIFIER_KEYS = {2: ("Control", "ControlLeft", 17), 8: ("Shift", "ShiftLeft", 16)}
# Keys a US keyboard types with Shift, and the unshifted key under each.
_SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
            "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`"}
_KEY_CODES = {"-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219), "]": ("BracketRight", 221),
              "\\": ("Backslash", 220), ";": ("Semicolon", 186), "'": ("Quote", 222), ",": ("Comma", 188),
              ".": ("Period", 190), "/": ("Slash", 191), "`": ("Backquote", 192), " ": ("Space", 32)}


def key_for(char: str) -> tuple[str, int, bool]:
    """The key code, Windows key number and Shift state a US keyboard types `char` with; ("", 0, False) when it has no key."""
    base = _SHIFTED.get(char, char)
    shift = char in _SHIFTED or (char.isascii() and char.isalpha() and char.isupper())
    if base.isascii() and base.isalpha():
        return f"Key{base.upper()}", ord(base.upper()), shift
    if base.isascii() and base.isdigit():
        return f"Digit{base}", ord(base), shift
    code, number = _KEY_CODES.get(base, ("", 0))
    return code, number, shift and bool(code)


# Screenshots are a diagnostic, so three months of them are kept and no more.
SCREENSHOT_DAYS = 90
# A Chrome that does not answer the polite close in this many seconds is killed, so a hung page cannot hang the close too.
CLEAN_CLOSE_WAIT = 10
# A sign-in handed to you within this many seconds of a check the store already counted is the same check, not a second one.
CHECK_COUNT_GAP = 300
# JPEG at this quality keeps a screenshot readable at a fraction of nodriver's default size.
SCREENSHOT_QUALITY = 60


def prune_screenshots(folder: Path, days: int = SCREENSHOT_DAYS, now: datetime | None = None) -> int:
    """Delete one store's screenshots older than `days` and return how many went."""
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - days * 86400
    removed = 0
    for entry in folder.glob("*"):
        try:
            if entry.suffix.lower() in (".jpg", ".png") and entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# Picture puzzles and boxes to tick need a person; Cloudflare's own checks usually pass by themselves.
NEEDS_A_PERSON = ("hcaptcha", "h_captcha", "recaptcha", "arkose", "funcaptcha", "geetest",
                  "captcha-delivery", "datadome", "px-captcha", "talon")


def challenge_needs_you(reason: str) -> bool:
    """True when a check named by CHALLENGE_JS is a captcha only a person can solve, not one that passes by itself."""
    text = str(reason or "").lower()
    return any(mark in text for mark in NEEDS_A_PERSON)


# Two goes with the authenticator secret, then a recovery code if there is one, then you over VNC.
OTP_KEY_ATTEMPTS = 2

# A human check in whatever document it runs in: what it found (a title, a frame, a widget, a phrase), or '' for none.
CHALLENGE_JS = r"""
(() => {
    const t = (document.title || '').toLowerCase();
    if (t.includes('just a moment') || t.includes('attention required') || t.includes('one more step')) return 'page title: ' + t.slice(0, 60);
    if (document.querySelector('#challenge-form, #challenge-running, #cf-challenge-running')) return 'Cloudflare challenge form';
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
    const frame = frames.find(f => rx.test((f.getAttribute('src') || '') + ' ' + (f.getAttribute('title') || '')) && !badge(f) && seen(f));
    if (frame) return 'frame: ' + ((frame.getAttribute('src') || '').split(/[?#]/)[0].slice(0, 90) || frame.getAttribute('title'));
    const widgets = [...document.querySelectorAll('.cf-turnstile, #h_captcha, #talon_frame_login_prod, #FunCaptcha, [id*="arkose" i], #datadome')];
    const widget = widgets.find(seen);
    if (widget) return 'widget: ' + (widget.id || widget.className || widget.tagName);
    const b = (document.body ? (document.body.innerText || '') : '').toLowerCase();
    const words = ['verify you are human', 'checking your browser', 'complete a security check', 'needs to review the security of your connection',
        "check that you're a real person", 'check that you are a real person', 'geo.captcha-delivery.com'];
    const word = words.find(w => b.includes(w));
    return word ? 'page text: ' + word : '';
})()
"""

# Length of the answer a solved hCaptcha or reCAPTCHA leaves in its field, 0 when empty, -1 without one; never the answer.
CAPTCHA_ANSWER_JS = r"""
(() => {
    const fields = [...document.querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]')];
    return fields.length ? Math.max(...fields.map(f => (f.value || '').length)) : -1;
})()
"""


def captcha_answer_note(lengths) -> str:
    """How the answer fields read once a puzzle vanished: solved leaves an answer, expired leaves it empty."""
    found = [int(n) for n in lengths if isinstance(n, (int, float)) and not isinstance(n, bool)]
    if not found:
        return "answer field unreadable"
    longest = max(found)
    if longest < 0:
        return "no answer field"
    return f"answer field filled ({longest} characters)" if longest else "answer field empty"


def answer_filled(lengths) -> bool:
    """True when a vanished puzzle left an answer behind, which only a solve does."""
    return any(isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 for n in lengths)


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
            "--disk-cache-size=262144000",  # 250 MB each for files and compiled scripts, else a profile's cache grows past a gigabyte
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
                self.browser = await self._launch_chrome(
                    store_browser_dir,
                    headless=headless,
                    sandbox=False,  # required when running as root in Docker
                    browser_executable_path=chrome_path,
                    browser_args=args,
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

    async def _launch_chrome(self, profile_dir: Path, **options) -> uc.Browser:
        """Start Chrome through nodriver; one slower than nodriver's wait is waited for and attached to, not killed."""
        try:
            with chrome_timezone():
                return await uc.start(user_data_dir=str(profile_dir), **options)
        except Exception as exc:
            slow = abandoned_start(profile_dir)
            if slow is None:
                raise
            failure = exc
        started = time.monotonic()
        try:
            waited = await wait_for_chrome(slow._process, slow.config.host, slow.config.port, CHROME_START_WAIT)
            # Host and port are already in the config, so this start attaches to that Chrome instead of starting one.
            browser = await uc.start(config=slow.config)
        except BaseException as rescue:
            if not isinstance(rescue, Exception):
                # Cancelled while waiting: nothing owns this Chrome any more, so it goes now.
                self._kill_process_tree(slow._process_pid)
                raise
            self.logger.debug("Chrome could not be attached to %.0fs after nodriver gave up (%s).",
                              time.monotonic() - started, rescue)
            raise failure from rescue
        finally:
            uc.util.get_registered_instances().discard(slow)
        # The attached browser owns the process, so close_browser() still ends the whole tree.
        browser._process, browser._process_pid = slow._process, slow._process_pid
        self.logger.debug("Chrome answered %.1fs after nodriver gave up on it, so the bot attached to it instead of starting it again.",
                          waited)
        return browser

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Strip non-alphanumeric chars and lowercase for fuzzy matching."""
        return re.sub(r'[^a-z0-9]', '', str(title).lower())

    def log_browser_ready(self) -> None:
        """Standardised log for browser ready state."""
        self.logger.info("🌐 [bold yellow]Browser ready[/bold yellow]")

    def log_signed_in(self, username: str | None = None) -> None:
        """Standardised log for successful login. The account is masked in every log line, self.user keeps it whole."""
        user = username or self.user or "unknown"
        self.user = user
        hide_account(user)
        self.logger.info("🔓 [bold green]Signed in as:[/bold green] %s", mask_account(user))

    async def close_browser(self) -> None:
        """Close the browser and kill its whole process tree (issue #19)."""
        if not self.browser:
            return
        # A clean close lets Chrome write its cookies to disk before the process tree is killed.
        if self.page:
            try:
                await asyncio.wait_for(self.page.send(uc.cdp.browser.close()), timeout=CLEAN_CLOSE_WAIT)
                await asyncio.sleep(1)
            except Exception as e:
                self.logger.debug("Clean browser close failed, killing it instead: %r", e)
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

    async def take_screenshot(self, name: str, failed: bool = True) -> Path | None:
        """Take a JPEG screenshot named with the date and time when DEBUG is on; DEBUG_SCREENSHOTS also sends it when it shows a failure."""
        if not self.page or not cfg.debug:
            return None
        removed = prune_screenshots(self.screenshot_path())
        if removed:
            self.logger.debug("Deleted %d screenshot(s) older than %d days.", removed, SCREENSHOT_DAYS)
        p = self.screenshot_path(f"{filenamify(name)}_{datetime.now():%Y-%m-%d_%H-%M-%S}.jpg")
        try:
            data = await self.page.send(uc.cdp.page.capture_screenshot(format_="jpeg", quality=SCREENSHOT_QUALITY))
            p.write_bytes(base64.b64decode(data))
            self.logger.debug("Screenshot saved: %s", p)
        except Exception:
            self.logger.exception("Failed to save screenshot")
            return None
        if cfg.debug_screenshots and failed:
            await self.notify(f"📸 {self.store_name}: `{p.name}`", screenshot_path=p)
        return p

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
                vw, vh = cfg.width, cfg.height
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
            vw, vh = cfg.width, cfg.height
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
            self.logger.debug("The wheel does not move '%s' into view, scrolling to it directly.", label)
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
        self.logger.debug("'%s' is still there and clickable after the click, it did not take.", label)
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
                self.logger.debug("Clicked '%s' with the mouse.", label)
                return True
            reason = "not taken"
        if reason == "hidden" or (allow_script and reason == "not taken"):
            if await run_js(self._script_click_js(finder)) is True and await self._click_took(read, label, key, reason):
                self.logger.debug("'%s' is %s, clicked by script.", label, reason)
                return True
        self.logger.debug("'%s' not clicked: %s.", label, reason)
        return False

    @staticmethod
    def _script_click_js(finder: str) -> str:
        return f"(() => {{ const el = ({finder})(); if (el && !el.disabled) {{ el.click(); return true; }} return false; }})()"

    async def _click_like_a_hand(self, finder: str, label: str) -> bool:
        """Wheel `finder`'s element into view and click it with the mouse; False when it cannot be clicked."""
        async def read() -> dict:
            return await self._read_box(self.page.evaluate, finder)

        box = await self._bring_into_view(read, self.page.evaluate, self._page_wheel_point, finder, label)
        if "x" not in box:
            self.logger.debug("'%s' not clicked with the mouse: %s.", label, box.get("reason", "missing"))
            return False
        await self._human_click(box["x"], box["y"])
        self.logger.debug("Clicked '%s' with the mouse (%s).", label, box.get("key", ""))
        return True

    async def _modifier_key(self, flag: int, down: bool) -> None:
        """Press or let go Shift (8) or Control (2) as a key of its own, the way a keyboard sends it."""
        key, code, number = MODIFIER_KEYS[flag]
        await self.page.send(uc.cdp.input_.dispatch_key_event(
            type_="keyDown" if down else "keyUp", key=key, code=code, windows_virtual_key_code=number,
            modifiers=flag if down else 0, location=1))
        await asyncio.sleep(random.uniform(0.03, 0.09))

    async def _type_like_a_hand(self, text: str) -> None:
        """Type into the focused field key by key, Shift held down for capitals and symbols, at a hand's uneven pace."""
        holding = False
        # A store can give its account a pace of its own; 1.0 is the base.
        pace = getattr(self, "typing_pace", 1.0)
        for char in str(text):
            code, number, shift = key_for(char)
            if not code:
                # No key for it: inserted the way an input method or emoji keyboard does, not as a key press with no key (probe 7.11).
                if holding:
                    await self._modifier_key(8, False)
                    holding = False
                await self.page.send(uc.cdp.input_.insert_text(text=char))
                await asyncio.sleep(random.uniform(0.09, 0.29) * pace)
                continue
            if shift != holding:
                await self._modifier_key(8, shift)
                holding = shift
            modifiers = 8 if shift else 0
            await self.page.send(uc.cdp.input_.dispatch_key_event(
                type_="keyDown", modifiers=modifiers, key=char, code=code, windows_virtual_key_code=number,
                text=char, unmodified_text=char))
            await asyncio.sleep(random.uniform(0.03, 0.09) * pace)
            await self.page.send(uc.cdp.input_.dispatch_key_event(
                type_="keyUp", modifiers=modifiers, key=char, code=code, windows_virtual_key_code=number))
            # Now and then a longer gap, the way a hand stops between words.
            await asyncio.sleep((random.uniform(0.06, 0.2) + (random.uniform(0.3, 0.9) if random.random() < 0.06 else 0)) * pace)
        if holding:
            await self._modifier_key(8, False)

    async def _press_key(self, key: str, code: str, number: int, text: str = "", modifiers: int = 0,
                         commands: list[str] | None = None) -> None:
        """Press one key like a keyboard: Shift or Control first when held, key down with its text, key up."""
        held = [flag for flag in MODIFIER_KEYS if modifiers & flag]
        for flag in held:
            await self._modifier_key(flag, True)
        await self.page.send(uc.cdp.input_.dispatch_key_event(
            type_="keyDown", key=key, code=code, windows_virtual_key_code=number, modifiers=modifiers,
            text=text or None, unmodified_text=text or None, commands=commands))
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await self.page.send(uc.cdp.input_.dispatch_key_event(
            type_="keyUp", key=key, code=code, windows_virtual_key_code=number, modifiers=modifiers))
        for flag in reversed(held):
            await self._modifier_key(flag, False)

    async def _press_enter(self) -> None:
        """Press Enter, which sends a form."""
        await self._press_key("Enter", "Enter", 13, "\r")

    async def _select_all(self) -> None:
        """Ctrl+A in the focused field, so what is typed next replaces its text."""
        await self._press_key("a", "KeyA", 65, modifiers=2, commands=["selectAll"])

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

    async def _count_check(self, store: str, reason: str | None = None, puzzle: bool | None = None) -> None:
        """Count a human check for the weekly line, once per visit; without a reason the page itself is read."""
        counted = self.__dict__.setdefault("_checks_counted", {})
        if time.monotonic() - counted.get(store, float("-inf")) < CHECK_COUNT_GAP:
            return
        if reason is None:
            try:
                reason = await self._human_challenge_reason()
            except Exception as exc:
                self.logger.debug("Could not look for a human check on the page: %s", exc)
                return
        if not reason:
            return
        self.logger.debug("Human check counted for %s: %s", store, reason)
        counted[store] = time.monotonic()
        record_check(store, challenge_needs_you(reason) if puzzle is None else puzzle, await page_brief(getattr(self, "page", None)))
        await self.take_screenshot(f"challenge_{store}")

    async def _wait_for_vnc_login(self, check_fn, *, timeout: int | None = None, interval: int = 5, log_interval: int = 60, custom_msg: str | None = None, store_key: str | None = None) -> bool:
        """Wait for manual VNC login.

        Polls every `interval` seconds, but only logs a waiting message every `log_interval` seconds.
        A store whose last prompt went unanswered is not asked again in this run.
        """
        timeout = timeout or cfg.vnc_login_timeout
        key = store_key or self.store_name
        from src.core.notifier import notify

        # Prime, Steam, GOG and Microsoft hand a sign-in captcha straight to you, so it is counted here.
        await self._count_check(key)
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
                    self.logger.info("Still waiting for you… %ds left.", remaining)
        mark_unanswered(key)
        return False

    async def _human_challenge_present(self) -> bool:
        """True when the page shows a Cloudflare or captcha check you could act on (see CHALLENGE_JS)."""
        return bool(await self._human_challenge_reason())

    async def _human_challenge_reason(self) -> str:
        """What made the page read as a human check (a title, a frame, a widget, a phrase), empty when there is none."""
        if not self.page:
            return ""
        try:
            return str(await self.page.evaluate(CHALLENGE_JS) or "")
        except Exception:
            return ""

    async def _page_brief(self) -> str:
        """The page's address and title, for a debug line about a page the bot could not read."""
        return await page_brief(self.page) or "unreadable page"

    async def _wait_out_challenge(self, label: str, settle: int = 12, store_key: str | None = None,
                                  present_fn=None, ask: bool = True, reason_fn=None) -> bool:
        """Let a human check pass by itself for ``settle`` seconds, then ask you over VNC once per store and run; True once it cleared.

        ``present_fn`` and ``reason_fn`` look somewhere else, such as a checkout frame; ``ask=False`` only waits and never asks.
        """
        present = present_fn or self._human_challenge_present
        reason_of = reason_fn or self._human_challenge_reason
        key = store_key or self.store_name
        # Set only once the check really went to you over VNC, so a puzzle that vanished in the first seconds is not yours.
        self._handed_over = False
        waited = 0
        # The last look comes right before asking, so a check that just cleared never pings you.
        while True:
            if not await present():
                return True
            if waited == 0:
                reason = await reason_of()
                self.logger.debug("%s: human check seen: %s", label, reason or "kind not read")
                record_check(key, challenge_needs_you(reason), await page_brief(getattr(self, "page", None)))
                self.__dict__.setdefault("_checks_counted", {})[key] = time.monotonic()
                # Every check is kept for a later look; DEBUG_SCREENSHOTS sends it like a failure.
                await self.take_screenshot(f"challenge_{key}")
                if waits_for_nobody(key) and challenge_needs_you(reason):
                    self.logger.debug("%s: nobody answered this run's prompt, and this one needs a person, so it is not waited for.", label)
                    return False
            if waited >= settle:
                break
            await asyncio.sleep(2)
            waited += 2
        if not ask:
            return False
        self.logger.debug("%s: the human check is still there after %ds: %s", label, settle, await reason_of() or "kind not read")
        # Nobody answered this store's last prompt, so it is not asked again and the log does not claim it is.
        if waits_for_nobody(key):
            return False

        self.logger.warning("%s is behind a Cloudflare / captcha human-check – requesting manual solve via VNC.", label)
        custom_msg = self._vnc_notice(
            f"{label}: security check",
            "A Cloudflare / captcha human-check is blocking the bot. Open the browser and complete it to continue.",
        )

        async def _cleared() -> bool:
            return not await present()

        self._handed_over = True
        return await self._wait_for_vnc_login(_cleared, custom_msg=custom_msg, store_key=store_key)

    def _captcha_holds(self, ends) -> bool:
        """True once a captcha here went unsolved, for an item not on its known last run: it waits for the next run."""
        # A single run cannot tell a last run, so there everything waits for the next start instead of drawing puzzles.
        return waits_for_nobody(self.store_name) and not lost_before_next_run(ends)

    def _captcha_held(self, held: int, item: str) -> None:
        """Tell once how many items an unsolved captcha left for the next run."""
        if held:
            self.logger.warning("A captcha was not solved, so %d more %s(s) wait for the next run.", held, item)
            needs_you(self.store_name, held, item, why=CAPTCHA_UNSOLVED)

    async def sleep(self, seconds: float) -> None:
        """Async sleep wrapper."""
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------
    # Entry point (to be overridden)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Override in subclasses to implement the claiming logic."""
        raise NotImplementedError
