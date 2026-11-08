"""A store that hangs is stopped at its limit, so the runs after it are not skipped; its browser is closed even then."""

import asyncio
import logging
from pathlib import Path

import pytest

import main
import src.core.claimer as claimer_module


class TestTheLimit:
    def test_it_grows_with_the_wait_for_you_in_novnc(self, monkeypatch):
        monkeypatch.setattr(main.cfg, "vnc_login_timeout", 180)
        assert main.store_time_limit() == 3600 + 720
        monkeypatch.setattr(main.cfg, "vnc_login_timeout", 900)
        assert main.store_time_limit() == 3600 + 3600

    def test_a_hung_store_is_stopped_and_cleans_up(self, monkeypatch, caplog):
        # Live 7.11: AliExpress waited 23 minutes on a page that never loaded.
        monkeypatch.setattr(main, "store_time_limit", lambda: 0.05)
        monkeypatch.setattr(main.cfg, "notify_errors", True)
        sent, closed = [], []

        async def notify(text):
            sent.append(text)
        monkeypatch.setattr(main, "notify", notify)

        async def hung():
            try:
                await asyncio.sleep(3600)
            finally:
                # The real close awaits too (a polite close, then a second's pause), after the cancel.
                await asyncio.sleep(0.01)
                closed.append(True)

        with caplog.at_level(logging.WARNING):
            # The outer five seconds only keep a broken limit from hanging the test run.
            assert asyncio.run(asyncio.wait_for(main.within_limit("AliExpress", hung(), "aliexpress"), timeout=5)) is None
        assert closed == [True] and "AliExpress ran longer than" in caplog.text and len(sent) == 1

    def test_a_store_in_time_gives_its_result(self):
        async def quick():
            return {"store": "Epic", "games": []}
        assert asyncio.run(main.within_limit("Epic", quick())) == {"store": "Epic", "games": []}

    def test_a_crash_still_reaches_the_caller(self):
        async def broken():
            raise RuntimeError("boom")
        with pytest.raises(RuntimeError):
            asyncio.run(main.within_limit("Epic", broken()))

    def test_every_store_and_redeemer_runs_within_it(self):
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        for call in ("func(routed.get(key)) if key in GP_TARGETS else func()", "gog.redeem_pending_codes()",
                     "ms_claimer.redeem_pending_codes()", "lg_claimer.redeem_pending_codes()", "claim_side_stores(routed)",
                     "steam_keys.redeem_pending_keys()", "meet(items, with_you)"):
            line = next(l for l in source.splitlines() if call in l and "await" in l)
            assert "within_limit(" in line, line


class TestAHungChromeIsStillClosed:
    def test_the_polite_close_gives_up_and_the_process_is_killed(self, monkeypatch):
        monkeypatch.setattr(claimer_module, "CLEAN_CLOSE_WAIT", 0.05)
        stopped, killed = [], []

        class Page:
            async def send(self, _command):
                await asyncio.sleep(3600)

        class Browser:
            _process_pid = 4242

            def stop(self):
                stopped.append(True)

        claimer = claimer_module.BaseClaimer.__new__(claimer_module.BaseClaimer)
        claimer.store_name = "test"
        claimer.page, claimer.browser = Page(), Browser()
        monkeypatch.setattr(claimer, "_kill_process_tree", lambda pid: killed.append(pid), raising=False)
        asyncio.run(asyncio.wait_for(claimer.close_browser(), timeout=5))
        assert stopped == [True] and killed == [4242]
