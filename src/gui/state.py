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
    "unity": {"name": "Unity", "badge": "U", "color": "#ffffff"},
    "gamerpower": {"name": "GamerPower", "badge": "⚡", "color": "#ffb020"},
    "aliexpress": {"name": "AliExpress", "badge": "AE", "color": "#ff4747"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_count(value) -> int | None:
    """Return a non-negative dashboard-safe integer, never a boolean."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _game_outcome(status: str) -> str:
    """Reduce store-specific status text to a small, secret-free vocabulary."""
    normalized = status.lower()
    if "fail" in normalized or "blocked" in normalized or "error" in normalized:
        return "failed"
    if "dry run" in normalized or "available" in normalized:
        return "available"
    if "already" in normalized or "exist" in normalized:
        return "owned"
    if "needs linking" in normalized:
        return "action_required"
    if "skip" in normalized:
        return "skipped"
    if "claimed" in normalized or "redeemed" in normalized or normalized.startswith("code:"):
        return "claimed"
    return "processed"


def summarize_store_result(store_key: str, result) -> tuple[str, dict | None]:
    """Build the dashboard summary using an explicit allowlist of safe fields."""
    if not isinstance(result, dict):
        return "Completed with no changes", None

    if store_key == "aliexpress" and isinstance(result.get("checkin"), dict):
        source = result["checkin"]
        outcome = source.get("outcome")
        if outcome not in {"collected", "collected_manual", "already_collected", "not_collected", "available"}:
            outcome = "not_collected"
        details = {
            "kind": "coins",
            "outcome": outcome,
            "claimedCoins": _safe_count(source.get("claimedCoins")),
            "offeredCoins": _safe_count(source.get("offeredCoins")),
            "balance": _safe_count(source.get("balance")),
            "streakDays": _safe_count(source.get("streakDays")),
            "tomorrowCoins": _safe_count(source.get("tomorrowCoins")),
        }
        claimed = details["claimedCoins"]
        if outcome in {"collected", "collected_manual"}:
            message = f"{claimed} coins collected" if claimed is not None else "Coins collected"
        elif outcome == "already_collected":
            message = "Coins already collected today"
        elif outcome == "available":
            message = "Collection available (dry run)"
        else:
            message = "Coins not collected"
        return message, details

    items = []
    for game in result.get("games") or []:
        if not isinstance(game, dict):
            continue
        title = game.get("title")
        status = game.get("status")
        if not isinstance(title, str) or not title.strip():
            continue
        items.append({
            "title": title.strip()[:180],
            "outcome": _game_outcome(status if isinstance(status, str) else ""),
        })

    if not items:
        return "Completed with no changes", None

    claimed = sum(item["outcome"] == "claimed" for item in items)
    if claimed:
        message = f"{claimed} claimed · {len(items)} checked"
    else:
        message = f"{len(items)} checked"
    return message, {"kind": "games", "items": items}


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
                "messageKey": "status.waiting",
                "lastRun": None,
                "details": None,
            }
            for key, meta in STORE_META.items()
        }

    def begin_run(self, store_keys: list[str]) -> None:
        with self._lock:
            self._running = True
            self._started_at = _now()
            for key in store_keys:
                if key in self._stores:
                    self._stores[key].update(
                        state="queued", message="Na fila", messageKey="status.queued", details=None
                    )

    def begin_store(self, key: str) -> None:
        with self._lock:
            if key in self._stores:
                self._stores[key].update(
                    state="running", message="Executando agora", messageKey="status.runningNow", details=None
                )

    def finish_store(
        self,
        key: str,
        message: str,
        *,
        failed: bool = False,
        details: dict | None = None,
        message_key: str | None = None,
    ) -> None:
        with self._lock:
            if key in self._stores:
                self._stores[key].update(
                    state="error" if failed else "success",
                    message=message,
                    messageKey=message_key or (
                        "status.failed" if failed else (
                            "status.resultCoins" if details and details.get("kind") == "coins" else
                            "status.resultGames" if details and details.get("kind") == "games" else
                            "status.completedNoChanges"
                        )
                    ),
                    lastRun=_now(),
                    details=deepcopy(details),
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
