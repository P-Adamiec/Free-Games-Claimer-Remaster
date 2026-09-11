"""Amazon's verification code screen (issue #46).

The screen used to be recognised by a list of English phrases, checked once, about
six seconds after the password was submitted. Miss it and the login loop navigates
away, which is what made entering a code by hand impossible.
"""

from pathlib import Path

import pytest

from src.stores.prime import is_code_screen

ROOT = Path(__file__).resolve().parent.parent


class TestIsCodeScreen:
    def test_the_input_alone_is_enough(self):
        # No English anywhere: this is the case a phrase list can never cover.
        assert is_code_screen({"otpField": True, "body": "Sicherheitscode eingeben"})

    def test_an_mfa_form_counts_too(self):
        assert is_code_screen({"mfaForm": True, "body": ""})

    @pytest.mark.parametrize("body", [
        "Enter security code",
        "Two-Step Verification",
        "We sent a One Time Password to your email",
        "Security code required, please Verify",
    ])
    def test_the_old_wording_still_works(self, body):
        assert is_code_screen({"body": body})

    @pytest.mark.parametrize("state", [
        {"body": "Prime Gaming, claim your free games"},
        {"body": ""},
        {},
        None,
    ])
    def test_an_ordinary_page_is_not_a_code_screen(self, state):
        assert not is_code_screen(state)


class TestTheLoopLeavesTheCodeScreenAlone:
    """Navigating away mid-code is the bug itself, so guard it in the source."""

    SOURCE = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")

    def test_navigation_waits_for_the_code_screen(self):
        block = self.SOURCE.split("# --- Login loop", 1)[1].split("async def _do_login", 1)[0]
        before_nav = block.split("await self.page.get(URL_CLAIM)")[-2]
        assert "_code_screen_present()" in before_nav

    def test_the_manual_wait_does_not_click_during_a_code_screen(self):
        block = self.SOURCE.split("async def _vnc_check", 1)[1].split("logged_in =", 1)[0]
        assert block.index("_code_screen_present()") < block.index("_click_sign_in()")

    def test_the_challenge_handler_polls(self):
        block = self.SOURCE.split("async def _handle_security_code_challenge", 1)[1]
        block = block.split("\n    async def ", 1)[0]
        assert "for _ in range(" in block and "_code_screen_present()" in block
