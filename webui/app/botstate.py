"""Everything the web interface knows about a bot, read from that bot's data volume.

The web container mounts each bot's volume at <FGC_BOTS_DIR>/<name> (default
/bots/<name>). It reads status.json, bot.log and fgc.db there, and writes
config.env and the request files. It never talks to Docker.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from webui import protocol
from webui.app import config_store, schema

ALIVE_SECONDS = 30


@dataclass(frozen=True)
class Bot:
    name: str
    index: int
    vnc_host: str
    vnc_port: int
    dir: Path
    env_file: Path | None

    @property
    def webui_dir(self) -> Path:
        return protocol.webui_dir(self.dir)

    @property
    def config_file(self) -> Path:
        return self.dir / protocol.CONFIG_FILE


NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def bots_from_env(environ=os.environ) -> list[Bot]:
    """FGC_BOTS=name,name, written by webui/bots.sh into the compose override.

    Bot <name> is the compose service fgc-<name>: its data volume is mounted at
    <FGC_BOTS_DIR>/<name> and its noVNC answers on fgc-<name>:7080, inside the
    compose network only.
    """
    base = Path(environ.get("FGC_BOTS_DIR") or "/bots")
    env_dir = Path(environ.get("FGC_BOTS_ENV_DIR") or "/bots-env")
    host_tpl = environ.get("FGC_BOT_HOST") or "fgc-{name}"
    out: list[Bot] = []
    for item in (p.strip() for p in (environ.get("FGC_BOTS") or "").split(",")):
        name = item.partition(":")[0].strip().lower()
        if not NAME_RE.match(name) or any(b.name == name for b in out):
            continue
        out.append(Bot(name, len(out), host_tpl.format(name=name), 7080, base / name, env_dir / f"{name}.env"))
    return out


def display_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("WEBUI_TIMEZONE") or "Europe/Amsterdam")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_time(value) -> str:
    dt = parse_time(value)
    return dt.astimezone(display_tz()).strftime("%d-%m-%Y %H:%M") if dt else "–"


# ------------------------------------------------------------------ schedule

def default_schedule(index: int) -> dict[str, str]:
    """A different pair of daily times per bot, so two Chromes never run at once by default."""
    hour = 6 + 3 * (index % 4)            # 06/18, 09/21, 12/00, 15/03
    minute = 30 * ((index // 4) % 2)      # bots 5-8 half an hour later
    times = f"{hour:02d}:{minute:02d},{(hour + 12) % 24:02d}:{minute:02d}"
    return {"SCHEDULER_HOURS": "0", "SCHEDULER_FIXED_TIMES": times,
            "SCHEDULER_TIMEZONE": "Europe/Amsterdam", "RUN_ON_STARTUP": "false"}


def seed_config(bot: Bot) -> bool:
    """Give a new bot its staggered schedule before it first starts. True when written."""
    if bot.config_file.exists() or not bot.dir.exists():
        return False
    config_store.write_config(bot.config_file, default_schedule(bot.index))
    return True


def schedule_text(values: dict[str, str]) -> str:
    hours = values.get("SCHEDULER_HOURS", "12").strip() or "12"
    times = values.get("SCHEDULER_FIXED_TIMES", "").strip()
    tz = values.get("SCHEDULER_TIMEZONE", "UTC").strip() or "UTC"
    parts = []
    if times:
        parts.append(f"dagelijks om {times.replace(',', ', ')} ({tz})")
    if hours not in ("0", ""):
        parts.append(f"elke {hours} uur vanaf het opstarten")
    if values.get("RUN_ON_STARTUP", "true").lower() in ("1", "true", "yes"):
        parts.append("en direct bij opstarten")
    return " ".join(parts) or "niets ingepland"


def _minutes(time_text: str, tz: str) -> int | None:
    try:
        h, m = (int(p) for p in time_text.split(":"))
        local = datetime.now(ZoneInfo(tz)).replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    utc = local.astimezone(timezone.utc)
    return utc.hour * 60 + utc.minute


def schedule_conflicts(configs: dict[str, dict[str, str]], gap: int = 60) -> list[str]:
    """Dutch warnings for bots that may run at the same time (memory: each run starts Chrome)."""
    warnings = []
    slots: list[tuple[str, int]] = []
    for name, values in configs.items():
        if (values.get("SCHEDULER_HOURS", "12").strip() or "12") != "0":
            warnings.append(f"{name} draait op een interval (elke zoveel uur): dat kan samenvallen met een "
                            "andere bot. Vaste tijden zijn veiliger.")
        tz = values.get("SCHEDULER_TIMEZONE", "UTC") or "UTC"
        for t in filter(None, (p.strip() for p in values.get("SCHEDULER_FIXED_TIMES", "").split(","))):
            minute = _minutes(t, tz)
            if minute is not None:
                slots.append((name, minute))
    for i, (a, ma) in enumerate(slots):
        for b, mb in slots[i + 1:]:
            if a != b and min(abs(ma - mb), 1440 - abs(ma - mb)) < gap:
                warnings.append(f"{a} en {b} hebben een run binnen een uur van elkaar.")
    return list(dict.fromkeys(warnings))


# ------------------------------------------------------------------ status

def status(bot: Bot) -> dict:
    raw = protocol.read_json(bot.webui_dir / protocol.STATUS_FILE) or {}
    beat = parse_time(raw.get("heartbeat"))
    age = (datetime.now(timezone.utc) - beat).total_seconds() if beat else None
    alive = age is not None and age < ALIVE_SECONDS
    last = raw.get("last_run") if isinstance(raw.get("last_run"), dict) else None
    if not alive:
        state, label = "off", "Niet bereikbaar"
    elif raw.get("restarting"):
        state, label = "busy", "Herstart…"
    elif raw.get("running"):
        state, label = "busy", "Bezig met een run"
    elif raw.get("queued"):
        state, label = "busy", "In de wachtrij (een andere bot is bezig)"
    elif (bot.webui_dir / protocol.RUN_REQUEST).exists():
        state, label = "busy", "Verzoek verstuurd…"
    else:
        state, label = "ok", "Actief, wacht op de volgende run"
    return {
        "alive": alive, "state": state, "label": label,
        "running": bool(alive and raw.get("running")),
        "queued": bool(alive and raw.get("queued")),
        "run_started": fmt_time(raw.get("run_started")),
        "last_run": last,
        "last_started": fmt_time(last.get("started")) if last else "–",
        "last_finished": fmt_time(last.get("finished")) if last else "–",
        "next_run": fmt_time(raw.get("next_run")) if alive else "–",
        "version": raw.get("version", ""),
        "restart_pending": bool(raw.get("restart_pending")) or (bot.webui_dir / protocol.RESTART_REQUEST).exists(),
        "run_pending": (bot.webui_dir / protocol.RUN_REQUEST).exists(),
        "message": raw.get("message", ""),
        "heartbeat": fmt_time(raw.get("heartbeat")),
    }


def env_value(bot: Bot, key: str) -> str:
    """One value from the bot's Docker env file (e.g. its VNC password), for its own user only."""
    if not bot.env_file or not bot.env_file.exists():
        return ""
    try:
        from dotenv import dotenv_values
        return dotenv_values(bot.env_file, interpolate=False).get(key) or ""
    except OSError:
        return ""


