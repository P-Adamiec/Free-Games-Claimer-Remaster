"""The shared job queue ("Takenlijst"): which bot may run a claim run, and when.

One SQLite file on a volume every bot and the web interface mount (fgc_queue).
Standard library only, because the bots import it too.

  * A bot's own schedule and "Nu uitvoeren" only ENQUEUE a job; a bot never has
    more than one queued job (dedupe).
  * A bot starts a run only after it has CLAIMED a job. Claiming takes a write
    lock (BEGIN IMMEDIATE), so two bots can never take the last free slot.
  * At most `workers` jobs run at once (FGC_WORKERS, "auto" = from memory).
  * Strict FIFO across bots: the oldest queued job of a live bot goes first.
  * Running jobs carry a heartbeat. A job whose bot stopped heartbeating, or
    that ran longer than the timeout, is marked failed/timeout and frees its slot.

Statuses: queued -> running -> done | failed | timeout, or queued -> cancelled.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_NAME = "jobs.db"
QUEUED, RUNNING, DONE, FAILED, TIMEOUT, CANCELLED = "queued", "running", "done", "failed", "timeout", "cancelled"
ACTIVE = (QUEUED, RUNNING)
FINISHED = (DONE, FAILED, TIMEOUT, CANCELLED)
TYPES = ("run",)                # "run" with params {"stores": [...]} is reserved for a partial run later
SOURCES = ("scheduled", "startup", "manual")

STALE_SECONDS = 120             # a running job without a heartbeat this long: its bot is gone
BOT_ALIVE_SECONDS = 60          # a bot without a heartbeat this long does not block the FIFO head
QUEUED_EXPIRE_SECONDS = 24 * 3600
KEEP_DAYS = 30
KEEP_JOBS = 1000
GB = 1024 ** 3
RUN_MEMORY = 1.5 * GB           # what one claim run (Chrome) needs, roughly
RESERVE_MEMORY = 1 * GB         # left for the system, the web interface and idle bots
MAX_AUTO_WORKERS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bot          TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT 'run',
    params       TEXT,
    source       TEXT NOT NULL,
    requested_by TEXT,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    heartbeat_at REAL,
    result       TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS jobs_bot ON jobs(bot, id);
CREATE TABLE IF NOT EXISTS bots (
    name         TEXT PRIMARY KEY,
    heartbeat_at REAL NOT NULL
);
"""


# ------------------------------------------------------------------ workers

def memory_bytes() -> int | None:
    """Memory this container may use: the cgroup limit if there is one, else MemTotal."""
    limit = None
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:
            limit = int(raw)
            break
    total = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    values = [v for v in (limit, total) if v]
    return min(values) if values else None


def auto_workers(mem: int | None) -> int:
    """floor((memory - 1 GB) / 1.5 GB), at least 1 and at most 4."""
    if not mem:
        return 1
    return max(1, min(MAX_AUTO_WORKERS, math.floor((mem - RESERVE_MEMORY) / RUN_MEMORY)))


def workers_from_env(environ=os.environ) -> int:
    raw = (environ.get("FGC_WORKERS") or "1").strip().lower()
    if raw == "auto":
        return auto_workers(memory_bytes())
    try:
        return max(1, min(16, int(raw)))
    except ValueError:
        return 1


# ------------------------------------------------------------------ the queue

