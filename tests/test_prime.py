"""Amazon's verification code screen (issue #46).

The screen used to be recognised by a list of English phrases, checked once, about
six seconds after the password was submitted. Miss it and the login loop navigates
away, which is what made entering a code by hand impossible.
"""

import asyncio
import re
from pathlib import Path

import pytest

from src.stores.prime import PrimeGamingClaimer, is_code_screen

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


class TestAFailedSignInStopsTheRun:
    """#77: a VNC login nobody answered still sent the run through every game, each one reported as failed."""

    SOURCE = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")

    class _Page:
        async def evaluate(self, js):
            return '{"found": false}'

        async def get(self, url):
            pass

    class _Stub:
        def __init__(self, signed_in, vnc_answer):
            self.page = TestAFailedSignInStopsTheRun._Page()
            self.signed_in, self.vnc_answer = signed_in, vnc_answer
            self.notify_games = []
            self._signed_out = False

        async def sleep(self, seconds):
            pass

        async def _is_signed_in(self):
            return self.signed_in

        async def _wait_for_vnc_login(self, check_fn, custom_msg=None):
            return self.vnc_answer

        def _vnc_notice(self, title, body):
            return title

        def log_signed_in(self):
            pass

    def test_nobody_at_vnc_is_a_failed_sign_in(self):
        stub = self._Stub(signed_in=False, vnc_answer=False)
        assert asyncio.run(PrimeGamingClaimer._ensure_logged_in(stub)) is False

    def test_a_session_found_on_the_second_look_is_a_sign_in(self):
        # No button and signed in on the re-check used to fall through to the VNC wait, which says no after an unanswered prompt.
        stub = self._Stub(signed_in=False, vnc_answer=False)
        looks = iter([False, True])

        async def second_look():
            return next(looks, True)
        stub._is_signed_in = second_look
        assert asyncio.run(PrimeGamingClaimer._ensure_logged_in(stub)) is True

    def test_a_valid_session_is_a_sign_in(self):
        stub = self._Stub(signed_in=True, vnc_answer=False)
        assert asyncio.run(PrimeGamingClaimer._ensure_logged_in(stub)) is True

    def test_a_game_after_a_lost_session_is_left_alone(self, monkeypatch):
        from src.core.config import cfg
        monkeypatch.setattr(cfg, "dryrun", False)
        stub = self._Stub(signed_in=False, vnc_answer=False)

        async def no_sign_in(silent_if_logged_in=False):
            return False
        stub._ensure_logged_in = no_sign_in
        asyncio.run(PrimeGamingClaimer._claim_single_internal(stub, {"title": "DOOM", "href": "/dp/x"}))
        assert stub._signed_out and stub.notify_games == []

    def test_the_run_stops_before_the_games(self):
        run = self.SOURCE.split("async def run", 1)[1].split("\n    async def ", 1)[0]
        assert run.index("if not await self._ensure_logged_in():") < run.index("await self._claim_internal_games()")

    def test_the_loop_stops_once_signed_out(self):
        loop = self.SOURCE.split("async def _claim_internal_games", 1)[1].split("\n    async def ", 1)[0]
        after = loop.split("if self._signed_out:", 1)[1][:400]
        assert "len(filtered_games) - index" in after and "needs_you(" in after and "break" in after

    def test_the_summary_hears_about_it_too(self):
        run = self.SOURCE.split("async def run", 1)[1].split("\n    async def ", 1)[0]
        assert 'needs_you(self.store_name, 0, "game")' in run


class TestACodeNotReadIsLookedForAgain:
    """Audit: a game claimed before its code showed was saved as claimed and never visited again."""

    SOURCE = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")
    TILES = re.search(r'stats_raw = await self\.page\.evaluate\(\s*(rf"""[\s\S]*?""")', SOURCE).group(1)
    EXTERNAL = SOURCE.split("async def _claim_single_external", 1)[1].split("\n    async def ", 1)[0]

    def _script(self, force, missing):
        import json
        return eval(self.TILES, {"force_check_str": force, "json": json, "missing_code": missing})

    def test_the_tiles_waiting_for_a_code_reach_the_page_script(self):
        script = self._script("false", ['A "quoted" title', "Other"])
        assert 'new Set(["A \\"quoted\\" title", "Other"])' in script
        # Checked live in Chrome: an unread code comes back, a known one and a new game behave as before.
        assert "if (targetIsHowToPlay && !forceCheck && !recheck.has(titleText)) return;" in script

    def test_a_missing_code_is_marked_and_a_found_one_unmarks_it(self):
        flat = " ".join(self.EXTERNAL.split())
        assert "if not link_needed and not obj.code: obj.status = NO_CODE" in flat
        assert "if obj.status == NO_CODE: obj.status = \"claimed\"" in flat

    def test_the_dead_filter_is_gone(self):
        assert "existing_codes_map" not in self.SOURCE


class TestPasskeyBlockerStaysOnSignInPages:
    """The DOM sweep MutationObserver is meant for /ap/ sign-in pages, not the Luna React SPA."""

    SOURCE = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")

    def test_the_passkey_sweep_checks_for_ap_path(self):
        block = self.SOURCE.split("add_script_to_evaluate_on_new_document", 1)[1].split("Passkey neutralization", 1)[0]
        assert 'location.pathname.includes("/ap/")' in block

