"""Every human check the bot meets (a Cloudflare page, a captcha puzzle, a sign-in captcha handed to you, AliExpress's slider, Bing's check), kept for two weeks in data/human_checks.json.
The log names how many each store showed in the last seven days.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from src.core.config import cfg

logger = logging.getLogger("fgc.checks")

STATE_FILE = "human_checks.json"
# Two weeks of history, and never more lines than that would hold even on a bad week.
KEEP_DAYS = 14
MAX_ENTRIES = 500


def _path():
    return cfg._data_dir / STATE_FILE


def load() -> list[dict]:
    """The saved checks, oldest first; empty when there is no file or it is unreadable."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def record(store: str, puzzle: bool, where: str = "", now: datetime | None = None) -> None:
    """Note one human check: the store, a puzzle or a check that passes by itself, and the page without its query."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=KEEP_DAYS)
    entries = [e for e in load() if _when(e) and _when(e) >= cutoff]
    entries.append({"at": now.isoformat(timespec="seconds"), "store": store, "kind": "puzzle" if puzzle else "check",
                    "where": str(where or "")[:120]})
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(entries[-MAX_ENTRIES:], indent=1), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not save the human check: %s", exc)


def _when(entry: dict) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def week_summary(now: datetime | None = None) -> str:
    """One line for the log, e.g. "epic 3 (2 puzzles), itchio 1"; empty when no store asked in the last seven days."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=7)
    counts: dict[str, list[int]] = {}
    for entry in load():
        when = _when(entry)
        if when and when >= cutoff:
            total = counts.setdefault(str(entry.get("store") or "?"), [0, 0])
            total[0] += 1
            total[1] += entry.get("kind") == "puzzle"
    return ", ".join(f"{store} {n} ({p} puzzle{'' if p == 1 else 's'})" if p else f"{store} {n}"
                     for store, (n, p) in sorted(counts.items()))