class JobQueue:
    def __init__(self, folder: Path, clock=time.time) -> None:
        self.path = Path(folder) / DB_NAME
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    @contextmanager
    def _write(self):
        """One write transaction that holds the database lock from the start (BEGIN IMMEDIATE)."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    def _read(self, sql: str, args=()) -> list[dict]:
        con = self._connect()
        try:
            return [_row(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    # ----- bots -----

    def bot_heartbeat(self, bot: str) -> None:
        with self._write() as con:
            con.execute("INSERT INTO bots(name, heartbeat_at) VALUES(?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET heartbeat_at = excluded.heartbeat_at", (bot, self.clock()))

    def bots_alive(self) -> dict[str, bool]:
        now = self.clock()
        return {r["name"]: now - r["heartbeat_at"] < BOT_ALIVE_SECONDS
                for r in self._read("SELECT name, heartbeat_at FROM bots")}

    # ----- enqueue / cancel -----

    def enqueue(self, bot: str, source: str = "manual", job_type: str = "run", params: dict | None = None,
                requested_by: str | None = None) -> tuple[dict, bool]:
        """Add a job, unless this bot already has one waiting. Returns (job, created)."""
        if job_type not in TYPES:
            raise ValueError(f"unknown job type {job_type!r}")
        with self._write() as con:
            existing = con.execute("SELECT * FROM jobs WHERE bot = ? AND type = ? AND status = ? "
                                   "ORDER BY id LIMIT 1", (bot, job_type, QUEUED)).fetchone()
            if existing:
                return _row(existing), False
            cur = con.execute(
                "INSERT INTO jobs(bot, type, params, source, requested_by, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (bot, job_type, json.dumps(params) if params else None, source, requested_by, QUEUED, self.clock()))
            return _row(con.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()), True

    def cancel(self, job_id: int, by: str | None = None) -> bool:
        """Cancel a job that has not started. A running job cannot be cancelled here."""
        with self._write() as con:
            cur = con.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE id = ? AND status = ?",
                              (CANCELLED, self.clock(), f"geannuleerd door {by}" if by else "geannuleerd",
                               job_id, QUEUED))
            return cur.rowcount == 1

    # ----- claim / heartbeat / finish (bot side) -----

    def claim(self, bot: str, workers: int, timeout: float) -> dict | None:
        """Take this bot's turn if a slot is free and its job is first in line. Atomic."""
        with self._write() as con:
            self._recover(con, timeout)
            now = self.clock()
            if con.execute("SELECT 1 FROM jobs WHERE bot = ? AND status = ?", (bot, RUNNING)).fetchone():
                return None
            running = con.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (RUNNING,)).fetchone()[0]
            if running >= max(1, workers):
                return None
            alive = {r[0] for r in con.execute("SELECT name FROM bots WHERE heartbeat_at > ?",
                                               (now - BOT_ALIVE_SECONDS,))} | {bot}
            busy = {r[0] for r in con.execute("SELECT bot FROM jobs WHERE status = ?", (RUNNING,))}
            # First in line: the oldest queued job of a live bot that is not running already.
            for row in con.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (QUEUED,)).fetchall():
                if row["bot"] in alive and row["bot"] not in busy:
                    if row["bot"] != bot:
                        return None
                    con.execute("UPDATE jobs SET status = ?, started_at = ?, heartbeat_at = ? WHERE id = ?",
                                (RUNNING, now, now, row["id"]))
                    return _row(con.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
            return None

    def heartbeat(self, job_id: int) -> None:
        with self._write() as con:
            con.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = ?",
                        (self.clock(), job_id, RUNNING))

    def finish(self, job_id: int, status: str = DONE, result: dict | None = None, error: str | None = None) -> None:
        if status not in FINISHED:
            raise ValueError(status)
        with self._write() as con:
            con.execute("UPDATE jobs SET status = ?, finished_at = ?, result = ?, error = ? "
                        "WHERE id = ? AND status = ?",
                        (status, self.clock(), json.dumps(result) if result else None,
                         (error or None) and error[:500], job_id, RUNNING))

    def abandon_running(self, bot: str, reason: str) -> int:
        """A bot that (re)starts has no run in progress, whatever the list says."""
        with self._write() as con:
            return con.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE bot = ? AND status = ?",
                               (FAILED, self.clock(), reason, bot, RUNNING)).rowcount

    # ----- housekeeping -----

    def recover(self, timeout: float) -> int:
        with self._write() as con:
            return self._recover(con, timeout)

    def _recover(self, con, timeout: float) -> int:
        now = self.clock()
        n = con.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? "
                        "WHERE status = ? AND started_at < ?",
                        (TIMEOUT, now, f"duurde langer dan {int(timeout // 60)} minuten", RUNNING,
                         now - timeout)).rowcount
        n += con.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? "
                         "WHERE status = ? AND heartbeat_at < ?",
                         (FAILED, now, "de bot reageerde niet meer tijdens de run", RUNNING,
                          now - STALE_SECONDS)).rowcount
        n += con.execute("UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE status = ? AND created_at < ?",
                         (CANCELLED, now, "verlopen: de bot was niet bereikbaar", QUEUED,
                          now - QUEUED_EXPIRE_SECONDS)).rowcount
        return n

    def prune(self, days: int = KEEP_DAYS, keep: int = KEEP_JOBS) -> int:
        with self._write() as con:
            n = con.execute("DELETE FROM jobs WHERE status IN (?, ?, ?, ?) AND finished_at < ?",
                            (*FINISHED, self.clock() - days * 86400)).rowcount
            n += con.execute("DELETE FROM jobs WHERE status IN (?, ?, ?, ?) AND id NOT IN "
                             "(SELECT id FROM jobs ORDER BY id DESC LIMIT ?)", (*FINISHED, keep)).rowcount
            return n

    # ----- reading -----

    def get(self, job_id: int) -> dict | None:
        rows = self._read("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return rows[0] if rows else None

    def active(self) -> list[dict]:
        """Running jobs first, then the queue in FIFO order, each queued job with its position."""
        rows = self._read("SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY status = 'queued', id", ACTIVE)
        pos = 0
        for r in rows:
            if r["status"] == QUEUED:
                pos += 1
                r["position"] = pos
        return rows

    def history(self, bot: str | None = None, limit: int = 100) -> list[dict]:
        if bot:
            return self._read("SELECT * FROM jobs WHERE bot = ? AND status IN (?, ?, ?, ?) "
                              "ORDER BY id DESC LIMIT ?", (bot, *FINISHED, limit))
        return self._read("SELECT * FROM jobs WHERE status IN (?, ?, ?, ?) ORDER BY id DESC LIMIT ?",
                          (*FINISHED, limit))

    def bot_state(self, bot: str) -> dict:
        """What the overview badge needs: this bot's running or queued job, and its place in line."""
        active = self.active()
        mine = [j for j in active if j["bot"] == bot]
        running = next((j for j in mine if j["status"] == RUNNING), None)
        queued = next((j for j in mine if j["status"] == QUEUED), None)
        last = self.history(bot, 1)
        return {"running": running, "queued": queued, "last": last[0] if last else None,
                "running_total": sum(1 for j in active if j["status"] == RUNNING)}


def _row(r) -> dict:
    d = dict(r)
    for key in ("params", "result"):
        try:
            d[key] = json.loads(d[key]) if d.get(key) else None
        except (TypeError, ValueError):
            d[key] = None
    return d


def open_queue(folder: str | Path | None) -> JobQueue | None:
    """The queue in this folder, or None when no queue is configured (a single upstream bot)."""
    if not folder:
        return None
    return JobQueue(Path(folder))
