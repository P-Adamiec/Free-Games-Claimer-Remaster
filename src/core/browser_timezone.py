"""The browser's time zone, matched to where your IP address is, so a site never sees UTC next to a Polish IP.

TZ in your settings wins, BROWSER_TIMEZONE can name a zone or switch the lookup off, otherwise a saved lookup decides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from src.core.config import cfg

logger = logging.getLogger("fgc.timezone")

# Free services that answer for the asking IP without a key; the second is asked only when the first fails.
PROVIDERS = (
    ("https://ipapi.co/timezone/", "text"),
    ("https://ipwho.is/?fields=timezone", "json"),
)

_zone: str | None = None

# The looked-up zone and when, kept so your IP address is not sent on every run.
STATE_FILE = "browser_timezone.json"
# How long that answer is trusted, by BROWSER_TIMEZONE_REFRESH.
REFRESH_DAYS = {"run": 0, "week": 7, "month": 30}


def lookup_due(state: dict, every: str, now: datetime) -> bool:
    """True when the saved zone is missing, unreadable, from the future or older than the chosen interval."""
    days = REFRESH_DAYS.get(every, REFRESH_DAYS["month"])
    if not days or not valid_zone((state or {}).get("zone")):
        return True
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")))
    except ValueError:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return checked > now or now - checked >= timedelta(days=days)


def _state_path():
    return cfg._data_dir / STATE_FILE


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(zone: str, now: datetime) -> None:
    try:
        _state_path().write_text(json.dumps({"zone": zone, "checked_at": now.isoformat()}), encoding="utf-8")
    except Exception as e:
        logger.debug("Could not save the time zone to %s: %s", STATE_FILE, e)


def valid_zone(name) -> str | None:
    """The IANA name when it is a real time zone (Python and Chrome read the same database), else None."""
    name = str(name or "").strip()
    if not name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
    except Exception:
        # A region folder such as "Europe" raises an OS error, not ZoneInfoNotFoundError.
        return None
    return name


def zone_from_answer(kind: str, body: str) -> str | None:
    """The time zone in a provider's answer: plain text, or ipwho.is JSON with {"timezone": {"id": ...}}."""
    if kind == "text":
        return valid_zone(body)
    try:
        data = json.loads(body or "{}")
    except Exception:
        return None
    zone = data.get("timezone") if isinstance(data, dict) else None
    if isinstance(zone, dict):
        zone = zone.get("id")
    return valid_zone(zone)


async def lookup(timeout: float = 6.0) -> str | None:
    """Ask the providers in turn which time zone this IP address is in."""
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "fgc-remaster"}) as client:
        for url, kind in PROVIDERS:
            try:
                answer = await client.get(url)
            except httpx.HTTPError as e:
                logger.debug("Time zone lookup at %s failed: %s", url, e)
                continue
            zone = zone_from_answer(kind, answer.text) if answer.status_code == 200 else None
            if zone:
                return zone
            logger.debug("%s gave no time zone (HTTP %s).", url, answer.status_code)
    return None


async def refresh() -> None:
    """Decide the browser's time zone for this run."""
    global _zone
    if cfg.tz:
        logger.debug("TZ is set to %s, the browser uses it as it is.", cfg.tz)
        return
    setting = (cfg.browser_timezone or "auto").strip()
    if setting.lower() == "off":
        logger.debug("BROWSER_TIMEZONE=off, the browser stays on UTC.")
        _zone = None
        return
    if setting.lower() != "auto":
        _zone = valid_zone(setting)
        if not _zone:
            logger.warning("BROWSER_TIMEZONE=%s is not a time zone name (e.g. Europe/Warsaw), so the browser stays on UTC.",
                           setting)
        return
    every = (cfg.browser_timezone_refresh or "month").strip().lower()
    if every not in REFRESH_DAYS:
        logger.warning("BROWSER_TIMEZONE_REFRESH=%s is not run, week or month, so month is used.", every)
        every = "month"
    now = datetime.now(timezone.utc)
    state = _load_state()
    if not lookup_due(state, every, now):
        _zone = valid_zone(state.get("zone"))
        logger.debug("Browser time zone: %s, looked up %s, asked again after a %s.", _zone, state.get("checked_at"), every)
        return
    try:
        zone = await asyncio.wait_for(lookup(), timeout=15)
    except Exception as e:
        logger.debug("Time zone lookup gave up: %s", e)
        zone = None
    if not zone:
        # The saved zone beats UTC, and the date is left alone so the next run tries again.
        _zone = _zone or valid_zone(state.get("zone"))
        logger.debug("Could not look up the time zone, the browser keeps %s.", _zone or "UTC")
        return
    _save_state(zone, now)
    if zone != _zone:
        logger.debug("Browser time zone: %s (from your IP address).", zone)
    _zone = zone


@contextmanager
def chrome_timezone():
    """TZ set only while Chrome starts: Chrome and every frame it opens keep it, this process's own clock does not."""
    zone = None if cfg.tz else _zone
    if not zone:
        yield
        return
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        # The C library caches the zone, so this process's log times are put back too.
        if hasattr(time, "tzset"):
            time.tzset()
