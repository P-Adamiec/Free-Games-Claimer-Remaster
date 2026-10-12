"""What one claiming run has already learned about the person at the keyboard.

A store whose VNC prompt went unanswered is not asked again in the same run: waiting
another full timeout would change nothing, and re-opening its sign-in page over and
over is exactly the behaviour that lowers a session's trust score.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("fgc.run")

_unanswered: set[str] = set()
_skipped: dict[str, int] = {}
# Asks the scheduler when it runs the bot next, at the moment that is needed; None for a single run.
_next_run_source = None
_run_started: datetime | None = None


def _key(store: str) -> str:
    return str(store or "").strip().lower()


def waits_for_nobody(store: str) -> bool:
    """True when this store already had a prompt nobody answered in this run."""
    return _key(store) in _unanswered


def mark_unanswered(store: str) -> None:
    """Remember that a prompt for this store timed out with no one acting on it."""
    key = _key(store)
    if key and key not in _unanswered:
        _unanswered.add(key)
        logger.debug("Nobody answered the %s prompt, the rest of this run skips it.", key)


def mark_answered(store: str) -> None:
    """A prompt was acted on, so the next one gets the full wait again."""
    _unanswered.discard(_key(store))


def needs_you(store: str, skipped: int = 1) -> None:
    """Count what this store could not do without you, for the run summary."""
    key = _key(store)
    if key:
        _skipped[key] = _skipped.get(key, 0) + skipped


def waiting_for_you() -> dict:
    """Stores that needed you in this run, mapped to how much they skipped."""
    return dict(_skipped)


def set_next_run_source(source) -> None:
    """Give a function that returns when the next scheduled run starts (an aware datetime) or None."""
    global _next_run_source
    _next_run_source = source


def next_run():
    """When the next scheduled run starts, asked now; None when this run is the last one."""
    try:
        return _next_run_source() if _next_run_source else None
    except Exception as e:
        logger.debug("Could not ask the scheduler for the next run: %s", e)
        return None


def run_started() -> datetime | None:
    """When this run began, so a store late in it knows how long a run takes to reach it."""
    return _run_started


def reset_run_state() -> None:
    """Start a run with a clean slate. The container runs for weeks, runs do not."""
    global _run_started
    _unanswered.clear()
    _skipped.clear()
    _run_started = datetime.now(timezone.utc)
