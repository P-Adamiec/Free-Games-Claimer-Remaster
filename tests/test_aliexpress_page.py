"""Telling a dead coin page apart from an unreadable widget, and reading today from the API.

Issue #33: both looked identical in the log, so the bot spent 28 minutes waiting out a page
AliExpress never intended to render.
"""

import json

import pytest

from src.stores.aliexpress import page_is_dead, today_from_payloads


def sign_list(nodes) -> dict:
    """A captured coin.channel.sign.list payload, shaped like the real one."""
    return {
        "api": "mtop.aliexpress.coin.channel.sign.list",
        "body": json.dumps({"data": {"data": {"signQuerySequenceNodeList": [{"dailySignNodeList": nodes}]}}}),
    }


def day(distance, signed=None, coins=None) -> dict:
    result = {}
    if signed is not None:
        result["signSuccess"] = signed
    if coins is not None:
        result["prizeInfoList"] = [{"prizeType": "coins", "prizeAmount": coins}]
    return {"calendarDayDistance": distance, "signResultList": [result]}


class TestDeadPage:
    """Measured live: scripts shipped (textContent 9480) but nothing painted (innerText 0)."""

    def test_the_real_dead_page(self):
        assert page_is_dead({"innerTextLen": 0, "textContentLen": 9480})

    def test_a_rendered_page_is_not_dead(self):
        assert not page_is_dead({"innerTextLen": 3985, "textContentLen": 151784})

    def test_a_page_that_rendered_a_little_still_counts_as_rendered(self):
        assert not page_is_dead({"innerTextLen": 400, "textContentLen": 9480})

    @pytest.mark.parametrize("health", [
        {},
        None,
        {"innerTextLen": -1, "textContentLen": -1},
        {"innerTextLen": 0, "textContentLen": 0},
        {"innerTextLen": 0},
    ])
    def test_unknown_or_empty_measurements_never_declare_it_dead(self, health):
        # Guessing "dead" would skip a check-in that might have worked.
        assert not page_is_dead(health)


class TestTodayFromApi:
    """Field names do not translate, which is the whole point of reading them."""

    def test_today_is_still_open(self):
        payloads = [sign_list([day(-1, signed=True, coins=50), day(0, signed=False, coins=70),
                               day(1, coins=90)])]
        assert today_from_payloads(payloads) == {"claimed": False, "coins": 70}

    def test_today_is_already_collected(self):
        assert today_from_payloads([sign_list([day(0, signed=True, coins=70)])]) == {
            "claimed": True, "coins": 70}

    def test_other_days_are_ignored(self):
        payloads = [sign_list([day(-2, signed=True, coins=10), day(1, signed=False, coins=90)])]
        assert today_from_payloads(payloads) == {"claimed": None, "coins": None}

    def test_a_non_coin_prize_is_not_a_coin_count(self):
        node = {"calendarDayDistance": 0,
                "signResultList": [{"signSuccess": False,
                                    "prizeInfoList": [{"prizeType": "coupon", "prizeAmount": 5}]}]}
        assert today_from_payloads([sign_list([node])]) == {"claimed": False, "coins": None}

    @pytest.mark.parametrize("payloads", [
        [], None,
        [{"api": "mtop.aliexpress.coin.execute", "body": '{"data": {}}'}],
        [{"api": "mtop.aliexpress.coin.channel.sign.list", "body": "not json"}],
        [{"api": "mtop.aliexpress.coin.channel.sign.list", "body": '{"data": null}'}],
    ])
    def test_nothing_usable_says_nothing(self, payloads):
        assert today_from_payloads(payloads) == {"claimed": None, "coins": None}

    def test_the_dead_page_case_yields_no_answer(self):
        # No coin API responses were captured at all when the page never rendered.
        assert today_from_payloads([])["claimed"] is None
