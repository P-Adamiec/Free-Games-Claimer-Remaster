"""[fork:webui] The bot side: status file, restart requests, and runs only through the job queue."""

import asyncio
import logging

import pytest

from webui import bridge, jobqueue, protocol


class FakeJob:
    def __init__(self, job_id, func, args=()):
        self.id, self.func, self.args, self.next_run_time = job_id, func, args, None


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, id, name="", replace_existing=False):
        self.jobs[id] = FakeJob(id, func)

    def remove_job(self, job_id):
        del self.jobs[job_id]

    def modify_job(self, job_id, func, args=()):
        self.jobs[job_id].func, self.jobs[job_id].args = func, args


def make(tmp_path, monkeypatch, name="alice", queue=None, workers="1", events=None, run_seconds=0.05):
    monkeypatch.setenv("FGC_BOT_NAME", name)
    monkeypatch.setenv("FGC_WORKERS", workers)
    sched, lock = FakeScheduler(), asyncio.Lock()

    async def run_job():
        async with lock:
            if events is not None:
                events.append(("start", name))
            await asyncio.sleep(run_seconds)
            if events is not None:
                events.append(("end", name))

    b = bridge.Bridge(sched, run_job, lock, tmp_path / name, queue=queue)
    b.dir.mkdir(parents=True)
    return b, sched, lock


class TestStatus:
    def test_status_file_is_written(self, tmp_path, monkeypatch):
        b, _, _ = make(tmp_path, monkeypatch)
        b.poll()
        data = protocol.read_json(b.dir / protocol.STATUS_FILE)
        assert data["running"] is False and data["heartbeat"] and data["bot"] == "alice"

    def test_log_lines_lose_markup_and_secrets(self):
        rec = logging.makeLogRecord({"msg": "\\[Steam] password=hunter2 ok"})
        assert bridge.plain(rec) == "[Steam] password=*** ok"


class TestMissingBaseGame:
    """DLC whose base game you do not own cannot be claimed: skipped, not a failure."""

    def test_its_warnings_are_not_problems_of_the_run(self):
        c = bridge.ProblemCollector()
        c.active = True
        for msg in ("Skipping DLC 'X' because required base game is missing.",
                    "Base game is not free or could not be added.", "'X' requires base game.", "Login failed"):
            c.emit(logging.makeLogRecord({"msg": msg, "levelno": logging.WARNING, "levelname": "WARNING"}))
        assert [p["message"] for p in c.items] == ["Login failed"]

    def test_it_does_not_count_as_failed(self, tmp_path, monkeypatch):
        import sqlite3
        from datetime import datetime, timedelta
        b, _, _ = make(tmp_path, monkeypatch)
        con = sqlite3.connect(b.data_dir / "fgc.db")
        con.execute("CREATE TABLE claimed_games (status TEXT, created_at TEXT, updated_at TEXT)")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.executemany("INSERT INTO claimed_games VALUES (?, ?, ?)",
                        [("claimed", now, now), ("failed:missing_base", now, now), ("failed", now, now)])
        con.commit()
        con.close()
        out = b.summary(datetime.now() - timedelta(minutes=1))
        assert out["claimed"] == 1 and out["failed"] == 1

    def test_the_web_interface_shows_it_as_no_base_game(self):
        from webui.app import botstate
        assert botstate.status_label("failed:missing_base") == ("Geen basisspel", "muted")
        assert botstate.status_label("failed") == ("Mislukt", "bad")


class TestRestart:
    def test_restart_waits_for_a_running_claim(self, tmp_path, monkeypatch):
        b, _, lock = make(tmp_path, monkeypatch)
        protocol.write_json(b.dir / protocol.RESTART_REQUEST, {})

        async def scenario():
            await lock.acquire()
            during = b.poll()
            lock.release()
            return during, b.poll()

        during, after = asyncio.run(scenario())
        assert during is None and after == {}

    def test_after_a_restart_by_us_the_startup_run_is_skipped(self, tmp_path, monkeypatch):
        b, sched, _ = make(tmp_path, monkeypatch)
        sched.add_job(lambda: None, id=bridge.STARTUP_JOB_ID)
        protocol.write_json(b.dir / protocol.RESTART_NOTE, {})
        b.setup()
        assert bridge.STARTUP_JOB_ID not in sched.jobs


