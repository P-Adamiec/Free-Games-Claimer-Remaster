"""AliExpress coin/check-in payload parsing.

The wallet balance arrives as a [{"name": …, "value": …}] list, the check-in
calendar as a plain nested object, the parser has to read both, or the streak
and tomorrow's reward silently disappear (v1.5 bug).
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.stores.aliexpress import (
    AliExpressClaimer,
    _as_int,
    _field_by_leaf,
    _flatten_payload,
)

# Shape of a real mtop.aliexpress.coin.execute response (values from a live run).
COIN_EXECUTE = {
    "success": True,
    "data": [
        {"name": "userCoinsNum", "value": 985},
        {"name": "defaultCoinsNum", "value": 100},
        {"name": "valueMoneyFormat", "value": json.dumps({"structure": {"cent": 3843, "currencyCode": "PLN"}})},
    ],
}

# Shape of a check-in response: nested, no name/value pairs at all.
SIGN_LIST = {
    "success": True,
    "data": {
        "continuousDays": 5,
        "signInfo": {"todayCoins": 50, "tomorrowCoins": 72},
        "dayList": [{"day": "2026-07-26", "coins": 50, "today": True}],
    },
}


def _sign_node(distance, seq, signed, coins=50):
    """One day of the real check-in calendar (shape taken from a live capture)."""
    return {
        "calendarDayDistance": distance,
        "signResultList": [{
            "sequenceNumber": seq,
            "signSuccess": signed,
            "prizeInfoList": [{"prizeType": "coins", "prizeAmount": coins}],
        }],
    }


def _calendar(*nodes, inner=None):
    """Full mtop envelope, as captured: {api, data: {data: {...}, success}, ret, v}."""
    payload = {"signQuerySequenceNodeList": [{"dailySignNodeList": list(nodes)}]} if inner is None else inner
    return {
        "api": "mtop.aliexpress.coin.channel.sign.list",
        "data": {"data": payload, "success": True},
        "ret": ["SUCCESS::接口调用成功"],
        "v": "1.0",
    }


# The calendar as AliExpress actually returns it: today is distance 0, tomorrow 1.
REAL_CALENDAR = _calendar(
    _sign_node(-2, 13, True), _sign_node(-1, 14, True),
    _sign_node(0, 15, False), _sign_node(1, 16, False), _sign_node(2, 17, False),
)

# Current sign.list shape from ae_coin_api.json. The check-in offer shown by
# the UI is 15; widgetCoinsInfo.coins=5 belongs to a separate widget reward.
CURRENT_SIGN_LIST = _calendar(inner={
    "signQuerySequenceNodeList": [{
        "dailySignNodeList": [{
            "calendarDayDistance": 0,
            "signResultList": [{
                "issuePrizeSuccess": False,
                "signSuccess": False,
                "prizeInfoList": [{
                    "dateTag": "commonNodePrize",
                    "prizeType": "coins",
                    "prizeAmount": 15,
                    "multiple": 1,
                }],
            }],
            "widgetCoinsInfo": {"coins": 5, "status": "restrictedReceive"},
        }],
    }],
    "titleInfo": {
        "subTitleBeforeSign": {
            "multilingualContent": "Faça check-in e ganhe: {0} moeda(s)",
            "variablelist": ["15"],
        },
    },
})


def _claimer(*payloads):
    claimer = AliExpressClaimer()
    claimer._coin_payloads = [
        {"api": api, "url": api, "fields": _flatten_payload(raw), "body": json.dumps(raw)}
        for api, raw in payloads
    ]
    return claimer


class TestFlattenPayload:
    def test_reads_name_value_pairs(self):
        fields = _flatten_payload(COIN_EXECUTE)
        assert _as_int(_field_by_leaf(fields, "userCoinsNum")) == 985
        assert _as_int(_field_by_leaf(fields, "defaultCoinsNum")) == 100

    def test_name_value_entries_are_not_indexed(self):
        # "data[0].userCoinsNum" would break every lookup by field name.
        assert "data.userCoinsNum" in _flatten_payload(COIN_EXECUTE)

    def test_parses_json_encoded_strings(self):
        fields = _flatten_payload(COIN_EXECUTE)
        assert _as_int(_field_by_leaf(fields, "cent")) == 3843

    def test_reads_plain_nested_objects(self):
        fields = _flatten_payload(SIGN_LIST)
        assert fields["data.continuousDays"] == 5
        assert fields["data.signInfo.tomorrowCoins"] == 72
        assert fields["data.dayList[0].coins"] == 50

    def test_survives_deep_nesting(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}
        assert isinstance(_flatten_payload(deep), dict)

    def test_handles_empty_and_scalar_input(self):
        assert _flatten_payload({}) == {}
        assert _flatten_payload([]) == {}


class TestAsInt:
    def test_accepts_numbers_and_numeric_text(self):
        assert _as_int(5) == 5
        assert _as_int("5") == 5
        assert _as_int("5 days") == 5
        assert _as_int(5.9) == 5

    def test_rejects_bools_and_junk(self):
        assert _as_int(True) is None
        assert _as_int(None) is None
        assert _as_int("no digits here") is None


class TestCheckinCalendar:
    """The calendar is the authoritative source; the live page has no named streak field."""

    def test_reads_streak_and_tomorrow_from_the_real_shape(self):
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", REAL_CALENDAR))._extract_checkin_info_from_api()
        assert info == {"streak": 15, "tomorrow": 50}

    def test_tomorrow_is_the_prize_not_the_day_number(self):
        # The day counter (16) sits right next to the prize; reporting it would be wrong.
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", REAL_CALENDAR))._extract_checkin_info_from_api()
        assert info["tomorrow"] == 50

    def test_a_missed_day_invalidates_the_streak_counter(self):
        broken = _calendar(_sign_node(-2, 13, True), _sign_node(-1, 14, False),
                           _sign_node(0, 15, False), _sign_node(1, 16, False))
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", broken))._extract_checkin_info_from_api()
        assert info["streak"] is None
        assert info["tomorrow"] == 50

    def test_calendar_without_tomorrow_still_reports_the_streak(self):
        today_only = _calendar(_sign_node(-1, 14, True), _sign_node(0, 15, False))
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", today_only))._extract_checkin_info_from_api()
        assert info["streak"] == 15
        assert info["tomorrow"] is None

    def test_malformed_calendar_is_ignored(self):
        for broken in (_calendar(inner={"signQuerySequenceNodeList": "nope"}), _calendar(inner={})):
            info = _claimer(("mtop.aliexpress.coin.channel.sign.list", broken))._extract_checkin_info_from_api()
            assert info == {"streak": None, "tomorrow": None}


class TestCurrentCheckinState:
    def test_reads_main_offer_without_confusing_widget_reward(self):
        state = _claimer(
            ("mtop.aliexpress.coin.channel.sign.list", CURRENT_SIGN_LIST),
        )._extract_checkin_state_from_api()

        assert state == {
            "loaded": True,
            "claimed": False,
            "todayCoins": 15,
            "widgetCoins": 5,
            "widgetStatus": "restrictedReceive",
            "rewardSource": "today prizeInfoList",
        }

    def test_uses_before_sign_subtitle_as_reward_fallback(self):
        payload = _calendar(inner={
            "signQuerySequenceNodeList": [{
                "dailySignNodeList": [{
                    "calendarDayDistance": 0,
                    "signResultList": [{"signSuccess": False, "prizeInfoList": []}],
                    "widgetCoinsInfo": {"coins": 5, "status": "restrictedReceive"},
                }],
            }],
            "titleInfo": {"subTitleBeforeSign": {"variablelist": ["15"]}},
        })

        state = _claimer(
            ("mtop.aliexpress.coin.channel.sign.list", payload),
        )._extract_checkin_state_from_api()

        assert state["todayCoins"] == 15
        assert state["rewardSource"] == "subTitleBeforeSign.variablelist"

    def test_today_is_selected_by_calendar_distance(self):
        payload = _calendar(
            _sign_node(-1, 14, True, 10),
            _sign_node(0, 15, False, 15),
            _sign_node(1, 16, False, 20),
        )
        state = _claimer(
            ("mtop.aliexpress.coin.channel.sign.list", payload),
        )._extract_checkin_state_from_api()

        assert state["todayCoins"] == 15
        assert state["claimed"] is False

    def test_api_state_marks_widget_loaded_when_dom_has_no_button(self):
        class EmptyDomPage:
            async def evaluate(self, _script):
                return json.dumps({
                    "claimed": False,
                    "btnText": None,
                    "earnText": None,
                    "todayCoins": None,
                })

        claimer = _claimer(("mtop.aliexpress.coin.channel.sign.list", CURRENT_SIGN_LIST))
        claimer.page = EmptyDomPage()
        state = asyncio.run(claimer._read_checkin_state())

        assert state["loaded"] is True
        assert state["btnText"] is None
        assert state["todayCoins"] == 15
        assert state["detectedBy"] == "sign.list"

    def test_dom_amount_keeps_ae_min_coins_guard_authoritative(self):
        class FlaggedDomPage:
            async def evaluate(self, _script):
                return json.dumps({
                    "claimed": False,
                    "btnText": "Collect 1",
                    "earnText": None,
                    "todayCoins": 1,
                })

        claimer = _claimer(("mtop.aliexpress.coin.channel.sign.list", CURRENT_SIGN_LIST))
        claimer.page = FlaggedDomPage()
        state = asyncio.run(claimer._read_checkin_state())

        assert state["loaded"] is True
        assert state["todayCoins"] == 1
        assert state["detectedBy"] == "dom+sign.list"


class TestLiveCoinCaptureControlFlow:
    def test_direct_coin_navigation_keeps_one_cdp_session(self):
        class RecordingPage:
            session_id = "coin-session"

            def __init__(self):
                self.commands = []

            async def send(self, command):
                self.commands.append(next(command))

            async def get(self, _url):
                raise AssertionError("coin navigation must not use Tab.get()")

        claimer = AliExpressClaimer()
        claimer.page = RecordingPage()
        asyncio.run(claimer._navigate_to_coins_directly())

        methods = [command["method"] for command in claimer.page.commands]
        assert methods == ["Network.enable", "Page.navigate"]
        assert claimer._coin_network_session == "coin-session"
        assert claimer.page.session_id == "coin-session"

    def test_response_body_is_skipped_after_session_change(self):
        class RecordingPage:
            session_id = "first-session"

            def __init__(self):
                self.commands = []

            async def send(self, command):
                self.commands.append(next(command))

        claimer = AliExpressClaimer()
        claimer.page = RecordingPage()
        claimer._coin_network_session = "first-session"
        response = SimpleNamespace(
            request_id="request-1",
            timestamp=10,
            response=SimpleNamespace(
                url="https://acs.aliexpress.com/h5/mtop.aliexpress.coin.execute/1.0/",
            ),
        )
        asyncio.run(claimer._on_coin_response(response))

        claimer.page.session_id = "second-session"
        asyncio.run(claimer._on_coin_loading_finished(SimpleNamespace(
            request_id="request-1",
            timestamp=11,
        )))

        assert claimer.page.commands == []
        assert claimer._coin_payloads == []

    def test_preflight_response_is_not_queued_for_body_capture(self):
        class Preflight:
            def __str__(self):
                return "ResourceType.PREFLIGHT"

        claimer = AliExpressClaimer()
        claimer.page = SimpleNamespace(session_id="coin-session")
        response = SimpleNamespace(
            request_id="preflight-1",
            timestamp=10,
            type_=Preflight(),
            response=SimpleNamespace(
                url="https://acs.aliexpress.com/h5/mtop.aliexpress.coin.channel.sign.list/1.0/",
            ),
        )

        asyncio.run(claimer._on_coin_response(response))

        assert claimer._coin_reqs == {}

    def test_retry_wait_polls_until_offer_becomes_collectable(self):
        claimer = AliExpressClaimer()
        states = iter([
            {"claimed": False, "btnText": None, "todayCoins": None},
            {"claimed": False, "btnText": "Coletar 15", "todayCoins": 15},
        ])
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async def fake_state():
            return next(states)

        claimer.sleep = fake_sleep
        claimer._read_checkin_state = fake_state
        state = asyncio.run(claimer._poll_checkin_during_retry_wait(
            timeout=30,
            min_coins=5,
            interval=10,
        ))

        assert sleeps == [10, 10]
        assert state["btnText"] == "Coletar 15"

    def test_retry_wait_does_not_accept_low_coin_offer(self):
        claimer = AliExpressClaimer()
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async def flagged_state():
            return {"claimed": False, "btnText": "Coletar 1", "todayCoins": 1}

        claimer.sleep = fake_sleep
        claimer._read_checkin_state = flagged_state
        state = asyncio.run(claimer._poll_checkin_during_retry_wait(
            timeout=25,
            min_coins=5,
            interval=10,
        ))

        assert sleeps == [10, 10, 5]
        assert state == {}

    def test_dom_fallback_uses_geometric_visibility(self):
        class EmptyDomPage:
            def __init__(self):
                self.script = ""

            async def evaluate(self, script):
                self.script = script
                return json.dumps({
                    "claimed": False,
                    "btnText": None,
                    "earnText": None,
                    "todayCoins": None,
                })

        claimer = AliExpressClaimer()
        claimer.page = EmptyDomPage()
        asyncio.run(claimer._read_checkin_state())

        assert "getBoundingClientRect" in claimer.page.script
        assert "getComputedStyle" in claimer.page.script
        assert "offsetParent" not in claimer.page.script


class TestLoginState:
    def test_coin_init_reports_authenticated_session(self):
        payload = {
            "data": {"groupResponse": {"alreadyLogin": True, "guest": False}},
            "success": True,
        }
        claimer = _claimer(("mtop.aliexpress.coin.channel.init", payload))

        assert claimer._login_state_from_coin_api() is True

    def test_coin_init_reports_logged_out_session(self):
        payload = {
            "data": {"groupResponse": {"alreadyLogin": False, "guest": True}},
            "success": True,
        }
        claimer = _claimer(("mtop.aliexpress.coin.channel.init", payload))

        assert claimer._login_state_from_coin_api() is False

    def test_unknown_dom_uses_authenticated_coin_api_state(self):
        class UnknownDomPage:
            async def evaluate(self, _script):
                return "unknown"

        payload = {
            "data": {"groupResponse": {"alreadyLogin": True}},
            "success": True,
        }
        claimer = _claimer(("mtop.aliexpress.coin.channel.init", payload))
        claimer.page = UnknownDomPage()

        assert asyncio.run(claimer._is_logged_in()) is True

    def test_visible_login_form_wins_over_prior_api_state(self):
        class LoggedOutDomPage:
            async def evaluate(self, _script):
                return "logged_out"

        payload = {
            "data": {"groupResponse": {"alreadyLogin": True}},
            "success": True,
        }
        claimer = _claimer(("mtop.aliexpress.coin.channel.init", payload))
        claimer.page = LoggedOutDomPage()

        assert asyncio.run(claimer._is_logged_in()) is False


class TestCheckinInfo:
    def test_reads_streak_and_tomorrow_from_calendar(self):
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", SIGN_LIST))._extract_checkin_info_from_api()
        assert info == {"streak": 5, "tomorrow": 72}

    def test_prefers_the_response_from_the_collect_itself(self):
        fresh = {"success": True, "data": {"signDays": 6, "nextDayCoins": 80}}
        info = _claimer(
            ("mtop.aliexpress.coin.channel.sign.list", SIGN_LIST),
            ("mtop.aliexpress.coin.channel.sign.execute", fresh),
        )._extract_checkin_info_from_api()
        assert info == {"streak": 6, "tomorrow": 80}

    def test_no_false_positives_without_checkin_fields(self):
        info = _claimer(("mtop.aliexpress.coin.execute", COIN_EXECUTE))._extract_checkin_info_from_api()
        assert info == {"streak": None, "tomorrow": None}

    def test_nothing_captured(self):
        assert _claimer()._extract_checkin_info_from_api() == {"streak": None, "tomorrow": None}

    @pytest.mark.parametrize("field", ["nextDayIndex", "nextDayStatus", "nextSignDay", "tomorrowDate"])
    def test_next_day_fields_that_are_not_coins_are_ignored(self, field):
        # Showing a day number as "tomorrow 12 🪙" is worse than showing nothing.
        payload = {"success": True, "data": {field: 12}}
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", payload))._extract_checkin_info_from_api()
        assert info["tomorrow"] is None

    @pytest.mark.parametrize("field", ["tomorrowCoins", "nextDayCoins", "nextDayRewardAmount", "coinsTomorrow"])
    def test_next_day_coin_fields_are_read(self, field):
        payload = {"success": True, "data": {field: 72}}
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", payload))._extract_checkin_info_from_api()
        assert info["tomorrow"] == 72

    @pytest.mark.parametrize("field", ["continuousDays", "consecutiveDays", "signDays", "checkInDays", "streak"])
    def test_streak_fields_are_read(self, field):
        payload = {"success": True, "data": {field: 7}}
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", payload))._extract_checkin_info_from_api()
        assert info["streak"] == 7

    @pytest.mark.parametrize("field", ["days", "totalDays", "dayIndex"])
    def test_bare_day_counters_are_not_treated_as_a_streak(self, field):
        payload = {"success": True, "data": {field: 7}}
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", payload))._extract_checkin_info_from_api()
        assert info["streak"] is None

    def test_one_field_cannot_fill_both_slots(self):
        # A key like "nextDaySignCoins" could match both patterns; it may not report twice.
        payload = {"success": True, "data": {"continuousSignDaysNextDayCoins": 9}}
        info = _claimer(("mtop.aliexpress.coin.channel.sign.list", payload))._extract_checkin_info_from_api()
        assert (info["streak"], info["tomorrow"]) != (9, 9)


class TestStatusText:
    def test_every_number_is_labelled(self):
        status = AliExpressClaimer()._format_checkin_status(50, {"streak": 5, "tomorrow": 72}, 985)
        assert status == "claimed 50 🪙, streak 5 days, tomorrow 72 🪙, balance 985 🪙"

    def test_omits_unknown_parts_instead_of_faking_them(self):
        status = AliExpressClaimer()._format_checkin_status(50, {"streak": None, "tomorrow": None}, 985)
        assert status == "claimed 50 🪙, balance 985 🪙"

    def test_singular_day(self):
        status = AliExpressClaimer()._format_checkin_status(10, {"streak": 1, "tomorrow": None}, None)
        assert status == "claimed 10 🪙, streak 1 day"

    def test_large_balance_is_grouped(self):
        status = AliExpressClaimer()._format_checkin_status(50, {}, 1040)
        assert status == "claimed 50 🪙, balance 1,040 🪙"

    def test_no_em_dash_anywhere_in_the_line(self):
        status = AliExpressClaimer()._format_checkin_status(50, {"streak": 5, "tomorrow": 72}, 1040)
        assert "—" not in status
