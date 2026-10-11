"""Bot side of the web interface (fork-only, runs inside the bot container).

main.py calls attach() once, right after the scheduler has started. From then on
a small background task:

  * copies the log to <data>/webui/bot.log, so the web interface can show it;
  * writes <data>/webui/status.json: alive, running, last run, next run;
  * makes every run wait for the shared run lock (one claim run at a time across
    all bots, see run_slot()), with a time limit for runs that hang;
  * picks up "run now" requests and queues a run through the same path;
  * picks up "settings changed" requests and restarts the bot between runs, so
    the new data/config.env is read. The process exits and Docker's restart
    policy (unless-stopped) starts the container again.

Nothing in here may ever break the bot: every step is wrapped, and a failure
only means the web interface shows less.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from webui import protocol

logger = logging.getLogger("fgc.webui")

POLL_SECONDS = 2
WEBUI_JOB_ID = "claim_webui"
STARTUP_JOB_ID = "claim_all_startup"
MAX_PROBLEMS = 50
LOCK_POLL_SECONDS = 5
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


class ProblemCollector(logging.Handler):
    """Remembers warnings and errors while a run is in progress."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.active = False
        self.items: deque = deque(maxlen=MAX_PROBLEMS)

    def emit(self, record: logging.LogRecord) -> None:
        if self.active:
            try:
                self.items.append({"level": record.levelname, "message": plain(record)[:500]})
            except Exception:
                pass


