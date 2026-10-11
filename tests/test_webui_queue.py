"""[fork:webui] The shared job queue: FIFO across bots, a worker limit, dedupe, recovery and history."""

import multiprocessing

import pytest

from webui import jobqueue as jq

TIMEOUT = 3 * 3600


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def q(tmp_path, clock):
    queue = jq.JobQueue(tmp_path, clock=clock)
    for bot in ("a", "b", "c"):
        queue.bot_heartbeat(bot)
    return queue


def add(q, clock, bot, source="scheduled"):
    clock.t += 1
    return q.enqueue(bot, source=source)[0]


class TestEnqueue:
    def test_a_new_job_waits(self, q, clock):
        job = add(q, clock, "a")
        assert job["status"] == jq.QUEUED and job["type"] == "run" and job["source"] == "scheduled"

    def test_no_second_waiting_job_for_the_same_bot(self, q, clock):
        first = add(q, clock, "a")
        again, created = q.enqueue("a", source="manual")
        assert not created and again["id"] == first["id"]

    def test_a_running_job_does_not_block_the_next_one(self, q, clock):
        add(q, clock, "a")
        q.claim("a", 1, TIMEOUT)
        _, created = q.enqueue("a", source="manual")
        assert created

    def test_params_are_kept_for_a_partial_run_later(self, q):
        job, _ = q.enqueue("a", params={"stores": ["epic"]})
        assert q.get(job["id"])["params"] == {"stores": ["epic"]}

    def test_unknown_type(self, q):
        with pytest.raises(ValueError):
            q.enqueue("a", job_type="format_disk")

    def test_database_is_in_wal_mode(self, q):
        con = q._connect()
        try:
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            con.close()


class TestClaimOrder:
    def test_fifo_across_bots(self, q, clock):
        add(q, clock, "b")
        add(q, clock, "a")
        assert q.claim("a", 1, TIMEOUT) is None      # b asked first
        assert q.claim("b", 1, TIMEOUT)["bot"] == "b"

    def test_one_worker_means_one_run(self, q, clock):
        add(q, clock, "a")
        add(q, clock, "b")
        assert q.claim("a", 1, TIMEOUT)
        assert q.claim("b", 1, TIMEOUT) is None

    def test_two_workers_allow_two_runs(self, q, clock):
        add(q, clock, "a")
        add(q, clock, "b")
        assert q.claim("a", 2, TIMEOUT)
        assert q.claim("b", 2, TIMEOUT)["bot"] == "b"

    def test_a_bot_never_runs_twice_at_once(self, q, clock):
        add(q, clock, "a")
        q.claim("a", 3, TIMEOUT)
        add(q, clock, "a")
        assert q.claim("a", 3, TIMEOUT) is None

    def test_a_busy_bot_at_the_head_does_not_block_others(self, q, clock):
        add(q, clock, "a")
        q.claim("a", 2, TIMEOUT)
        add(q, clock, "a")                            # a's next run, first in line but a is busy
        add(q, clock, "b")
        assert q.claim("b", 2, TIMEOUT)["bot"] == "b"

    def test_a_dead_bot_at_the_head_does_not_block_others(self, q, clock):
        add(q, clock, "a")
        add(q, clock, "b")
        clock.t += jq.BOT_ALIVE_SECONDS + 1
        q.bot_heartbeat("b")
        assert q.claim("b", 1, TIMEOUT)["bot"] == "b"

    def test_nothing_queued(self, q):
        assert q.claim("a", 1, TIMEOUT) is None

    def test_claiming_is_atomic_across_processes(self, tmp_path):
        queue = jq.JobQueue(tmp_path)
        for bot in ("p0", "p1", "p2", "p3", "p4", "p5"):
            queue.bot_heartbeat(bot)
            queue.enqueue(bot)
        # spawn, not fork: an SQLite library state must never be copied into a child process.
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(6) as pool:
            got = pool.starmap(_claim_in_process, [(str(tmp_path), f"p{i}") for i in range(6)])
        assert sum(1 for g in got if g) == 1
        statuses = [(j["bot"], j["status"], j["error"]) for j in queue._read("SELECT * FROM jobs")]
        assert sum(1 for _, st, _ in statuses if st == jq.RUNNING) == 1, statuses


def _claim_in_process(folder, bot):
    return jq.JobQueue(folder).claim(bot, 1, TIMEOUT) is not None