class TestThroughTheQueue:
    def test_schedule_only_enqueues(self, tmp_path, monkeypatch):
        q = jobqueue.JobQueue(tmp_path / "q")
        b, sched, _ = make(tmp_path, monkeypatch, queue=q)
        sched.add_job(lambda: None, id="claim_all")
        sched.add_job(lambda: None, id=bridge.STARTUP_JOB_ID)
        b.wrap_jobs()
        job = sched.jobs["claim_all"]
        job.func(*job.args)
        job.func(*job.args)                           # deduped
        sched.jobs[bridge.STARTUP_JOB_ID].func(*sched.jobs[bridge.STARTUP_JOB_ID].args)
        active = q.active()
        assert len(active) == 1 and active[0]["source"] == "scheduled"

    def test_without_a_queue_the_schedule_is_left_alone(self, tmp_path, monkeypatch):
        b, sched, _ = make(tmp_path, monkeypatch)
        original = lambda: None  # noqa: E731
        sched.add_job(original, id="claim_all")
        b.wrap_jobs()
        assert sched.jobs["claim_all"].func is original

    def test_a_restart_marks_the_cut_off_run(self, tmp_path, monkeypatch):
        q = jobqueue.JobQueue(tmp_path / "q")
        q.bot_heartbeat("alice")
        q.enqueue("alice")
        job = q.claim("alice", 1, 3600)
        b, _, _ = make(tmp_path, monkeypatch, queue=q)
        b.setup()
        assert q.get(job["id"])["status"] == jobqueue.FAILED

    def test_a_claimed_job_runs_and_is_finished_with_a_summary(self, tmp_path, monkeypatch):
        q = jobqueue.JobQueue(tmp_path / "q")
        events = []
        b, _, _ = make(tmp_path, monkeypatch, queue=q, events=events)
        job, _ = q.enqueue("alice", source="manual")

        async def scenario():
            b.poll()
            await b.task

        asyncio.run(scenario())
        done = q.get(job["id"])
        assert events == [("start", "alice"), ("end", "alice")]
        assert done["status"] == jobqueue.DONE and done["result"]["claimed"] == 0

    def test_a_crashing_run_is_marked_failed(self, tmp_path, monkeypatch):
        q = jobqueue.JobQueue(tmp_path / "q")
        b, _, _ = make(tmp_path, monkeypatch, queue=q)

        async def boom():
            raise RuntimeError("chrome gone")

        b.run_job = boom
        job, _ = q.enqueue("alice")

        async def scenario():
            b.poll()
            await b.task

        asyncio.run(scenario())
        got = q.get(job["id"])
        assert got["status"] == jobqueue.FAILED and "chrome gone" in got["error"]

    def test_a_stuck_run_is_marked_timeout_and_restarts_the_bot(self, tmp_path, monkeypatch):
        q = jobqueue.JobQueue(tmp_path / "q")
        b, _, _ = make(tmp_path, monkeypatch, queue=q, run_seconds=5)
        restarted = []
        monkeypatch.setattr(b, "restart", lambda req: restarted.append(req))
        job, _ = q.enqueue("alice")

        async def scenario():
            b.poll()
            b.timeout = 0
            b.current_started -= 1
            b.poll()
            b.task.cancel()

        asyncio.run(scenario())
        assert restarted and q.get(job["id"])["status"] == jobqueue.TIMEOUT

    def test_no_new_job_while_a_restart_is_pending(self, tmp_path, monkeypatch):
        q = jobqueue.JobQueue(tmp_path / "q")
        b, _, _ = make(tmp_path, monkeypatch, queue=q)
        q.enqueue("alice")
        protocol.write_json(b.dir / protocol.RESTART_REQUEST, {})
        monkeypatch.setattr(b, "restart", lambda req: None)

        async def scenario():
            b.poll()

        asyncio.run(scenario())
        assert b.current is None and q.active()[0]["status"] == jobqueue.QUEUED


async def _drive(bridges, until):
    while not until():
        for b in bridges:
            b.poll()
        await asyncio.sleep(0.01)
    for b in bridges:
        if b.task:
            await b.task


def _overlap(events) -> int:
    running = most = 0
    for kind, _ in events:
        running += 1 if kind == "start" else -1
        most = max(most, running)
    return most


@pytest.mark.parametrize("workers,expected", [("1", 1), ("2", 2)])
def test_two_bots_and_the_worker_limit(tmp_path, monkeypatch, workers, expected):
    q = jobqueue.JobQueue(tmp_path / "q")
    events = []
    a, _, _ = make(tmp_path, monkeypatch, "alice", q, workers, events, run_seconds=0.2)
    b, _, _ = make(tmp_path, monkeypatch, "bob", q, workers, events, run_seconds=0.2)
    for bot in (a, b):
        bot.last_beat = 0
        q.bot_heartbeat(bot.bot_name)
    q.enqueue("alice")
    q.enqueue("bob")
    asyncio.run(_drive([a, b], lambda: len(events) == 4))
    # FGC_WORKERS=1: never two runs at once, in FIFO order. =2: both at once is allowed (and happens).
    assert _overlap(events) == expected
    if expected == 1:
        assert events == [("start", "alice"), ("end", "alice"), ("start", "bob"), ("end", "bob")]
