"""Humble Choice: which months count, which keys get revealed, and what the answers mean.

The orders below are shaped like the ones Humble's own API hands the keys page:
a Choice month is an order whose product is a subscription, and each game in it
is a third-party key (TPK) that gains a `redeemed_key_val` only once revealed.
"""

from datetime import datetime, timezone

from src.stores.humble import (
    choose_outcome,
    detail_facts,
    extract_tpks,
    facts_from_cache,
    facts_to_cache,
    is_choice_order,
    order_created,
    owned_in_plan,
    pick_month,
    plan_reveals,
    reveal_outcome,
    summary_facts,
    tpk_state,
)


def _order(created="2026-10-06T17:00:00", machine="october_2026_choice", category="subscription", tpks=()):
    return {
        "gamekey": "abc123",
        "created": created,
        "product": {"machine_name": machine, "human_name": "October 2026 Choice", "category": category},
        "tpkd_dict": {"all_tpks": list(tpks)},
    }


def _tpk(name="A Game", machine="agame_choice_steam", key_type="steam", index=0, key=None, expired=False):
    return {
        "human_name": name,
        "machine_name": machine,
        "key_type": key_type,
        "key_index": index,
        "redeemed_key_val": key,
        "is_expired": expired,
        "expiry_date": "2027-11-03T17:00:00" if not expired else "2020-01-01T00:00:00",
    }


class TestWhichMonthsCount:
    def test_a_subscription_product_is_a_choice_month(self):
        assert is_choice_order(_order()) is True

    def test_a_month_named_choice_counts_without_the_category(self):
        assert is_choice_order(_order(category="bundle")) is True

    def test_an_ordinary_bundle_is_not_a_choice_month(self):
        order = _order(machine="great_games_bundle", category="bundle")
        order["product"]["human_name"] = "Great Games Bundle"
        assert is_choice_order(order) is False

    def test_an_unreadable_date_reads_as_no_date(self):
        assert order_created(_order(created="not a date")) is None
        assert order_created(_order(created="")) is None

    def test_a_zulu_stamp_reads_as_utc(self):
        created = order_created(_order(created="2026-10-06T17:00:00Z"))
        assert created is not None and created.tzinfo is not None


class TestExtractTpks:
    def test_the_fields_the_run_decides_on(self):
        (tpk,) = extract_tpks(_order(tpks=[_tpk(key="XXXXX-YYYYY-ZZZZZ")]))
        assert tpk["gamekey"] == "abc123"
        assert tpk["title"] == "A Game"
        assert tpk["key_type"] == "steam"
        assert tpk["revealed_key"] == "XXXXX-YYYYY-ZZZZZ"
        assert tpk["expired"] is False

    def test_an_order_without_keys_gives_none(self):
        assert extract_tpks(_order()) == []
        assert extract_tpks({}) == []

    def test_a_bad_key_index_reads_as_zero(self):
        (tpk,) = extract_tpks(_order(tpks=[_tpk(index="soon")]))
        assert tpk["key_index"] == 0


class TestTpkState:
    def test_revealed_expired_waiting(self):
        assert tpk_state(extract_tpks(_order(tpks=[_tpk(key="K")]))[0]) == "revealed"
        assert tpk_state(extract_tpks(_order(tpks=[_tpk(expired=True)]))[0]) == "expired"
        assert tpk_state(extract_tpks(_order(tpks=[_tpk()]))[0]) == "waiting"


