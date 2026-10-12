"""Bot side of the web interface (fork-only, runs inside the bot container).

main.py calls attach() once, right after the scheduler has started. From then on
a small background task:

  * copies the log to <data>/webui/bot.log, so the web interface can show it;
  * writes <data>/webui/status.json: alive, running, last run, next run;
  * turns the bot's own schedule into jobs on the shared queue (webui/jobqueue.py)
    and runs a claim run only after it has claimed a job there, so at most
    FGC_WORKERS runs happen at once across all bots, first come first served;
  * keeps a heartbeat on its running job and ends a run that hangs;
  * picks up "settings changed" requests and restarts the bot between runs, so
    the new data/config.env is read. The process exits and Docker's restart
    policy (unless-stopped) starts the container again.

Nothing in here may ever break the bot: every step is wrapped, and a failure
only means the web interface shows less.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from webui import jobqueue, protocol

logger = logging.getLogger("fgc.webui")

POLL_SECONDS = 3
HEARTBEAT_SECONDS = 15
PRUNE_SECONDS = 3600
STARTUP_JOB_ID = "claim_all_startup"
MAX_PROBLEMS = 50
# Secrets never reach the log on purpose, this only catches what slips through by accident.
_SECRETISH = re.compile(r"(?i)(password|passwd|otp|token|secret|webhook)(\s*[=:]\s*)(\S+)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plain(record: logging.LogRecord) -> str:
    """The message as text: rich markup removed, obvious secrets masked."""
    try:
        msg = record.getMessage()
    except Exception:  # a broken format string must not take the log down
        msg = str(record.msg)
    try:
        from rich.text import Text
        msg = Text.from_markup(msg).plain
    except Exception:
        msg = msg.replace("\\[", "[")
    return _SECRETISH.sub(r"\1\2***", msg)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} {record.levelname:<7} {plain(record)}"
        if record.exc_info:
            # Only the last line of a traceback: the full one stays in `docker logs`.
            last = self.formatException(record.exc_info).strip().splitlines()[-1:]
            text += "".join(f" | {line}" for line in last)
        return text


# DLC whose base game you do not own cannot be claimed; that is not a problem of the run.
MISSING_BASE_MESSAGES = ("required base game is missing", "base game is not free or could not be added",
                         "requires base game")


def is_missing_base(status: str) -> bool:
    return "missing_base" in (status or "").lower()


class ProblemCollector(logging.Handler):
    """Remembers warnings and errors while a run is in progress (not DLC without its base game)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.active = False
        self.items: deque = deque(maxlen=MAX_PROBLEMS)

    def emit(self, record: logging.LogRecord) -> None:
        if self.active:
            try:
                if any(m in record.getMessage().lower() for m in MISSING_BASE_MESSAGES):
                    return
                self.items.append({"level": record.levelname, "message": plain(record)[:500]})
            except Exception:
                pass


