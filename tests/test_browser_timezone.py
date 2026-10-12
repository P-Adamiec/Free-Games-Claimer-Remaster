"""The browser's time zone follows your IP address unless you chose one, and never leaks into this process's clock."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.core import browser_timezone as bt
from src.core.config import cfg


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bt, "_zone", None)
    monkeypatch.setattr(cfg, "tz", None)
    monkeypatch.setattr(cfg, "browser_timezone", "auto")
    monkeypatch.delenv("TZ", raising=False)
    # The saved answer lives in the data folder, which here is a fresh one per test.
    monkeypatch.setattr(bt.cfg, "_data_dir", tmp_path)
    monkeypatch.setattr(bt.cfg, "browser_timezone_refresh", "month")


class TestReadingTheAnswer:
    @pytest.mark.parametrize("name,zone", [
        ("Europe/Warsaw", "Europe/Warsaw"),
        ("  Europe/Warsaw\n", "Europe/Warsaw"),
        ("UTC", "UTC"),
        ("Not/AZone", None),
        ("../../etc/passwd", None),
        # A region folder raises an OS error, not ZoneInfoNotFoundError, and once stopped every store.
        ("Europe", None),
        ("Etc", None),
        ("America", None),
        ("", None),
        (None, None),
    ])
    def test_only_real_zones_pass(self, name, zone):
        assert bt.valid_zone(name) == zone

    def test_the_plain_text_answer(self):
        assert bt.zone_from_answer("text", "Europe/Warsaw\n") == "Europe/Warsaw"

    def test_the_json_answer(self):
        assert bt.zone_from_answer("json", '{"timezone": {"id": "Europe/Warsaw", "abbr": "CEST"}}') == "Europe/Warsaw"

    @pytest.mark.parametrize("body", ["", "not json", '{"success": false}', '{"timezone": {"id": "Mars/Base"}}', "[]"])
    def test_a_useless_json_answer_gives_nothing(self, body):
        assert bt.zone_from_answer("json", body) is None

    def test_a_hostile_deeply_nested_answer_gives_nothing(self):
        assert bt.zone_from_answer("json", "[" * 100000 + "]" * 100000) is None

    def test_an_error_page_in_text_gives_nothing(self):
        assert bt.zone_from_answer("text", "<html>Too many requests</html>") is None


class TestWhoDecides:
    def _refresh(self, monkeypatch, found="Europe/Warsaw"):
        calls = []

        async def fake_lookup(timeout=6.0):
            calls.append(1)
            return found
        monkeypatch.setattr(bt, "lookup", fake_lookup)
        asyncio.run(bt.refresh())
        return calls

    def test_auto_takes_the_ip_address_zone(self, monkeypatch):
        assert self._refresh(monkeypatch) and bt._zone == "Europe/Warsaw"

    def test_your_tz_wins_and_nothing_is_looked_up(self, monkeypatch):
        monkeypatch.setattr(cfg, "tz", "America/New_York")
        assert self._refresh(monkeypatch) == [] and bt._zone is None

    def test_off_keeps_utc_without_asking(self, monkeypatch):
        monkeypatch.setattr(cfg, "browser_timezone", "off")
        assert self._refresh(monkeypatch) == [] and bt._zone is None

    def test_a_named_zone_is_used_without_asking(self, monkeypatch):
        monkeypatch.setattr(cfg, "browser_timezone", "Asia/Tokyo")
        assert self._refresh(monkeypatch) == [] and bt._zone == "Asia/Tokyo"

    def test_a_misspelt_zone_keeps_utc(self, monkeypatch):
        monkeypatch.setattr(cfg, "browser_timezone", "Europe/Warszawa")
        self._refresh(monkeypatch)
        assert bt._zone is None

    def test_a_failed_lookup_keeps_the_last_zone(self, monkeypatch):
        monkeypatch.setattr(bt, "_zone", "Europe/Warsaw")
        self._refresh(monkeypatch, found=None)
        assert bt._zone == "Europe/Warsaw"


class TestOnlyChromeGetsIt:
    def test_tz_is_set_while_chrome_starts_and_gone_after(self, monkeypatch):
        monkeypatch.setattr(bt, "_zone", "Europe/Warsaw")
        with bt.chrome_timezone():
            assert os.environ.get("TZ") == "Europe/Warsaw"
        assert "TZ" not in os.environ

    def test_it_is_gone_after_a_failed_start_too(self, monkeypatch):
        monkeypatch.setattr(bt, "_zone", "Europe/Warsaw")
        with pytest.raises(RuntimeError):
            with bt.chrome_timezone():
                raise RuntimeError("Chrome did not start")
        assert "TZ" not in os.environ

    def test_an_earlier_value_is_put_back(self, monkeypatch):
        monkeypatch.setenv("TZ", "")
        monkeypatch.setattr(bt, "_zone", "Europe/Warsaw")
        with bt.chrome_timezone():
            assert os.environ["TZ"] == "Europe/Warsaw"
        assert os.environ["TZ"] == ""

    def test_a_lookup_that_breaks_does_not_stop_the_run(self, monkeypatch):
        async def broken(timeout=6.0):
            raise RuntimeError("provider down")
        monkeypatch.setattr(bt, "lookup", broken)
        monkeypatch.setattr(bt, "_zone", "Europe/Warsaw")
        asyncio.run(bt.refresh())
        assert bt._zone == "Europe/Warsaw"

    def test_nothing_is_touched_without_a_zone(self):
        with bt.chrome_timezone():
            assert "TZ" not in os.environ

    def test_your_tz_is_left_as_it_is(self, monkeypatch):
        monkeypatch.setattr(cfg, "tz", "America/New_York")
        monkeypatch.setenv("TZ", "America/New_York")
        monkeypatch.setattr(bt, "_zone", "Europe/Warsaw")
        with bt.chrome_timezone():
            assert os.environ["TZ"] == "America/New_York"
        assert os.environ["TZ"] == "America/New_York"


class TestTheAnswerIsKept:
    """Asked on the first run, then again only when BROWSER_TIMEZONE_REFRESH says, a month by default."""

    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def _refresh(self, monkeypatch, found="Europe/Warsaw"):
        calls = []

        async def fake_lookup(timeout=6.0):
            calls.append(1)
            return found
        monkeypatch.setattr(bt, "lookup", fake_lookup)
        asyncio.run(bt.refresh())
        return calls

    def _save(self, zone, days_ago):
        bt._state_path().write_text(json.dumps({"zone": zone, "checked_at": (
            datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()}), encoding="utf-8")

    @pytest.mark.parametrize("every,days_ago,due", [
        ("run", 0, True),
        ("week", 6, False), ("week", 8, True),
        ("month", 29, False), ("month", 30, True),
        ("anything else", 29, False),
    ])
    def test_when_it_asks_again(self, every, days_ago, due):
        state = {"zone": "Europe/Warsaw", "checked_at": (self.NOW - timedelta(days=days_ago)).isoformat()}
        assert bt.lookup_due(state, every, self.NOW) is due

    @pytest.mark.parametrize("state", [
        {}, {"zone": "Mars/Base", "checked_at": "2026-10-11T12:00:00+00:00"},
        {"zone": "Europe/Warsaw", "checked_at": "not a date"},
        {"zone": "Europe/Warsaw", "checked_at": "2027-01-01T00:00:00+00:00"},
    ])
    def test_a_missing_broken_or_future_answer_is_asked_again(self, state):
        assert bt.lookup_due(state, "month", self.NOW) is True

    def test_the_first_run_asks_and_saves(self, monkeypatch):
        assert self._refresh(monkeypatch) == [1]
        assert json.loads(bt._state_path().read_text(encoding="utf-8"))["zone"] == "Europe/Warsaw"

    def test_the_next_run_asks_nobody(self, monkeypatch):
        self._save("Europe/Berlin", days_ago=3)
        assert self._refresh(monkeypatch) == [] and bt._zone == "Europe/Berlin"

    def test_a_month_later_it_asks_again(self, monkeypatch):
        self._save("Europe/Berlin", days_ago=31)
        assert self._refresh(monkeypatch) == [1] and bt._zone == "Europe/Warsaw"

    def test_per_run_asks_every_time(self, monkeypatch):
        monkeypatch.setattr(bt.cfg, "browser_timezone_refresh", "run")
        self._save("Europe/Berlin", days_ago=0)
        assert self._refresh(monkeypatch) == [1]

    def test_a_failed_lookup_keeps_the_saved_zone_and_tries_again_next_run(self, monkeypatch):
        self._save("Europe/Berlin", days_ago=31)
        before = bt._state_path().read_text(encoding="utf-8")
        self._refresh(monkeypatch, found=None)
        assert bt._zone == "Europe/Berlin" and bt._state_path().read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("setting,tz", [("Asia/Tokyo", None), ("off", None), ("auto", "America/New_York")])
    def test_a_zone_you_chose_asks_nobody_and_saves_nothing(self, monkeypatch, setting, tz):
        monkeypatch.setattr(cfg, "browser_timezone", setting)
        monkeypatch.setattr(cfg, "tz", tz)
        assert self._refresh(monkeypatch) == [] and not bt._state_path().exists()

    def test_an_unknown_interval_is_named_and_counts_as_a_month(self, monkeypatch, caplog):
        monkeypatch.setattr(bt.cfg, "browser_timezone_refresh", "daily")
        self._save("Europe/Berlin", days_ago=3)
        with caplog.at_level("WARNING", logger="fgc.timezone"):
            assert self._refresh(monkeypatch) == []
        assert "BROWSER_TIMEZONE_REFRESH=daily" in caplog.text