class TestFinishAndRecover:
    def test_finish_frees_the_slot(self, q, clock):
        add(q, clock, "a")
        add(q, clock, "b")
        job = q.claim("a", 1, TIMEOUT)
        q.finish(job["id"], jq.DONE, {"claimed": 2, "failed": 0})
        assert q.get(job["id"])["result"] == {"claimed": 2, "failed": 0}
        assert q.claim("b", 1, TIMEOUT)

    def test_a_silent_bot_loses_its_job(self, q, clock):
        add(q, clock, "a")
        add(q, clock, "b")
        job = q.claim("a", 1, TIMEOUT)
        clock.t += jq.STALE_SECONDS + 1
        q.bot_heartbeat("b")
        assert q.claim("b", 1, TIMEOUT)               # recovery runs inside the claim
        assert q.get(job["id"])["status"] == jq.FAILED

    def test_heartbeats_keep_a_long_run_alive(self, q, clock):
        add(q, clock, "a")
        job = q.claim("a", 1, TIMEOUT)
        for _ in range(10):
            clock.t += jq.STALE_SECONDS - 1
            q.heartbeat(job["id"])
        q.recover(TIMEOUT)
        assert q.get(job["id"])["status"] == jq.RUNNING

    def test_a_run_past_the_timeout_is_marked(self, q, clock):
        add(q, clock, "a")
        job = q.claim("a", 1, 600)
        clock.t += 601
        q.heartbeat(job["id"])
        q.recover(600)
        got = q.get(job["id"])
        assert got["status"] == jq.TIMEOUT and "10 minuten" in got["error"]

    def test_a_restarted_bot_abandons_its_run(self, q, clock):
        add(q, clock, "a")
        job = q.claim("a", 1, TIMEOUT)
        assert q.abandon_running("a", "herstart") == 1
        assert q.get(job["id"])["status"] == jq.FAILED

    def test_finish_does_not_overwrite_a_recovered_job(self, q, clock):
        add(q, clock, "a")
        job = q.claim("a", 1, 600)
        clock.t += 601
        q.recover(600)
        q.finish(job["id"], jq.DONE)
        assert q.get(job["id"])["status"] == jq.TIMEOUT

    def test_old_waiting_jobs_expire(self, q, clock):
        job = add(q, clock, "a")
        clock.t += jq.QUEUED_EXPIRE_SECONDS + 1
        q.recover(TIMEOUT)
        assert q.get(job["id"])["status"] == jq.CANCELLED


class TestCancel:
    def test_a_waiting_job(self, q, clock):
        job = add(q, clock, "a")
        assert q.cancel(job["id"], by="admin")
        assert q.get(job["id"])["status"] == jq.CANCELLED
        assert q.claim("a", 1, TIMEOUT) is None

    def test_not_a_running_job(self, q, clock):
        add(q, clock, "a")
        job = q.claim("a", 1, TIMEOUT)
        assert not q.cancel(job["id"])


class TestHistory:
    def test_positions_in_line(self, q, clock):
        add(q, clock, "a")
        add(q, clock, "b")
        add(q, clock, "c")
        q.claim("a", 1, TIMEOUT)
        active = q.active()
        assert [(j["bot"], j.get("position")) for j in active] == [("a", None), ("b", 1), ("c", 2)]
        assert q.bot_state("c")["queued"]["position"] == 2

    def test_prune_by_age_and_count(self, q, clock):
        for _ in range(5):
            add(q, clock, "a")
            job = q.claim("a", 1, TIMEOUT)
            q.finish(job["id"])
        assert q.prune(days=30, keep=3) == 2
        clock.t += 31 * 86400
        assert q.prune(days=30, keep=3) == 3
        assert q.history() == []


class TestWorkers:
    @pytest.mark.parametrize("gb,expected", [(None, 1), (1, 1), (2, 1), (4, 2), (8, 4), (64, 4)])
    def test_auto_from_memory(self, gb, expected):
        assert jq.auto_workers(gb and int(gb * jq.GB)) == expected

    @pytest.mark.parametrize("raw,expected", [("", 1), ("2", 2), ("0", 1), ("junk", 1), ("99", 16)])
    def test_from_env(self, raw, expected):
        assert jq.workers_from_env({"FGC_WORKERS": raw}) == expected

    def test_auto_reads_this_machine(self):
        assert 1 <= jq.workers_from_env({"FGC_WORKERS": "auto"}) <= jq.MAX_AUTO_WORKERS
