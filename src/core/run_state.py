"""What one claiming run has already learned about the person at the keyboard.

A store whose VNC prompt went unanswered is not asked again in the same run: waiting
another full timeout would change nothing, and re-opening its sign-in page over and
over is exactly the behaviour that lowers a session's trust score.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("fgc.run")

_unanswered: set[str] = set()
_skipped: dict[tuple[str, str, str], int] = {}
NOT_SIGNED_IN = "the sign-in was not finished"
CAPTCHA_UNSOLVED = "a captcha was not solved"
# The same, for a giveaway that ends before the next run: "waits for the next run" would not be true.
CAPTCHA_ENDS = "a captcha was not solved before the giveaway ends"
NO_ANSWER = "nobody answered over VNC"
# Asks the scheduler when it runs the bot next, at the moment that is needed; None for a single run.
_next_run_source = None
_run_started: datetime | None = None
# Puzzles nobody solved, kept per store for a meeting with you in noVNC later in this run.
_booked: dict[str, list[dict]] = {}
# A booking lasts until 30 minutes before the giveaway ends and 15 before the next run.
BEFORE_END = timedelta(minutes=30)
BEFORE_RUN = timedelta(minutes=15)
SHORTEST = timedelta(minutes=10)


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


def needs_you(store: str, skipped: int, item: str, why: str = NOT_SIGNED_IN) -> None:
    """Count the items (game, key, code) this store left for the next run without you, and why; 0 when not known yet."""
    key = _key(store)
    if key:
        _skipped[(key, why, item)] = _skipped.get((key, why, item), 0) + skipped


def waiting_for_you() -> dict:
    """(store, why, item) that needed you in this run, mapped to how many items wait for the next run."""
    return dict(_skipped)


def describe_wait(why: str, count: int, item: str) -> str:
    """The sentence for the summary, e.g. 'a captcha was not solved, so 3 games wait for the next run.'"""
    if not count:
        return f"{why}, so nothing was claimed."
    plural = "" if count == 1 else "s"
    if why == CAPTCHA_ENDS:
        return f"{why}, so {count} {item}{plural} could not be claimed."
    return f"{why}, so {count} {item}{plural} wait{'s' if count == 1 else ''} for the next run."


def utc_text(when: datetime) -> str:
    """A time for the log and notifications, in UTC."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def booking_until(ends: datetime | None, last_chance: bool, now: datetime | None = None) -> datetime | None:
    """Until when a puzzle nobody solved can wait for you in noVNC; None when that leaves too little time."""
    now = now or datetime.now(timezone.utc)
    upcoming = next_run()
    # A single run (RUN_ONCE, Ofelia, cron, --once) ends at once, its next start tries again (owner, 2026-11-02).
    if upcoming is None:
        return None
    from src.core.config import cfg
    # A meeting can still wait one prompt for you, so even that ends before the next run; a run close by leaves no time.
    limits = [upcoming - BEFORE_RUN - timedelta(seconds=cfg.vnc_login_timeout)]
    if last_chance and ends is not None:
        limits.append(ends - BEFORE_END)
    until = min(limits)
    return until if until - now >= SHORTEST else None


def book(store: str, item: dict) -> bool:
    """Keep an item (title, data, ends, last_chance, until, seen) for a meeting in noVNC; False when it is already kept."""
    items = _booked.setdefault(_key(store), [])
    if any(kept["title"] == item["title"] for kept in items):
        return False
    items.append(item)
    return True


def booked() -> dict:
    """The items kept for a meeting, per store."""
    return {store: list(items) for store, items in _booked.items() if items}


def is_booked(store: str, title: str) -> bool:
    """True when this item already waits for a meeting with you."""
    return any(item["title"] == title for item in _booked.get(_key(store), []))


def unbook_title(store: str, title: str) -> None:
    """Drop an item that got claimed after all."""
    unbook(store, [item for item in _booked.get(_key(store), []) if item["title"] == title])


def unbook(store: str, items: list) -> None:
    """Drop items whose meeting took place or whose time ran out."""
    gone = {id(item) for item in items}
    _booked[_key(store)] = [item for item in _booked.get(_key(store), []) if id(item) not in gone]


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


def ends_before_next_run(ends: datetime | None, now: datetime | None = None) -> bool:
    """True when a giveaway ends before the next run gets to it, so this run is its last chance; False when its end is unknown."""
    if ends is None:
        return False
    now = now or datetime.now(timezone.utc)
    upcoming = next_run()
    # A trigger due while this run still works is dropped by the scheduler, so it is no next run (as AliExpress counts it).
    if upcoming is None or upcoming <= now + timedelta(minutes=20):
        return True
    # The next run gets to the store as late into it as this one did.
    started = run_started()
    reach = now - started if started and started <= now else timedelta(0)
    return ends <= upcoming + reach


def lost_before_next_run(ends: datetime | None) -> bool:
    """True when a giveaway ends before a known next run; a single run cannot tell, its next start may still make it."""
    return next_run() is not None and ends_before_next_run(ends)


def run_started() -> datetime | None:
    """When this run began, so a store late in it knows how long a run takes to reach it."""
    return _run_started


def reset_run_state() -> None:
    """Start a run with a clean slate. The container runs for weeks, runs do not."""
    global _run_started
    _unanswered.clear()
    _skipped.clear()
    _booked.clear()
    _run_started = datetime.now(timezone.utc)
