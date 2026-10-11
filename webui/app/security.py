"""Login throttling and CSRF tokens."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from pathlib import Path

from webui import protocol

WINDOW = 15 * 60          # failures older than this are forgotten
FREE_TRIES = 3            # no delay for the first few typos
LOCK_AFTER = 10           # then refuse outright for the rest of the window
MAX_DELAY = 30.0


class Throttle:
    """Failed logins per client address and per user name, in memory."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        now = self.clock()
        kept = [t for t in self.failures.get(key, []) if now - t < WINDOW]
        if kept:
            self.failures[key] = kept
        else:
            self.failures.pop(key, None)
        return kept

    def count(self, *keys: str) -> int:
        with self._lock:
            return max((len(self._recent(k)) for k in keys), default=0)

    def blocked(self, *keys: str) -> bool:
        return self.count(*keys) >= LOCK_AFTER

    def delay(self, *keys: str) -> float:
        """Seconds to wait before answering this attempt."""
        n = self.count(*keys)
        return 0.0 if n < FREE_TRIES else min(2.0 ** (n - FREE_TRIES + 1), MAX_DELAY)

    def fail(self, *keys: str) -> None:
        with self._lock:
            now = self.clock()
            for k in keys:
                self.failures.setdefault(k, []).append(now)

    def success(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self.failures.pop(k, None)


def csrf_token(session: dict) -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_ok(session: dict, given: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and given and hmac.compare_digest(expected, given))


def secret_key(env_value: str | None, path: Path) -> str:
    """WEBUI_SECRET_KEY, or a random key kept on the web interface's own volume."""
    if env_value and len(env_value) >= 32:
        return env_value
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    protocol.atomic_write(path, key + "\n", mode=0o600)
    return key
