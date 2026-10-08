"""Telling a dead coin page apart from an unreadable widget, and reading today from the API.

Issue #33: both looked identical in the log, so the bot spent 28 minutes waiting out a page
AliExpress never intended to render.
"""

import json
from pathlib import Path

import pytest

from src.stores.aliexpress import (aliexpress_country_site, aliexpress_mobile_url, page_is_dead,
                                  signed_in_from_payloads, today_from_payloads)


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


class TestCountrySites:
    """Issue #73 (PR #74 by @privatepenguinzero): an Italian account's coin page went to it.aliexpress.com."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "aliexpress.py").read_text(encoding="utf-8")

    @pytest.mark.parametrize("url,site", [
        ("https://it.aliexpress.com/?gatewayAdapt=glo2ita", "it"),
        ("https://es.aliexpress.com/", "es"),
        ("https://m.aliexpress.com/p/coin-index/index.html", ""),
        ("https://www.aliexpress.com/", ""),
        ("https://m.it.aliexpress.com/p/coin-index/index.html", ""),
        ("https://it.aliexpress.com.evil.example/", ""),
        ("https://evil.example/?next=it.aliexpress.com", ""),
        ("http://it.aliexpress.com/", ""),
        ("", ""),
        (None, ""),
    ])
    def test_the_site_is_read_from_the_host_only(self, url, site):
        assert aliexpress_country_site(url) == site

    def test_mobile_addresses_follow_the_site(self):
        assert aliexpress_mobile_url("") == "https://m.aliexpress.com/"
        assert aliexpress_mobile_url("it", "/p/coin-index/index.html") == "https://m.it.aliexpress.com/p/coin-index/index.html"

    def test_every_coin_page_load_can_follow_the_redirect(self):
        # A direct load anywhere else would skip the country check.
        assert self.SOURCE.count("COIN_PATH)") >= 2
        assert "page.get(aliexpress_mobile_url(self._site, COIN_PATH))" in self.SOURCE
        assert self.SOURCE.count("await self._load_coin_page()") == 2

    def test_the_italian_check_in_button_is_recognised(self):
        assert self.SOURCE.count("odbierz|raccogli|") == 2


class TestSignedInFromTheApi:
    """A signed-in coin page is told by the account's own API answer, in any language (TODO 37, #73)."""

    SOURCE = TestCountrySites.SOURCE

    def test_the_accounts_calendar_means_signed_in(self):
        assert signed_in_from_payloads([sign_list([day(0, signed=True, coins=40)])]) is True

    def test_a_wallet_alone_is_no_proof(self):
        # Release review: only the calendar was seen live; a 0 balance could come back for an expired session.
        assert signed_in_from_payloads([{"api": "mtop.aliexpress.coin.execute", "body": "{}",
                                         "fields": {"data.userCoinsNum": 0}}]) is None

    @pytest.mark.parametrize("payloads", [
        None,
        [],
        [sign_list([])],
        [{"api": "mtop.aliexpress.coin.channel.sign.list", "body": "not json"}],
        [{"api": "mtop.aliexpress.coin.channel.sign.list", "body": json.dumps({"ret": ["FAIL_SYS_SESSION_EXPIRED"]})}],
        [{"api": "mtop.something.else", "body": "{}", "fields": {"data.title": "x"}}],
    ])
    def test_no_answer_never_means_signed_out_either(self, payloads):
        # Signed out is only the login form's call; the API can only confirm a session.
        assert signed_in_from_payloads(payloads) is None

    def test_the_login_form_wins_then_the_api_then_the_labels(self):
        check = self.SOURCE.split("async def _is_logged_in", 1)[1].split("\n    async def ", 1)[0]
        form = check.index('if state.get("loginForm"):')
        api = check.index("signed_in_from_payloads(await self._read_coin_api(quiet=True))")
        labels = check.index('return bool(state.get("known"))')
        assert form < api < labels
        # Only this page's answer counts: a calendar from an earlier page must not end a sign-in wait early.
        assert "signed_in_from_payloads(self._coin_payloads)" not in check