class Bridge:
    def __init__(self, scheduler, run_job, lock, data_dir: Path, version: str = "", queue=None) -> None:
        self.scheduler = scheduler
        self.run_job = run_job
        self.lock = lock
        self.data_dir = Path(data_dir)
        self.dir = protocol.webui_dir(self.data_dir)
        self.version = version
        self.started_at = _now()
        self.collector = ProblemCollector()
        self.was_running = False
        self.run_started: str | None = None
        self.last_run: dict | None = None
        self.restart_pending = False
        self.bot_name = os.environ.get("FGC_BOT_NAME", "") or "bot"
        # The shared job queue; None means a single upstream-style bot that runs on its own schedule.
        self.queue = queue
        self.workers = jobqueue.workers_from_env()
        self.timeout = _minutes_env("FGC_RUN_TIMEOUT_MINUTES", 180) * 60
        self.current: dict | None = None
        self.current_started: float | None = None
        self.task = None
        self.last_beat = 0.0
        self.last_prune = 0.0

    # ----- setup -----

    def setup(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        protocol.match_owner(self.dir, self.dir.parent)
        root = logging.getLogger()
        handler = RotatingFileHandler(self.dir / protocol.LOG_FILE, maxBytes=1_000_000,
                                      backupCount=2, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(PlainFormatter())
        root.addHandler(handler)
        root.addHandler(self.collector)
        previous = protocol.read_json(self.dir / protocol.STATUS_FILE) or {}
        if isinstance(previous.get("last_run"), dict):
            self.last_run = previous["last_run"]
        # Left behind by our own restart (new settings, stuck run): no extra startup run. A run
        # wanted right after saving is a job the web interface already put in the queue.
        note = protocol.take_request(self.dir / protocol.RESTART_NOTE)
        if note is not None:
            try:
                if self.scheduler.get_job(STARTUP_JOB_ID):
                    self.scheduler.remove_job(STARTUP_JOB_ID)
                    logger.info("Restarted by the web interface, skipping the startup run.")
            except Exception:
                logger.debug("Could not skip the startup run", exc_info=True)
        if self.queue:
            # Whatever the list says, this process has no run in progress.
            if self.queue.abandon_running(self.bot_name, "de bot is herstart tijdens de run"):
                logger.warning("A run of this bot was cut off by a restart, it is marked as failed.")
            self.queue.bot_heartbeat(self.bot_name)

    def wrap_jobs(self) -> None:
        """With a queue, the bot's own schedule only adds a job to the queue."""
        if not self.queue:
            return
        for job in self.scheduler.get_jobs():
            source = "startup" if job.id == STARTUP_JOB_ID else "scheduled"
            try:
                self.scheduler.modify_job(job.id, func=self.enqueue, args=(source,))
            except Exception:
                logger.debug("Could not route job %s through the queue", job.id, exc_info=True)

    # ----- the queue -----

    def enqueue(self, source: str = "scheduled") -> None:
        try:
            job, created = self.queue.enqueue(self.bot_name, source=source)
        except Exception:
            logger.warning("Could not add a run to the queue.", exc_info=True)
            return
        if created:
            logger.info("Run added to the queue (job %s, %s).", job["id"], source)
        else:
            logger.info("This bot already has a run waiting in the queue (job %s), not adding another.", job["id"])

    def claim(self) -> None:
        if self.current or self.restart_pending or self.lock.locked():
            return
        job = self.queue.claim(self.bot_name, self.workers, self.timeout)
        if job:
            self.current = job
            self.current_started = time.monotonic()
            logger.info("Job %s: our turn in the queue, starting the claiming run.", job["id"])
            self.task = asyncio.get_running_loop().create_task(self.execute(job))

    async def execute(self, job: dict) -> None:
        began = datetime.now(timezone.utc)
        status, error = jobqueue.DONE, None
        try:
            if job.get("params"):
                logger.info("Job %s asks for %s; a partial run is not supported yet, running everything.",
                            job["id"], job["params"])
            await self.run_job()
        except Exception as exc:  # run_claimers catches store crashes itself, this is the rest
            logger.exception("Job %s failed", job["id"])
            status, error = jobqueue.FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            result = self.summary(began)
            try:
                self.queue.finish(job["id"], status, result, error)
            except Exception:
                logger.warning("Could not mark job %s as finished.", job["id"], exc_info=True)
            self.current = None
            self.current_started = None

    def summary(self, began: datetime) -> dict:
        """Short result for the job list: games claimed and failed in this run, and logged problems."""
        out = {"claimed": 0, "failed": 0, "problems": [p["message"][:200] for p in list(self.collector.items)[:20]]}
        db = self.data_dir / "fgc.db"
        since = began.strftime("%Y-%m-%d %H:%M:%S")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                for st, n in con.execute("SELECT status, COUNT(*) FROM claimed_games "
                                         "WHERE COALESCE(updated_at, created_at) >= ? GROUP BY status", (since,)):
                    st = (st or "").lower()
                    if st.startswith("claimed"):
                        out["claimed"] += n
                    elif st.startswith("failed") and not is_missing_base(st):
                        out["failed"] += n
            finally:
                con.close()
        except sqlite3.Error:
            pass
        return out

    def check_timeout(self) -> None:
        if self.current and self.current_started and time.monotonic() - self.current_started > self.timeout:
            logger.error("Job %s has taken more than %d minutes and seems stuck, restarting the bot.",
                         self.current["id"], self.timeout // 60)
            try:
                self.queue.finish(self.current["id"], jobqueue.TIMEOUT, self.summary(datetime.now(timezone.utc)),
                                  f"duurde langer dan {self.timeout // 60} minuten")
            except Exception:
                pass
            self.restart({})

    def queue_round(self) -> None:
        now = time.monotonic()
        if now - self.last_beat >= HEARTBEAT_SECONDS or not self.last_beat:
            self.queue.bot_heartbeat(self.bot_name)
            if self.current:
                self.queue.heartbeat(self.current["id"])
            self.last_beat = now
        self.check_timeout()
        self.claim()
        if now - self.last_prune >= PRUNE_SECONDS:
            self.last_prune = now
            self.queue.recover(self.timeout)
            self.queue.prune()

    # ----- status -----

    def next_run(self) -> str | None:
        try:
            times = [job.next_run_time for job in self.scheduler.get_jobs() if job.next_run_time]
        except Exception:
            return None
        return min(times).astimezone(timezone.utc).isoformat(timespec="seconds") if times else None

    def track_run(self) -> None:
        running = self.lock.locked()
        if running and not self.was_running:
            self.run_started = _now()
            self.collector.items.clear()
            self.collector.active = True
        elif not running and self.was_running:
            self.collector.active = False
            self.last_run = {"started": self.run_started, "finished": _now(),
                             "problems": list(self.collector.items)}
            self.run_started = None
        self.was_running = running

    def wants_restart(self) -> dict | None:
        path = self.dir / protocol.RESTART_REQUEST
        if not path.exists():
            self.restart_pending = False
            return None
        self.restart_pending = True  # no new job is claimed meanwhile
        if self.lock.locked() or self.current:
            return None  # never in the middle of a run: wait until it is done
        return protocol.take_request(path)

    def status(self) -> dict:
        return {
            "heartbeat": _now(),
            "pid": os.getpid(),
            "bot": self.bot_name,
            "started_at": self.started_at,
            "version": self.version,
            "running": self.lock.locked(),
            "job": self.current["id"] if self.current else None,
            "queue": self.queue is not None,
            "workers": self.workers,
            "run_started": self.run_started,
            "last_run": self.last_run,
            "next_run": self.next_run(),
            "restart_pending": self.restart_pending,
        }

    def write_status(self, **extra) -> None:
        protocol.write_json(self.dir / protocol.STATUS_FILE, {**self.status(), **extra})

    def poll(self) -> dict | None:
        """One round. Returns the restart request when the bot should restart now."""
        self.track_run()
        restart = self.wants_restart()
        if self.queue:
            try:
                self.queue_round()
            except Exception:
                logger.debug("Queue round failed", exc_info=True)
        self.write_status(restarting=restart is not None)
        return restart

    # ----- loop -----

    async def loop(self) -> None:
        while True:
            try:
                restart = self.poll()
                if restart is not None:
                    self.restart(restart)
            except Exception:
                logger.debug("Web interface bridge round failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    def restart(self, request: dict) -> None:
        protocol.write_json(self.dir / protocol.RESTART_NOTE, {"at": _now()})
        logger.info("Restarting the bot (new settings or a stuck run).")
        logging.shutdown()
        # Docker's restart policy (unless-stopped) brings the container back with the new config.env.
        os._exit(0)


def _minutes_env(name: str, default: int) -> int:
    try:
        return max(5, int(os.environ.get(name, default)))
    except ValueError:
        return default


_bridge: Bridge | None = None
_task = None


def attach(scheduler, run_job, lock) -> None:
    """Hook called from main.py once the scheduler runs. Never raises."""
    global _bridge, _task
    if os.environ.get("WEBUI_BRIDGE", "true").strip().lower() in ("0", "false", "no"):
        return
    try:
        from src.core.config import cfg
        from src.version import __version__
        # No message about DLC without its base game unless the user asks for it (upstream default: on).
        if not (os.environ.get("NOTIFY_MISSING_BASE") or "").strip():
            cfg.notify_missing_base = False
        queue = None
        try:
            queue = jobqueue.open_queue(os.environ.get("FGC_QUEUE_DIR", "").strip())
        except Exception:
            logger.warning("The shared job queue could not be opened, this bot runs on its own schedule.",
                           exc_info=True)
        _bridge = Bridge(scheduler, run_job, lock, cfg._data_dir, __version__, queue)
        _bridge.setup()
        _bridge.wrap_jobs()
        _bridge.write_status(restarting=False)
        _task = asyncio.get_running_loop().create_task(_bridge.loop())
    except Exception:
        logger.warning("Web interface bridge could not start, the bot runs on without it.", exc_info=True)