class Bridge:
    def __init__(self, scheduler, run_job, lock, data_dir: Path, version: str = "") -> None:
        self.scheduler = scheduler
        self.run_job = run_job
        self.lock = lock
        self.dir = protocol.webui_dir(Path(data_dir))
        self.version = version
        self.started_at = _now()
        self.collector = ProblemCollector()
        self.was_running = False
        self.run_started: str | None = None
        self.last_run: dict | None = None
        self.restart_pending = False
        self.message = ""
        self.queued_since: str | None = None
        self.run_started_at: float | None = None
        self.bot_name = os.environ.get("FGC_BOT_NAME", "")
        # Shared folder for the run lock; unset means a single bot, no queue.
        lock_dir = os.environ.get("FGC_RUN_LOCK_DIR", "").strip()
        self.lock_file = Path(lock_dir) / "run.lock" if lock_dir else None
        self.timeout = _minutes_env("FGC_RUN_TIMEOUT_MINUTES", 180) * 60

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
        # Left behind by our own restart for new settings: honour "no run right after saving".
        note = protocol.take_request(self.dir / protocol.RESTART_NOTE)
        if note is not None and not note.get("run_after", False):
            try:
                if self.scheduler.get_job(STARTUP_JOB_ID):
                    self.scheduler.remove_job(STARTUP_JOB_ID)
                    logger.info("Restarted for new settings from the web interface, skipping the startup run.")
            except Exception:
                logger.debug("Could not skip the startup run", exc_info=True)
        elif note is not None:
            logger.info("Restarted for new settings from the web interface.")

    def wrap_jobs(self) -> None:
        """Send every claim job the scheduler already has through the queue."""
        for job in self.scheduler.get_jobs():
            try:
                self.scheduler.modify_job(job.id, func=self.queued_run)
            except Exception:
                logger.debug("Could not route job %s through the run queue", job.id, exc_info=True)

    # ----- the queue: one claim run at a time across all bots -----

    async def run_slot(self):
        """Wait for the shared lock. Returns an open file (the lock), or None without a queue.

        flock is held by the kernel for this process: a crashed or killed bot releases
        it automatically, so a lock can never go stale. A run that hangs is ended by
        check_timeout(), which restarts the bot and so releases the lock as well.
        """
        if self.lock_file is None:
            return None
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_file, "a+")
        except PermissionError:
            # A bot running as PUID may not write the shared folder; flock works on a read-only file too.
            fh = open(self.lock_file, "r")
        waited = 0.0
        announced = False
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(f"{self.bot_name or os.getpid()} {_now()}\n")
                    fh.flush()
                except OSError:
                    pass  # who holds the lock is only informative
                return fh
            except BlockingIOError:
                if not announced:
                    logger.info("Another bot is claiming right now, this run waits its turn.")
                    announced = True
            if waited >= self.timeout:
                fh.close()
                raise TimeoutError
            await asyncio.sleep(LOCK_POLL_SECONDS)
            waited += LOCK_POLL_SECONDS

    async def queued_run(self) -> None:
        """What every scheduled run and every "run now" goes through."""
        if self.queued_since or self.lock.locked():
            logger.info("A run is already queued or in progress, skipping this trigger.")
            return
        self.queued_since = _now()
        self.write_status_safe()
        try:
            slot = await self.run_slot()
        except TimeoutError:
            logger.warning("Waited %d minutes for the other bots without getting a turn, skipping this run.",
                           self.timeout // 60)
            return
        except Exception:
            logger.warning("Run queue unavailable, running without waiting.", exc_info=True)
            slot = None
        finally:
            self.queued_since = None
        try:
            await self.run_job()
        finally:
            if slot is not None:
                try:
                    fcntl.flock(slot.fileno(), fcntl.LOCK_UN)
                finally:
                    slot.close()

    def check_timeout(self) -> None:
        started = self.run_started_at
        if self.lock.locked() and started and time.monotonic() - started > self.timeout:
            logger.error("This run has taken more than %d minutes and seems stuck, restarting the bot.",
                         self.timeout // 60)
            self.restart({"run_after": False})

    # ----- one poll -----

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
            self.run_started_at = time.monotonic()
            self.collector.items.clear()
            self.collector.active = True
        elif not running and self.was_running:
            self.collector.active = False
            self.last_run = {"started": self.run_started, "finished": _now(),
                             "problems": list(self.collector.items)}
            self.run_started = None
            self.run_started_at = None
        self.was_running = running

    def handle_run_request(self) -> None:
        req = protocol.take_request(self.dir / protocol.RUN_REQUEST)
        if req is None:
            return
        if self.lock.locked() or self.queued_since:
            self.message = "Er loopt al een run of er staat er een in de wachtrij; het verzoek is overgeslagen."
            logger.info("Web interface asked for a run, but one is already queued or in progress.")
            return
        logger.info("Web interface asked for a run, queueing one now.")
        self.scheduler.add_job(self.queued_run, id=WEBUI_JOB_ID, name="Run requested from the web interface",
                               replace_existing=True)
        self.message = ""

    def wants_restart(self) -> dict | None:
        path = self.dir / protocol.RESTART_REQUEST
        if not path.exists():
            self.restart_pending = False
            return None
        self.restart_pending = True
        if self.lock.locked() or self.queued_since:
            return None  # never in the middle of a run (or its turn): wait until it is done
        return protocol.take_request(path)

    def status(self) -> dict:
        return {
            "heartbeat": _now(),
            "pid": os.getpid(),
            "started_at": self.started_at,
            "version": self.version,
            "running": self.lock.locked(),
            "queued": bool(self.queued_since) and not self.lock.locked(),
            "queued_since": self.queued_since,
            "run_queue": self.lock_file is not None,
            "run_started": self.run_started,
            "last_run": self.last_run,
            "next_run": self.next_run(),
            "restart_pending": self.restart_pending,
            "message": self.message,
        }

    def write_status(self, **extra) -> None:
        protocol.write_json(self.dir / protocol.STATUS_FILE, {**self.status(), **extra})

    def write_status_safe(self) -> None:
        try:
            self.write_status(restarting=False)
        except Exception:
            pass

    def poll(self) -> dict | None:
        """One round. Returns the restart request when the bot should restart now."""
        self.track_run()
        self.check_timeout()
        self.handle_run_request()
        restart = self.wants_restart()
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
        protocol.write_json(self.dir / protocol.RESTART_NOTE,
                            {"at": _now(), "run_after": bool(request.get("run_after", False))})
        logger.info("Settings were changed in the web interface, restarting the bot to load them.")
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
        _bridge = Bridge(scheduler, run_job, lock, cfg._data_dir, __version__)
        _bridge.setup()
        _bridge.wrap_jobs()
        _bridge.write_status(restarting=False)
        _task = asyncio.get_running_loop().create_task(_bridge.loop())
    except Exception:
        logger.warning("Web interface bridge could not start, the bot runs on without it.", exc_info=True)
