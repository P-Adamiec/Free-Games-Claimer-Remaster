"""Thread-safe runtime state consumed by the local dashboard."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock


STORE_META = {
    "steam": {"name": "Steam", "badge": "S", "color": "#1b9fff"},
    "epic": {"name": "Epic Games", "badge": "E", "color": "#ffffff"},
    "fab": {"name": "Fab", "badge": "F", "color": "#6e56cf"},
    "prime": {"name": "Prime Gaming", "badge": "P", "color": "#9146ff"},
    "gog": {"name": "GOG", "badge": "G", "color": "#b65cff"},
    "ubisoft": {"name": "Ubisoft", "badge": "U", "color": "#29a3ff"},
    "gamerpower": {"name": "GamerPower", "badge": "⚡", "color": "#ffb020"},
    "aliexpress": {"name": "AliExpress", "badge": "AE", "color": "#ff4747"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DashboardState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._running = False
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._stores = {
            key: {
                **meta,
                "key": key,
                "state": "idle",
                "message": "Aguardando execução",
                "lastRun": None,
            }
            for key, meta in STORE_META.items()
        }

    def begin_run(self, store_keys: list[str]) -> None:
        with self._lock:
            self._running = True
            self._started_at = _now()
            for key in store_keys:
                if key in self._stores:
                    self._stores[key].update(state="queued", message="Na fila")

    def begin_store(self, key: str) -> None:
        with self._lock:
            if key in self._stores:
                self._stores[key].update(state="running", message="Executando agora")

    def finish_store(self, key: str, message: str, *, failed: bool = False) -> None:
        with self._lock:
            if key in self._stores:
                self._stores[key].update(
                    state="error" if failed else "success",
                    message=message,
                    lastRun=_now(),
                )

    def finish_run(self) -> None:
        with self._lock:
            self._running = False
            self._finished_at = _now()

    def snapshot(self, enabled: list[str], schedule: dict | None = None) -> dict:
        with self._lock:
            stores = deepcopy(list(self._stores.values()))
            payload = {
                "running": self._running,
                "startedAt": self._started_at,
                "finishedAt": self._finished_at,
                "stores": stores,
            }
        enabled_set = set(enabled)
        for store in payload["stores"]:
            store["enabled"] = store["key"] in enabled_set
        payload["schedule"] = schedule or {}
        return payload


dashboard_state = DashboardState()
