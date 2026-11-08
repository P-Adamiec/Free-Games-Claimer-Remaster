"""Every human check counts toward the weekly line: a sign-in captcha handed over, AliExpress's slider, Bing's check."""

import asyncio
import json
import time

import pytest

import src.core.claimer as claimer_module
from src.core import run_state


@pytest.fixture
def recorded(monkeypatch):
    # No screenshot from a test, whatever DEBUG says in the owner's .env.
    monkeypatch.setattr(claimer_module.cfg, "debug", False)
    seen = []
    monkeypatch.setattr(claimer_module, "record_check", lambda store, puzzle, where="": seen.append((store, puzzle)))
    return seen


def _claimer(cls=claimer_module.BaseClaimer, reason=""):
    claimer = cls.__new__(cls)
    # BaseClaimer itself is "base"; the sign-in tests stand for Prime.
    claimer.store_name = "prime" if cls is claimer_module.BaseClaimer else cls.store_name
    claimer.page = None

    async def read():
        return reason
    claimer._human_challenge_reason = read
    return claimer


class TestASignInCaptchaHandedToYou:
    def test_it_is_counted_before_you_are_asked(self, recorded):
        # Review 8.11: Prime, Steam, GOG and Microsoft hand sign-in captchas straight over, so they were never counted.
        claimer = _claimer(reason="frame: https://www.google.com/recaptcha/api2/bframe")
        run_state.mark_unanswered("prime")

        async def signed_in():
            return False
        assert asyncio.run(claimer._wait_for_vnc_login(signed_in)) is False
        assert recorded == [("prime", True)]

    def test_a_sign_in_without_a_check_counts_nothing(self, recorded):
        asyncio.run(_claimer(reason="")._count_check("prime"))
        assert recorded == []

    def test_a_check_the_store_already_counted_is_not_counted_twice(self, recorded):
        claimer = _claimer(reason="frame: hcaptcha")
        claimer._checks_counted = {"prime": time.monotonic()}
        asyncio.run(claimer._count_check("prime"))
        assert recorded == []

    def test_the_wait_out_marks_what_it_counted(self):
        source = claimer_module.BaseClaimer._wait_out_challenge.__code__.co_names
        assert "record_check" in source and "monotonic" in source


class TestAliExpressSlider:
    def _diagnose(self, recorded, **found):
        from src.stores.aliexpress import AliExpressClaimer
        claimer = _claimer(AliExpressClaimer)

        class Page:
            async def evaluate(self, _js):
                return json.dumps({"cookieCount": 3, **found})
        claimer.page = Page()
        asyncio.run(claimer._diagnose_page())
        return recorded

    def test_a_slider_on_screen_is_counted_as_a_puzzle(self, recorded):
        assert self._diagnose(recorded, challenge=True, onScreen=True) == [("aliexpress", True)]

    def test_the_cookie_left_from_an_old_check_is_not(self, recorded):
        assert self._diagnose(recorded, hasX5sec=True, challenge=True, onScreen=False) == []
