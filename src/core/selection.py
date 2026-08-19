"""Which stores this run was asked to handle.

Lets a delegating store (GamerPower) skip work for stores the user did not start,
without importing main.py, which would be a circular import.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("fgc.selection")

_active: set[str] | None = None


def set_active_stores(keys) -> None:
    """Record the stores main.py resolved for this run."""
    global _active
    _active = {str(key).strip().lower() for key in (keys or []) if str(key).strip()}
    logger.debug("Active stores for this run: %s", sorted(_active))


def is_store_active(key: str) -> bool:
    """True when the store runs in this session. Nothing recorded means no restriction."""
    if _active is None:
        return True
    return str(key).strip().lower() in _active


def apply_run_selection(keys) -> None:
    """Restrict delegation to this run's stores, unless GamerPower was picked on its own."""
    others = [key for key in (keys or []) if str(key).strip().lower() != "gamerpower"]
    if others:
        set_active_stores(keys)
    else:
        # Nothing else was chosen, so there is no choice to respect and everything is fair game.
        reset_active_stores()


def reset_active_stores() -> None:
    """Drop the recording, used by tests to get back to the unrestricted default."""
    global _active
    _active = None
