"""Humble Choice: which months count, which keys get revealed, and what the answers mean.

The orders below are shaped like the ones Humble's own API hands the keys page:
a Choice month is an order whose product is a subscription, and each game in it
is a third-party key (TPK) that gains a `redeemed_key_val` only once revealed.
"""

from src.stores.humble import (
    choose_outcome,
    extract_tpks,
    is_choice_order,
    order_created,
    order_is_legacy,
    plan_reveals,
    reveal_outcome,
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

    def test_a_month_before_february_2022_is_legacy(self):
        assert order_is_legacy(_order(created="2021-12-07T18:00:00")) is True
        assert order_is_legacy(_order(created="2022-01-04T18:00:00")) is True

    def test_the_single_plan_months_are_not_legacy(self):
        assert order_is_legacy(_order(created="2022-02-01T18:00:00")) is False
        assert order_is_legacy(_order()) is False

    def test_an_unreadable_date_counts_as_current(self):
        assert order_is_legacy(_order(created="not a date")) is False
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
