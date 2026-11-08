"""A captcha on a giveaway's last run is worth asking you about even with EG_CAPTCHA_ASK off (owner, 2026-10-30)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.core import run_state
from src.core.claimer import giveaway_end
from src.stores.epic_fab import parse_free_listings
from src.stores.epic_mobile import parse_free_games

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
PUZZLE = "checkout frame, frame: https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html"


@pytest.mark.parametrize("value,expected", [
    ("2026-11-05T16:00:00.000Z", datetime(2026, 11, 5, 16, tzinfo=timezone.utc)),
    ("2026-11-05 23:59:00", datetime(2026, 11, 5, 23, 59, tzinfo=timezone.utc)),
    ("N/A", None), ("", None), (None, None)])
def test_every_source_s_end_date_is_read(value, expected):
    assert giveaway_end(value) == expected


@pytest.fixture
def next_run_in_12_hours(monkeypatch):
    """The next run 12 hours after the real clock, for the flows that read the time themselves."""
    monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
    monkeypatch.setattr(run_state, "run_started", lambda: None)


def _soon(hours):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


@pytest.mark.parametrize("hours,last", [(3, True), (12, True), (13, False), (None, False)])
def test_a_giveaway_that_ends_before_the_next_run_is_on_its_last_chance(monkeypatch, hours, last):
    monkeypatch.setattr(run_state, "_next_run_source", lambda: NOW + timedelta(hours=12))
    monkeypatch.setattr(run_state, "run_started", lambda: None)
    ends = None if hours is None else NOW + timedelta(hours=hours)
    assert run_state.ends_before_next_run(ends, now=NOW) is last


def test_without_a_next_run_a_known_end_is_the_last_chance(monkeypatch):
    monkeypatch.setattr(run_state, "_next_run_source", None)
    assert run_state.ends_before_next_run(NOW, now=NOW) is True


def test_mobile_and_fab_carry_their_end_dates():
    claim = {"purchaseType": "Claim", "price": {"decimalPrice": 0}, "discount": {"discountEndDate": "2026-11-10T18:00:00.000Z"}}
    section = {"type": "freeGame", "offers": [{"content": {"title": "Galaxy Kart", "mapping": {"slug": "galaxy-kart-android-135821"},
                                                           "purchase": [claim]}}]}
    assert parse_free_games({"data": [section]}, "android")[0]["ends"] == "2026-11-10T18:00:00.000Z"
    blade = {"isLimitedFreeContent": True, "blockContentType": "listings", "tiles": [{"listing": {
        "uid": "abc", "title": "Dragon Cave", "licenses": [{"priceTier": {"discountEndDate": "2026-11-03T14:59:00Z"}}]}}]}
    from src.stores import epic_fab
    blade["blockContentType"] = epic_fab.FREE_BLOCK_TYPE
    assert parse_free_listings(blade)[0]["ends"] == "2026-11-03T14:59:00Z"


def _epic_checkout(monkeypatch, ends):
    from src.stores import epic as epic_module
    from src.stores.epic import EpicGamesClaimer
    monkeypatch.setattr(epic_module.cfg, "eg_captcha_ask", False)
    waited = []

    async def reason():
        return PUZZLE

    async def wait_out(*args, **kwargs):
        waited.append((kwargs["settle"], kwargs["ask"]))
        return False

    stub = type("Stub", (), {"_meeting": False, "_with_you": False, "_claiming_url": "https://store.epicgames.com/p/galaxy-kart", "_seen_at_prompt": None, "_book": EpicGamesClaimer._book, "store_name": "epic", "_checkout_challenge_reason": staticmethod(reason),
                             "_checkout_challenge_present": staticmethod(reason), "_wait_out_challenge": staticmethod(wait_out)})()
    stub._checkout_asked, stub._checkout_puzzles, stub._checkout_stuck, stub._ends_now = set(), set(), set(), ends
    stub._checkout_handed = {}
    asyncio.run(EpicGamesClaimer._clear_checkout_challenge(stub, "Galaxy Kart (Android)"))
    return waited


def test_epic_asks_you_on_the_last_run_even_with_eg_captcha_ask_off(next_run_in_12_hours, monkeypatch):
    assert _epic_checkout(monkeypatch, _soon(5)) == [(12, True)]


def test_epic_leaves_it_for_the_next_run_while_there_is_one(next_run_in_12_hours, monkeypatch):
    assert _epic_checkout(monkeypatch, _soon(96)) == [(12, False)]


def test_fab_asks_you_on_the_last_run_even_with_eg_captcha_ask_off(next_run_in_12_hours, monkeypatch):
    from src.stores import epic_fab
    from src.stores.epic_fab import FabClaimer
    monkeypatch.setattr(epic_fab.cfg, "eg_captcha_ask", False)
    waited = []

    async def reason():
        return PUZZLE

    async def wait_out(label, **kwargs):
        waited.append((kwargs["settle"], kwargs["ask"]))
        return False

    stub = type("Stub", (), {"_meeting": False, "_with_you": False, "_claiming": {"uid": "abc"}, "_seen_at_prompt": None, "_book": FabClaimer._book, "store_name": "fab", "_checkout_puzzle": False, "_ends_now": _soon(2)})()
    stub._checkout_challenge_reason, stub._wait_out_challenge = reason, wait_out
    asyncio.run(FabClaimer._clear_checkout_challenge(stub, "Dragon Cave"))
    assert waited == [(12, True)]


def test_the_stop_after_a_puzzle_spares_a_game_on_its_last_run():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "src" / "stores"
    epic = (root / "epic.py").read_text(encoding="utf-8")
    assert "last_chance = ends_before_next_run(self._ends_now)" in epic
    assert "if self._checkout_puzzles and not cfg.eg_captcha_ask and not last_chance:" in epic
    assert "if self._checkout_puzzle and not cfg.eg_captcha_ask and not ends_before_next_run(ends):" in \
        (root / "epic_fab.py").read_text(encoding="utf-8")
    # After nobody answered, a single run (RUN_ONCE, Ofelia, cron) holds even a known end: it cannot tell a last run.
    assert "not lost_before_next_run(self._ends_now)" in epic


def test_epic_s_own_end_date_wins_over_gamerpower_s():
    from src.stores.epic import game_ends
    free = [{"url": "https://store.epicgames.com/en-US/p/x-game", "ends": "2026-11-05T16:00:00.000Z"},
            {"url": "https://store.epicgames.com/en-US/p/no-date", "ends": ""}]
    extra = [{"final_url": "https://store.epicgames.com/en-US/p/x-game", "end_date": "N/A"},
             {"final_url": "https://store.epicgames.com/en-US/p/no-date", "end_date": "2026-11-02 23:59:00"},
             {"final_url": "https://store.epicgames.com/en-US/p/only-gp", "end_date": "2026-11-03 23:59:00"}]
    ends = game_ends(free, extra)
    assert ends["x-game"] == datetime(2026, 11, 5, 16, tzinfo=timezone.utc)
    # GamerPower names only the day: on 2.11 it gave Sorry We're Closed 5.11 23:59, Epic ended it at 16:00 UTC.
    assert ends["no-date"] == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert ends["only-gp"] == datetime(2026, 11, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("run_at,next_at,last", [
    (datetime(2026, 11, 7, 10, 40, tzinfo=timezone.utc), datetime(2026, 11, 7, 22, 40, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 7, 22, 40, tzinfo=timezone.utc), datetime(2026, 11, 8, 10, 40, tzinfo=timezone.utc), True)])
def test_a_gamerpower_find_gets_its_last_chance_the_evening_before(monkeypatch, run_at, next_at, last):
    # Hunt For Quest: GamerPower "2026-11-08 23:59:00"; read as 23:59 the run of 8.11 10:40 would have left it.
    from src.stores.epic import game_ends
    ends = game_ends([], [{"final_url": "https://store.epicgames.com/p/epic-world-hunt-for-quest-4500b1",
                           "end_date": "2026-11-08 23:59:00"}])["epic-world-hunt-for-quest-4500b1"]
    monkeypatch.setattr(run_state, "_next_run_source", lambda: next_at)
    monkeypatch.setattr(run_state, "run_started", lambda: None)
    assert run_state.ends_before_next_run(ends, now=run_at) is last


def test_gamerpower_hands_its_end_date_on():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
    assert '"end_date": item.get("end_date", "") or ""' in source


def test_the_next_run_needs_as_long_to_reach_the_store_as_this_one(monkeypatch):
    # Next run 15:55, Steam first: it reaches Epic at about 16:05, after a 16:00 end.
    monkeypatch.setattr(run_state, "_next_run_source", lambda: NOW + timedelta(hours=12))
    monkeypatch.setattr(run_state, "run_started", lambda: NOW - timedelta(minutes=10))
    assert run_state.ends_before_next_run(NOW + timedelta(hours=12, minutes=5), now=NOW) is True
    assert run_state.ends_before_next_run(NOW + timedelta(hours=12, minutes=15), now=NOW) is False


def test_a_trigger_due_while_this_run_works_is_no_next_run(monkeypatch):
    monkeypatch.setattr(run_state, "_next_run_source", lambda: NOW + timedelta(minutes=10))
    assert run_state.ends_before_next_run(NOW + timedelta(days=3), now=NOW) is True