class TestPlanReveals:
    def test_only_waiting_keys_are_planned(self):
        tpks = extract_tpks(_order(tpks=[
            _tpk(name="Waiting"),
            _tpk(name="Done", machine="done_choice_steam", key="XXXXX-11111-22222"),
            _tpk(name="Too Late", machine="late_choice_steam", expired=True),
        ]))
        assert [t["title"] for t in plan_reveals(tpks)] == ["Waiting"]

    def test_the_steam_variant_of_a_platform_choice_wins(self):
        tpks = extract_tpks(_order(tpks=[
            _tpk(machine="agame_choice_epic_keyless", key_type="epic"),
            _tpk(machine="agame_choice_steam", key_type="steam"),
        ]))
        planned = plan_reveals(tpks)
        assert [t["machine_name"] for t in planned] == ["agame_choice_steam"]

    def test_an_epic_only_variant_is_still_planned(self):
        tpks = extract_tpks(_order(tpks=[_tpk(machine="agame_choice_epic_keyless", key_type="epic")]))
        assert len(plan_reveals(tpks)) == 1

    def test_the_same_variant_twice_is_revealed_once(self):
        tpks = extract_tpks(_order(tpks=[_tpk(), _tpk()]))
        assert len(plan_reveals(tpks)) == 1

    def test_two_different_games_are_both_planned(self):
        tpks = extract_tpks(_order(tpks=[
            _tpk(),
            _tpk(name="B Game", machine="bgame_choice_steam"),
        ]))
        assert len(plan_reveals(tpks)) == 2


class TestRevealOutcome:
    def test_success_is_a_reveal(self):
        assert reveal_outcome(200, {"success": True, "key": "XXXXX"}) == "revealed"

    def test_a_refused_session_stops_the_run(self):
        assert reveal_outcome(401, {}) == "blocked"
        assert reveal_outcome(403, {"success": False}) == "blocked"

    def test_already_revealed_is_its_own_answer(self):
        assert reveal_outcome(200, {"success": False, "errors": {"key": ["Key already redeemed"]}}) == "already"

    def test_anything_else_failed(self):
        assert reveal_outcome(200, {"success": False}) == "failed"
        assert reveal_outcome(500, None) == "failed"


class TestChooseOutcome:
    def test_success_or_already_chosen_counts(self):
        assert choose_outcome(200, {"success": True}) is True
        assert choose_outcome(200, {"success": False, "error": "Content already chosen"}) is True

    def test_a_plain_refusal_does_not(self):
        assert choose_outcome(403, {}) is False
        assert choose_outcome(200, {"success": False}) is False


class TestOwnedInPlan:
    """The Steam-library guard: which planned keys it holds back."""

    def _planned(self):
        return plan_reveals(extract_tpks(_order(tpks=[
            _tpk(name="A Game"),
            _tpk(name="B Game", machine="bgame_choice_steam"),
            _tpk(name="C Game", machine="cgame_choice_gog", key_type="gog"),
        ])))

    def test_a_steam_key_already_on_steam_is_held_back(self):
        # Owned titles arrive normalized, the form _normalize_title produces.
        held = owned_in_plan(self._planned(), {"agame"})
        assert [t["title"] for t in held] == ["A Game"]

    def test_only_steam_keys_can_be_held_back(self):
        held = owned_in_plan(self._planned(), {"agame", "bgame", "cgame"})
        assert [t["title"] for t in held] == ["A Game", "B Game"]

    def test_nothing_owned_holds_nothing_back(self):
        assert owned_in_plan(self._planned(), set()) == []
        assert owned_in_plan(self._planned(), None) == []


class TestSummaryFacts:
    """The month is picked from the order list alone; this reads one entry."""

    def test_a_list_entry_classifies_without_opening_the_order(self):
        fact = summary_facts(_order())
        assert fact["gamekey"] == "abc123"
        assert fact["choice"] is True
        assert fact["human_name"] == "October 2026 Choice"
        assert fact["created"] is not None and fact["created"].year == 2026

    def test_a_bundle_entry_is_not_a_choice_month(self):
        entry = _order(machine="great_games_bundle", category="bundle")
        entry["product"]["human_name"] = "Great Games Bundle"
        assert summary_facts(entry)["choice"] is False

    def test_an_entry_with_no_product_says_nothing(self):
        fact = summary_facts({"gamekey": "xyz789", "created": "2026-10-06T17:00:00"})
        assert fact["choice"] is None
        assert fact["human_name"] == ""

    def test_detail_facts_agree_with_the_summary_shape(self):
        fact = detail_facts(_order(), "abc123")
        assert fact["choice"] is True
        assert fact["created"] is not None


