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

# The same sane default for every new bot: the job queue decides who runs first.
DEFAULT_SCHEDULE = {"SCHEDULER_HOURS": "0", "SCHEDULER_FIXED_TIMES": "06:00,18:00",
                    "SCHEDULER_TIMEZONE": "Europe/Amsterdam", "RUN_ON_STARTUP": "false"}


def seed_config(bot: Bot) -> bool:
    """Give a new bot the default schedule before it first starts. True when written."""
    if bot.config_file.exists() or not bot.dir.exists():
        return False
    config_store.write_config(bot.config_file, dict(DEFAULT_SCHEDULE))
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


# ------------------------------------------------------------------ status

def fmt_epoch(value) -> str:
    if not value:
        return "–"
    return datetime.fromtimestamp(float(value), timezone.utc).astimezone(display_tz()).strftime("%d-%m-%Y %H:%M")


def minutes_since(value) -> int:
    return max(0, int((datetime.now(timezone.utc).timestamp() - float(value or 0)) // 60))


def status(bot: Bot, queue_state: dict | None = None) -> dict:
    """Bot status for the badge: the queue says running/queued, status.json says alive."""
    raw = protocol.read_json(bot.webui_dir / protocol.STATUS_FILE) or {}
    beat = parse_time(raw.get("heartbeat"))
    age = (datetime.now(timezone.utc) - beat).total_seconds() if beat else None
    alive = age is not None and age < ALIVE_SECONDS
    last = raw.get("last_run") if isinstance(raw.get("last_run"), dict) else None
    q = queue_state or {}
    running, queued = q.get("running"), q.get("queued")
    if running:
        state, label = "busy", f"Bezig met een run ({minutes_since(running['started_at'])} min)"
    elif not alive:
        state, label = "off", "Niet bereikbaar"
    elif raw.get("restarting"):
        state, label = "busy", "Herstart…"
    elif queued:
        state, label = "busy", (f"In de wachtrij (plek {queued['position']})" if queued.get("position")
                                else "In de wachtrij")
    else:
        state, label = "ok", "Actief, wacht op de volgende run"
    return {
        "alive": alive, "state": state, "label": label,
        "running": bool(running), "queued": bool(queued),
        "job": running or queued,
        "last_run": last,
        "last_finished": fmt_time(last.get("finished")) if last else "–",
        "next_run": fmt_time(raw.get("next_run")) if alive else "–",
        "version": raw.get("version", ""),
        "restart_pending": bool(raw.get("restart_pending")) or (bot.webui_dir / protocol.RESTART_REQUEST).exists(),
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


def request_restart(bot: Bot) -> None:
    protocol.write_json(bot.webui_dir / protocol.RESTART_REQUEST, {"at": datetime.now(timezone.utc).isoformat()})


JOB_LABELS = {"queued": ("In de wachtrij", "muted"), "running": ("Bezig", "busy"), "done": ("Klaar", "ok"),
              "failed": ("Mislukt", "bad"), "timeout": ("Te lang bezig", "bad"),
              "cancelled": ("Geannuleerd", "muted")}
SOURCE_LABELS = {"scheduled": "planning", "startup": "bij opstarten", "manual": "handmatig"}


def job_view(job: dict) -> dict:
    """A job from the queue, ready for a template."""
    label, tone = JOB_LABELS.get(job["status"], (job["status"], "muted"))
    result = job.get("result") or {}
    end = job.get("finished_at")
    dur = int((end - job["started_at"]) // 60) if end and job.get("started_at") else None
    return {**job, "label": label, "tone": tone, "source_label": SOURCE_LABELS.get(job["source"], job["source"]),
            "created": fmt_epoch(job["created_at"]), "started": fmt_epoch(job.get("started_at")),
            "finished": fmt_epoch(end), "duration": dur,
            "elapsed": minutes_since(job["started_at"]) if job["status"] == "running" else None,
            "claimed": result.get("claimed"), "failed_games": result.get("failed"),
            "problems": result.get("problems") or []}


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
