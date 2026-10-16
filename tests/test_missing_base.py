"""#78: a DLC whose base game you lack is told once, then again only if it is still free three weeks later."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.claimer import missing_base_due, missing_base_status, older_than, row_extra, track_missing_base

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)


class TestWhenToTell:
    @pytest.mark.parametrize("previous,reported,due", [
        (None, None, True),
        ("unknown", None, True),
        ("existed", (NOW - timedelta(days=2)).isoformat(), True),
        ("failed:missing_base", None, True),
        ("failed:missing_base", (NOW - timedelta(days=20)).isoformat(), False),
        ("failed:missing_base", (NOW - timedelta(days=22)).isoformat(), True),
        ("failed:missing_base", (NOW - timedelta(days=21)).isoformat(), True),
        ("failed:missing_base", "not a date", True),
        ("failed:missing_base", "2026-10-10T12:00:00", False),
    ])
    def test_new_changed_or_weeks_old_is_told(self, previous, reported, due):
        assert missing_base_due(previous, reported, NOW) is due

    def test_the_status_is_shown_or_parked(self):
        assert missing_base_status(True) == "failed:missing_base"
        assert missing_base_status(False) == "skipped:missing_base"


class TestDatesAndExtra:
    """Shared with Epic, which reads an owned game's page again only every few days."""

    @pytest.mark.parametrize("stamp,old", [
        ((NOW - timedelta(days=1)).isoformat(), False),
        ((NOW - timedelta(days=3)).isoformat(), True),
        ((NOW - timedelta(days=4)).isoformat(), True),
        (None, True),
        ("", True),
        ("not a date", True),
        ("2026-10-11T12:00:00", False),
    ])
    def test_a_date_is_old_enough_or_not(self, stamp, old):
        assert older_than(stamp, 3, NOW) is old

    @pytest.mark.parametrize("extra,expected", [
        (None, {}), ("", {}), ("{not json", {}), ("[1, 2]", {}),
        ('{"page_checked": "x"}', {"page_checked": "x"}),
    ])
    def test_the_extra_column_is_always_a_dict(self, extra, expected):
        assert row_extra(SimpleNamespace(extra=extra)) == expected


class TestTheRow:
    def test_the_first_time_is_told_and_remembered(self):
        row = SimpleNamespace(status="unknown", extra=None)
        assert track_missing_base(row, NOW) == "failed:missing_base"
        assert row.status == "failed:missing_base"
        assert json.loads(row.extra)["missing_base_reported"] == NOW.isoformat()

    def test_the_next_run_is_quiet_and_keeps_the_date(self):
        row = SimpleNamespace(status="unknown", extra=None)
        track_missing_base(row, NOW)
        assert track_missing_base(row, NOW + timedelta(hours=12)) == "skipped:missing_base"
        assert json.loads(row.extra)["missing_base_reported"] == NOW.isoformat()

    def test_three_weeks_later_it_is_told_again(self):
        row = SimpleNamespace(status="unknown", extra=None)
        track_missing_base(row, NOW)
        later = NOW + timedelta(days=21, hours=1)
        assert track_missing_base(row, later) == "failed:missing_base"
        assert json.loads(row.extra)["missing_base_reported"] == later.isoformat()

    def test_other_extra_keys_are_kept(self):
        row = SimpleNamespace(status="unknown", extra=json.dumps({"external_store": "gog"}))
        track_missing_base(row, NOW)
        assert json.loads(row.extra)["external_store"] == "gog"

    def test_a_dry_run_shows_it_without_using_up_the_report(self):
        row = SimpleNamespace(status="unknown", extra=None)
        assert track_missing_base(row, NOW, remember=False) == "failed:missing_base"
        assert track_missing_base(row, NOW + timedelta(hours=12)) == "failed:missing_base"

    @pytest.mark.parametrize("extra", ["{not json", "[1, 2]", ""])
    def test_a_broken_extra_is_replaced(self, extra):
        row = SimpleNamespace(status="unknown", extra=extra)
        assert track_missing_base(row, NOW) == "failed:missing_base"
        assert "missing_base_reported" in json.loads(row.extra)


class TestBothStoresUseIt:
    @pytest.mark.parametrize("store", ["steam.py", "epic.py"])
    def test_the_store_asks_before_telling(self, store):
        source = (ROOT / "src" / "stores" / store).read_text(encoding="utf-8")
        assert 'notify_game["status"] = track_missing_base(obj, remember=not cfg.dryrun)' in source
        assert 'notify_game["status"] = "failed:missing_base"' not in source

    def test_steam_checks_ownership_before_the_base_game(self):
        # The base-game check leaves the DLC page, so a DLC you own was once read as missing its base (#78 review).
        source = (ROOT / "src" / "stores" / "steam.py").read_text(encoding="utf-8")
        claim = source.split("async def _claim_game", 1)[1].split("\n    async def ", 1)[0]
        assert claim.index("game_area_already_owned") < claim.index("_ensure_base_game(")

    def test_steam_keeps_the_dlc_in_the_database(self):
        source = (ROOT / "src" / "stores" / "steam.py").read_text(encoding="utf-8")
        block = source.split("if not has_base_game:", 1)[1].split("return", 1)[0]
        assert "get_or_create(" in block and "game_id=app_id" in block