def request_run(bot: Bot) -> None:
    protocol.write_json(bot.webui_dir / protocol.RUN_REQUEST, {"at": datetime.now(timezone.utc).isoformat()})


def request_restart(bot: Bot, run_after: bool = False) -> None:
    protocol.write_json(bot.webui_dir / protocol.RESTART_REQUEST,
                        {"at": datetime.now(timezone.utc).isoformat(), "run_after": run_after})


# ------------------------------------------------------------------ games, log

STATUS_LABELS = (("claimed", "Geclaimd", "ok"), ("existed", "Had je al", "muted"),
                 ("already", "Had je al", "muted"), ("failed", "Mislukt", "bad"),
                 ("skipped", "Overgeslagen", "muted"), ("free_to_play", "Free-to-play", "muted"),
                 ("notified", "Gemeld", "muted"))
STORE_NAMES = {s.key: s.name for s in schema.STORES}


def status_label(status: str) -> tuple[str, str]:
    s = (status or "").lower()
    for prefix, label, tone in STATUS_LABELS:
        if s.startswith(prefix):
            return label, tone
    return status or "?", "muted"


def games(bot: Bot, limit: int = 500) -> list[dict]:
    db = bot.dir / "fgc.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT store, title, url, status, created_at, updated_at FROM claimed_games "
                "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for store, title, url, st, created, updated in rows:
        label, tone = status_label(st)
        out.append({"store": store, "store_name": STORE_NAMES.get(store, store), "title": title,
                    "url": url if (url or "").startswith(("https://", "http://")) else None,
                    "status": st, "label": label, "tone": tone,
                    "when": fmt_time(updated or created), "raw_when": parse_time(updated or created)})
    return out


def run_failures(bot: Bot, st: dict, all_games: list[dict]) -> list[dict]:
    """Problems from the last finished run: logged warnings/errors plus failed games in that window."""
    last = st.get("last_run") or {}
    out = [{"kind": p.get("level", ""), "text": p.get("message", "")} for p in last.get("problems") or []]
    start, end = parse_time(last.get("started")), parse_time(last.get("finished"))
    if start and end:
        for g in all_games:
            when = g["raw_when"]
            if g["tone"] == "bad" and when and start <= when <= end:
                out.append({"kind": "MISLUKT", "text": f"{g['store_name']}: {g['title']} ({g['status']})"})
    return out


def log_tail(bot: Bot, lines: int = 200) -> list[str]:
    path = bot.webui_dir / protocol.LOG_FILE
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=lines))
    except OSError:
        return []
