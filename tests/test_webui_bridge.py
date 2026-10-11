"""[fork:webui] The bot side: status file, requests, and one claim run at a time across bots."""

import asyncio
import json

import pytest

from webui import bridge, protocol


class FakeJob:
    def __init__(self, job_id, func):
        self.id, self.func, self.next_run_time = job_id, func, None


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

    def modify_job(self, job_id, func):
        self.jobs[job_id].func = func


def make(tmp_path, monkeypatch, name="alice", lock_dir=None, runs=None):
    if lock_dir:
        monkeypatch.setenv("FGC_RUN_LOCK_DIR", str(lock_dir))
    else:
        monkeypatch.delenv("FGC_RUN_LOCK_DIR", raising=False)
    monkeypatch.setenv("FGC_BOT_NAME", name)
    sched, lock = FakeScheduler(), asyncio.Lock()

    async def run_job():
        async with lock:
            if runs is not None:
                runs.append(("start", name))
            await asyncio.sleep(0.05)
            if runs is not None:
                runs.append(("end", name))

    b = bridge.Bridge(sched, run_job, lock, tmp_path / name)
    b.dir.mkdir(parents=True)
    return b, sched, lock


class TestStatus:
    def test_status_file_is_written(self, tmp_path, monkeypatch):
        b, _, _ = make(tmp_path, monkeypatch)
        b.poll()
        data = protocol.read_json(b.dir / protocol.STATUS_FILE)
        assert data["running"] is False and data["heartbeat"]

    def test_a_finished_run_is_remembered_with_its_problems(self, tmp_path, monkeypatch):
        b, _, lock = make(tmp_path, monkeypatch)

        async def scenario():
            await lock.acquire()
            b.track_run()
            b.collector.emit(__import__("logging").makeLogRecord({"levelname": "ERROR", "msg": "Epic login failed"}))
            lock.release()
            b.track_run()

        asyncio.run(scenario())
        assert b.last_run["problems"][0]["message"] == "Epic login failed"

    def test_log_lines_lose_markup_and_secrets(self):
        import logging
        rec = logging.makeLogRecord({"msg": "\\[Steam] password=hunter2 ok"})
        assert bridge.plain(rec) == "[Steam] password=*** ok"


class TestRequests:
    def test_run_request_is_taken_once(self, tmp_path, monkeypatch):
        b, sched, _ = make(tmp_path, monkeypatch)
        protocol.write_json(b.dir / protocol.RUN_REQUEST, {})
        b.poll()
        assert bridge.WEBUI_JOB_ID in sched.jobs
        assert not (b.dir / protocol.RUN_REQUEST).exists()

    def test_restart_waits_for_a_running_claim(self, tmp_path, monkeypatch):
        b, _, lock = make(tmp_path, monkeypatch)
        protocol.write_json(b.dir / protocol.RESTART_REQUEST, {"run_after": False})

        async def scenario():
            await lock.acquire()
            during = b.poll()
            lock.release()
            return during, b.poll()

        during, after = asyncio.run(scenario())
        assert during is None and after == {"run_after": False}

    def test_after_a_settings_restart_the_startup_run_is_skipped(self, tmp_path, monkeypatch):
        b, sched, _ = make(tmp_path, monkeypatch)
        sched.add_job(lambda: None, id=bridge.STARTUP_JOB_ID)
        protocol.write_json(b.dir / protocol.RESTART_NOTE, {"run_after": False})
        b.setup()
        assert bridge.STARTUP_JOB_ID not in sched.jobs

    def test_jobs_are_routed_through_the_queue(self, tmp_path, monkeypatch):
        b, sched, _ = make(tmp_path, monkeypatch)
        sched.add_job(lambda: None, id="claim_all")
        b.wrap_jobs()
        assert sched.jobs["claim_all"].func == b.queued_run


class TestOneRunAtATime:
    def test_two_bots_take_turns(self, tmp_path, monkeypatch):
        runs = []
        a, _, _ = make(tmp_path, monkeypatch, "alice", tmp_path / "lock", runs)
        b, _, _ = make(tmp_path, monkeypatch, "bob", tmp_path / "lock", runs)
        monkeypatch.setattr(bridge, "LOCK_POLL_SECONDS", 0.01)

        async def both():
            await asyncio.gather(a.queued_run(), b.queued_run())

        asyncio.run(both())
        # Never two "start" in a row: the second bot waited for the first to end.
        assert [k for k, _ in runs] == ["start", "end", "start", "end"]

    def test_a_second_trigger_while_queued_is_dropped(self, tmp_path, monkeypatch):
        runs = []
        a, _, _ = make(tmp_path, monkeypatch, "alice", None, runs)

        async def twice():
            await asyncio.gather(a.queued_run(), a.queued_run())

        asyncio.run(twice())
        assert runs.count(("start", "alice")) == 1

    def test_waiting_gives_up_after_the_timeout(self, tmp_path, monkeypatch):
        import fcntl
        runs = []
        a, _, _ = make(tmp_path, monkeypatch, "alice", tmp_path / "lock", runs)
        a.timeout = 0.03
        monkeypatch.setattr(bridge, "LOCK_POLL_SECONDS", 0.01)
        (tmp_path / "lock").mkdir()
        with open(tmp_path / "lock" / "run.lock", "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            asyncio.run(a.queued_run())
        assert runs == [] and a.queued_since is None

    def test_a_stuck_run_restarts_the_bot(self, tmp_path, monkeypatch):
        b, _, lock = make(tmp_path, monkeypatch)
        b.timeout = 0
        restarted = []
        monkeypatch.setattr(b, "restart", lambda req: restarted.append(req))

        async def scenario():
            await lock.acquire()
            b.track_run()
            b.run_started_at -= 1
            b.check_timeout()
            lock.release()

        asyncio.run(scenario())
        assert restarted == [{"run_after": False}]