class TestPickMonth:
    """Only the most recent current-plan month is worked on; this picks it."""

    def _fact(self, name, created, choice=True):
        stamp = (datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
                 if created else None)
        return {"gamekey": name, "choice": choice, "created": stamp, "human_name": name}

    def test_the_newest_current_month_wins(self):
        facts = [self._fact("september", "2026-09-01T17:00:00"),
                 self._fact("october", "2026-10-06T17:00:00"),
                 self._fact("august", "2026-08-04T17:00:00")]
        latest, legacy, older = pick_month(facts)
        assert latest["human_name"] == "october"
        assert legacy == []
        assert older == 2

    def test_months_before_february_2022_are_legacy(self):
        facts = [self._fact("december 2021", "2021-12-07T18:00:00"),
                 self._fact("january 2022", "2022-01-04T18:00:00"),
                 self._fact("february 2022", "2022-02-01T18:00:00")]
        latest, legacy, older = pick_month(facts)
        assert [fact["human_name"] for fact in legacy] == ["december 2021", "january 2022"]
        assert latest["human_name"] == "february 2022"
        assert older == 0

    def test_no_current_month_picks_nothing(self):
        latest, legacy, older = pick_month([self._fact("december 2021", "2021-12-07T18:00:00")])
        assert latest is None
        assert len(legacy) == 1
        assert older == 0

    def test_an_unreadable_date_counts_as_current_but_ranks_oldest(self):
        facts = [self._fact("mystery", None),
                 self._fact("january 2023", "2023-01-03T17:00:00")]
        latest, legacy, older = pick_month(facts)
        assert legacy == []
        assert latest["human_name"] == "january 2023"
        assert older == 1

    def test_non_choice_orders_are_ignored(self):
        facts = [self._fact("a bundle", "2026-10-01T17:00:00", choice=False),
                 self._fact("october", "2026-10-06T17:00:00")]
        latest, legacy, older = pick_month(facts)
        assert latest["human_name"] == "october"
        assert legacy == [] and older == 0


class TestFactsCache:
    """Classifications are kept between runs; these are the kept shapes."""

    def _fact(self, name, created, choice=True):
        stamp = (datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
                 if created else None)
        return {"gamekey": name, "choice": choice, "created": stamp, "human_name": name}

    def test_a_round_trip_keeps_the_pick_the_same(self):
        facts = [self._fact("october", "2026-10-06T17:00:00"),
                 self._fact("may 2021", "2021-05-04T17:00:00"),
                 self._fact("a bundle", "2026-10-02T17:00:00", choice=False)]
        kept = facts_from_cache(facts_to_cache(facts))
        latest, legacy, older = pick_month(list(kept.values()))
        assert latest["human_name"] == "october"
        assert [fact["human_name"] for fact in legacy] == ["may 2021"]
        assert older == 0

    def test_facts_without_a_verdict_are_not_kept(self):
        fact = self._fact("mystery", "2026-10-06T17:00:00")
        fact["choice"] = None
        assert facts_to_cache([fact]) == {}

    def test_malformed_kept_entries_are_dropped_not_guessed(self):
        kept = facts_from_cache({
            "good": {"choice": True, "created": "2026-10-06T17:00:00", "human_name": "October"},
            "junk": "not a dict",
            "no verdict": {"created": "2026-10-06T17:00:00"},
        })
        assert list(kept) == ["good"]

    def test_a_kept_stamp_that_no_longer_parses_keeps_its_verdict(self):
        kept = facts_from_cache({"old": {"choice": True, "created": "someday", "human_name": "Old"}})
        assert kept["old"]["choice"] is True
        assert kept["old"]["created"] is None
