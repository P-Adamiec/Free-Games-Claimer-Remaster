"""The human checks each store showed, kept two weeks so a user can see and send how often a store asks (2026-10-30)."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from src.core import claimer as claimer_module
from src.core import human_checks

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def test_a_check_is_saved_with_its_store_kind_and_page():
    human_checks.record("epic", True, "https://store.epicgames.com/en-US/p/galaxy-kart (Galaxy Kart)", now=NOW)
    assert human_checks.load() == [{"at": "2026-10-30T12:00:00+00:00", "store": "epic", "kind": "puzzle",
                                    "where": "https://store.epicgames.com/en-US/p/galaxy-kart (Galaxy Kart)"}]


def test_old_checks_go_and_the_file_never_grows_past_its_limit():
    human_checks.record("itchio", False, now=NOW - timedelta(days=20))
    for minute in range(human_checks.MAX_ENTRIES + 5):
        human_checks.record("epic", False, now=NOW + timedelta(minutes=minute))
    entries = human_checks.load()
    assert len(entries) == human_checks.MAX_ENTRIES and {e["store"] for e in entries} == {"epic"}


def test_the_log_line_counts_the_last_seven_days_per_store():
    human_checks.record("epic", True, now=NOW - timedelta(days=8))
    human_checks.record("epic", True, now=NOW - timedelta(days=1))
    human_checks.record("epic", False, now=NOW - timedelta(hours=3))
    human_checks.record("itchio", False, now=NOW - timedelta(hours=1))
    assert human_checks.week_summary(now=NOW) == "epic 2 (1 puzzle), itchio 1"


def test_nothing_to_say_without_checks_or_with_a_broken_file():
    assert human_checks.week_summary(now=NOW) == ""
    human_checks._path().write_text("not json", encoding="utf-8")
    assert human_checks.load() == [] and human_checks.week_summary(now=NOW) == ""


def test_every_check_the_shared_wait_meets_is_recorded_once(monkeypatch):
    recorded = []
    monkeypatch.setattr(claimer_module, "record_check", lambda store, puzzle, where: recorded.append((store, puzzle, where)))
    looks = [True, True, False]

    class Stub:
        store_name, logger, page = "epic", logging.getLogger("test"), None

        async def _human_challenge_present(self):
            return looks.pop(0)

        async def _human_challenge_reason(self):
            return "frame: https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html"

        async def take_screenshot(self, name, failed=True):
            pass

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    assert asyncio.run(claimer_module.BaseClaimer._wait_out_challenge(Stub(), "Epic", settle=12, ask=False)) is True
    assert recorded == [("epic", True, "")]


def test_the_file_holds_no_account_data():
    human_checks.record("epic", False, "https://www.epicgames.com/id/login (Sign in)", now=NOW)
    assert set(json.loads(human_checks._path().read_text(encoding="utf-8"))[0]) == {"at", "store", "kind", "where"}
