"""Update check – tells you when a newer release of the bot is published.

Reads the latest release from GitHub (no account, no data sent about you) and
sends one notification per new version. Failures are never fatal: if GitHub is
unreachable the bot simply carries on claiming.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from src.core.config import cfg
from src.version import __repo__, __version__

logger = logging.getLogger("fgc.updates")

STATE_FILE = "update_check.json"

# Wait this long before asking GitHub again (shorter after a failed attempt).
CHECK_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(hours=1)


def _parse_version(text: str) -> tuple[int, ...]:
    """Turn '1.5', 'v1.5' or 'v1.3d' into a comparable number tuple."""
    match = re.match(r"[vV]?(\d+(?:\.\d+)*)", str(text or "").strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _releases_url() -> str:
    """Latest-release endpoint for this repo (works for forks too)."""
    owner_repo = str(__repo__).rstrip("/").split("github.com/")[-1]
    return f"https://api.github.com/repos/{owner_repo}/releases/latest"


def _state_path():
    return cfg._data_dir / STATE_FILE


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("Could not write %s: %s", STATE_FILE, e)


def _due_for_check(state: dict) -> bool:
    """False while the last check is still recent enough."""
    try:
        last = datetime.fromisoformat(state["last_check"])
    except Exception:
        return True
    wait = RETRY_INTERVAL if state.get("last_result") == "error" else CHECK_INTERVAL
    return datetime.now(timezone.utc) - last >= wait


async def fetch_latest_release() -> dict | None:
    """Read the newest published release from GitHub. None on any failure."""
    url = _releases_url()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"fgc-remaster/{__version__}",
            })
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.debug("Update check failed (%s): %s", url, e)
        return None

    tag = str(data.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not _parse_version(tag):
        logger.debug("Update check: unusable tag_name %r", tag)
        return None
    return {
        "version": version,
        "tag": tag,
        "url": data.get("html_url") or f"{__repo__}/releases/latest",
    }


def build_message(release: dict) -> str:
    """Short, factual update notice."""
    return (
        f"🔄 **Update available: {release['tag']}**\n"
        f"You are running v{__version__}.\n"
        f"What changed: {release['url']}\n"
        f"To update: `docker compose pull && docker compose up -d`"
    )


async def notify_if_update_available(*, at_startup: bool = False) -> None:
    """Check for a newer release and notify once per version. Never raises."""
    if not cfg.notify_updates:
        return

    state = _load_state()
    if not at_startup and not _due_for_check(state):
        logger.debug("Update check skipped (last check %s)", state.get("last_check"))
        return

    release = await fetch_latest_release()
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    state["last_result"] = "error" if release is None else "ok"
    if release is None:
        _save_state(state)
        return

    if _parse_version(release["version"]) <= _parse_version(__version__):
        logger.debug("Up to date (running %s, latest release %s)", __version__, release["tag"])
        _save_state(state)
        return

    logger.info("⬆️ Update available: %s (you are running v%s). %s",
                release["tag"], __version__, release["url"])

    if state.get("notified_version") == release["version"]:
        logger.debug("Update %s already announced, not notifying again", release["tag"])
        _save_state(state)
        return

    from src.core.notifier import notify

    try:
        await notify(build_message(release), title="Free Games Claimer update")
        state["notified_version"] = release["version"]
    except Exception as e:
        logger.debug("Could not send update notification: %s", e)
    _save_state(state)
