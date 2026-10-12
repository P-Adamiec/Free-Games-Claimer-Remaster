"""AliExpress raises the alarm only when no later run can still collect today (2026-10-10: an alert came, the next run collected)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import run_state
from src.stores.aliexpress import ae_day_end, day_start_from_payloads, retry_later

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "src" / "stores" / "aliexpress.py").read_text(encoding="utf-8")
# Today's node as the API sent it on 2026-10-11: the day began at 07:00 UTC, midnight in California.
DAY_START_MS = 1791702000000
DAY_START = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)


def _payload(nodes):
    body = {"data": {"data": {"systemTimeMill": DAY_START_MS + 1, "signQuerySequenceNodeList": [{"dailySignNodeList": nodes}]}}}
    return {"api": "mtop.aliexpress.coin.channel.sign.list", "body": json.dumps(body)}


class TestTheDay:
    def test_the_day_start_matches_the_saved_answer(self):
        assert datetime.fromtimestamp(DAY_START_MS / 1000, tz=timezone.utc) == DAY_START

    def test_todays_start_is_read_from_its_node(self):
        nodes = [{"calendarDayDistance": -1, "calendarDayStartTimeMill": DAY_START_MS - 86400000},
                 {"calendarDayDistance": 0, "calendarDayStartTimeMill": DAY_START_MS}]
        assert day_start_from_payloads([_payload(nodes)]) == DAY_START_MS

    @pytest.mark.parametrize("payloads", [[], None, [{"api": "mtop.other", "body": "{}"}], [{"api": "sign.list", "body": "x"}]])
    def test_no_calendar_says_nothing(self, payloads):
        assert day_start_from_payloads(payloads) is None

    def test_the_day_ends_a_day_after_it_began(self):
        assert ae_day_end(DAY_START_MS, DAY_START + timedelta(hours=7)) == DAY_START + timedelta(days=1)

    def test_without_the_api_it_is_the_next_midnight_in_california(self):
        assert ae_day_end(None, datetime(2026, 10, 11, 13, 56, tzinfo=timezone.utc)) == datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)

    def test_in_winter_california_midnight_is_an_hour_later(self):
        assert ae_day_end(None, datetime(2026, 12, 1, 13, 56, tzinfo=timezone.utc)) == datetime(2026, 12, 2, 8, 0, tzinfo=timezone.utc)

    def test_a_stale_start_falls_back_to_the_clock(self):
        late = DAY_START + timedelta(days=3, hours=1)
        assert ae_day_end(DAY_START_MS, late) > late


class TestRetryLater:
    NOW = datetime(2026, 10, 10, 13, 56, tzinfo=timezone.utc)
    STARTED = NOW - timedelta(minutes=5)

    def test_the_run_twelve_hours_later_still_has_hours_of_today(self):
        # 10.10 13:56 UTC failed, 11.10 01:56 UTC collected; the day ended 11.10 07:00 UTC.
        assert retry_later(self.NOW + timedelta(hours=12), now=self.NOW, started=self.STARTED) is True

    def test_a_run_after_the_day_ends_is_too_late(self):
        assert retry_later(self.NOW + timedelta(hours=24), now=self.NOW, started=self.STARTED) is False

    def test_a_run_that_leaves_less_than_two_hours_is_too_late(self):
        end = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)
        assert retry_later(end - timedelta(minutes=90), now=self.NOW, started=self.STARTED) is False

    def test_the_time_a_run_takes_to_reach_aliexpress_counts(self):
        end = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)
        slow_start = self.NOW - timedelta(minutes=50)
        assert retry_later(end - timedelta(hours=2, minutes=30), now=self.NOW, started=slow_start) is False
        assert retry_later(end - timedelta(hours=2, minutes=30), now=self.NOW, started=self.STARTED) is True

    def test_a_trigger_during_this_run_is_dropped_so_it_does_not_count(self):
        assert retry_later(self.NOW + timedelta(minutes=10), now=self.NOW, started=self.STARTED) is False

    def test_a_past_next_run_does_not_count(self):
        assert retry_later(self.NOW - timedelta(minutes=5), now=self.NOW, started=self.STARTED) is False

    def test_no_next_run_means_this_was_the_last_chance(self):
        assert retry_later(None, now=self.NOW) is False

    def test_the_api_day_wins_over_the_clock(self):
        assert retry_later(DAY_START + timedelta(hours=20), DAY_START_MS, now=DAY_START + timedelta(hours=8)) is True


class TestTheDayTheClocksChange:
    def test_spring_forward_day_ends_at_california_midnight(self):
        # 8 March 2026: the day starts 08:00 UTC (PST) and ends 07:00 UTC next day (PDT), 23 hours.
        start = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
        end = ae_day_end(int(start.timestamp() * 1000), start + timedelta(hours=4))
        assert end == datetime(2026, 3, 9, 7, 0, tzinfo=timezone.utc)


class TestEveryMissAsksFirst:
    @pytest.mark.parametrize("before", [
        'self._report("failed, the coin page did not render',
        "🚫 Session flagged as low-trust",
        "AliExpress check-in widget did not render, offering manual VNC collection",
        "AliExpress served an empty coin page",
    ])
    def test_the_alarm_comes_only_after_asking_about_the_next_run(self, before):
        tail = SOURCE.split(before, 1)[0][-900:]
        assert "_leave_to_next_run(" in tail

    def test_a_deferred_miss_is_a_short_quiet_skip(self):
        block = SOURCE.split("def _leave_to_next_run", 1)[1].split("\n    def ", 1)[0]
        assert 'self._report("skipped, next run tries again ⏳")' in block


class TestTheNextRunIsKnown:
    def test_it_is_asked_when_needed(self):
        when = datetime(2026, 10, 11, 1, 56, tzinfo=timezone.utc)
        run_state.set_next_run_source(lambda: when)
        assert run_state.next_run() == when
        run_state.set_next_run_source(None)
        assert run_state.next_run() is None

    def test_a_broken_scheduler_answer_means_no_next_run(self):
        def broken():
            raise RuntimeError("scheduler shut down")
        run_state.set_next_run_source(broken)
        assert run_state.next_run() is None
        run_state.set_next_run_source(None)

    def test_a_run_knows_when_it_started(self):
        run_state.reset_run_state()
        assert run_state.run_started() is not None

    def test_main_takes_the_earliest_repeating_job(self, monkeypatch):
        import main
        soon = datetime(2026, 10, 11, 1, 56, tzinfo=timezone.utc)
        jobs = [SimpleNamespace(id="claim_all", next_run_time=soon + timedelta(hours=12)),
                SimpleNamespace(id="claim_fixed_08_00", next_run_time=soon),
                SimpleNamespace(id="claim_all_startup", next_run_time=soon - timedelta(hours=1)),
                SimpleNamespace(id="paused", next_run_time=None)]
        monkeypatch.setattr(main, "_scheduler", SimpleNamespace(get_jobs=lambda: jobs))
        assert main._next_scheduled_run() == soon

    def test_a_single_run_has_no_next_one(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_scheduler", None)
        assert main._next_scheduled_run() is None

    def test_main_hands_the_scheduler_over(self):
        assert "set_next_run_source(_next_scheduled_run)" in (ROOT / "main.py").read_text(encoding="utf-8")
