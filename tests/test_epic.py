"""Epic claim verification (issue #39).

Walking Epic's checkout only proves what that page displayed, so a claim counts
only once the product page itself reports the game as owned.
"""

import re
from pathlib import Path

import pytest

from src.stores.epic import (PAGE_STATE_JS, is_owned, is_region_locked, region_locked_status,
                             title_from_slug)


class TestOwnedState:
    def test_an_owned_page_is_owned(self):
        assert is_owned({"flow": "owned", "text": "in library"})

    def test_the_button_text_counts_whatever_the_flow_says(self):
        assert is_owned({"flow": "old_cta", "text": "IN LIBRARY"})

    @pytest.mark.parametrize("state", [
        {"flow": "new_get", "text": "get"},
        {"flow": "new_add", "text": "add to library"},
        {"flow": "old_cta", "text": "buy now"},
    ])
    def test_a_page_that_still_offers_the_game_is_not_owned(self, state):
        assert not is_owned(state)

    def test_the_offer_text_is_not_a_confirmation(self):
        # "Add it to your library" is what Epic says before you own anything.
        assert not is_owned({"flow": "new_get", "text": "add it to your library"})

    @pytest.mark.parametrize("state", [{"flow": "unknown", "text": ""}, {}, None])
    def test_an_unreadable_page_is_not_owned(self, state):
        assert not is_owned(state)


class TestPageStateOrder:
    """An "In Library" chip in a recommendation row must not outrank this product's own button."""

    def test_every_claim_button_is_checked_before_ownership(self):
        owned_at = PAGE_STATE_JS.index("'owned'")
        for flow in ("'new_add'", "'new_get'", "'old_cta'"):
            assert PAGE_STATE_JS.index(flow) < owned_at

    def test_the_reader_returns_json(self):
        # page.evaluate() hands back a CDP structure for a plain object, a string survives.
        assert PAGE_STATE_JS.strip().startswith("JSON.stringify(")


class TestAGameBlockedInYourRegion:
    """BURIED STARS, 4.10: claimed on 1.10, then Epic blocked it in Poland and showed only a sentence."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
    CLAIM = SOURCE.split("async def _claim_game", 1)[1].split("\n    async def ", 1)[0]

    def test_the_page_is_recognised(self):
        assert is_region_locked({"flow": "region", "text": ""})
        assert not is_region_locked({"flow": "unknown", "text": ""})
        assert not is_region_locked(None)
        assert "unavailable in your platform or region" in PAGE_STATE_JS

    def test_it_is_checked_after_every_real_button(self):
        # A product page with a button is never mistaken for the blocked one.
        assert PAGE_STATE_JS.index("'region'") > PAGE_STATE_JS.index("'owned'")

    @pytest.mark.parametrize("previous,expected", [
        ("claimed", "existed"), ("existed", "existed"),
        ("unknown", None), ("failed", None), ("skipped:region", None), (None, None),
    ])
    def test_a_game_you_took_stays_owned_the_rest_stays_quiet(self, previous, expected):
        assert region_locked_status(previous) == expected

    def test_the_sentence_never_becomes_the_title(self):
        # The heading is read after the region check, so Epic's sentence cannot be stored as the name.
        assert self.CLAIM.index("is_region_locked(state)") < self.CLAIM.index("document.querySelector('h1')")

    @pytest.mark.parametrize("slug,title", [
        ("buried-stars-d7c88c", "Buried Stars"),
        ("system-shock-2-25th-anniversary-remaster-cb94d9", "System Shock 2 25Th Anniversary Remaster"),
        ("fortnite", "Fortnite"),
    ])
    def test_a_name_from_the_address_when_nothing_else_has_one(self, slug, title):
        assert title_from_slug(slug) == title

    def test_a_gamerpower_find_on_epics_own_list_is_not_checked_twice(self):
        run = self.SOURCE.split("async def run", 1)[1].split("\n    async def ", 1)[0]
        assert "already handled" in run and "in handled" in run


class TestClaimHonesty:
    """Mobile games were reported as claimed on the strength of the checkout page alone."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
    BLOCK = SOURCE.split("async def _claim_game", 1)[1].split("async def _handle_new_checkout", 1)[0]

    def test_the_checkout_result_no_longer_decides(self):
        assert "claimed = await self._handle" not in self.SOURCE

    def test_the_library_decides(self):
        assert "_confirm_in_library" in self.BLOCK

    def test_an_unconfirmed_claim_is_reported_as_such(self):
        assert "failed:unconfirmed" in self.BLOCK

    def test_the_early_success_check_ignores_the_offer_text(self):
        checkout = self.SOURCE.split("async def _handle_new_checkout", 1)[1]
        assert "add it to your library" in checkout.split("already_done = await", 1)[1][:800]


class TestTheCodeScreenIsNotAbandoned:
    """A rejected 2FA code used to spin for two minutes and then take the page away (issue #46)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
    LOOP = SOURCE.split("otp_tried = 0", 1)[1].split('if "login/review"', 1)[0]

    def test_a_rejected_code_is_retried_once_after_a_reload(self):
        # Epic answers a stale request with "Incorrect response. Please refresh the page."
        assert "page.reload()" in self.LOOP
        assert "otp_tried >= OTP_KEY_ATTEMPTS" in self.LOOP

    def test_the_second_rejection_hands_over_to_the_user(self):
        assert "mfa_manual = True" in self.LOOP

    def test_the_page_is_not_taken_away_while_the_code_screen_is_up(self):
        # The guard has to sit between the wait loop and the navigation that verifies success.
        block = self.SOURCE.split("otp_tried = 0", 1)[1]
        assert block.index("if not mfa_manual and await self._mfa_prompt_present():") < block.index("# verify success")

    def test_what_epic_said_reaches_the_log(self):
        assert "_mfa_error_text" in self.SOURCE


class TestRememberThisBrowser:
    """Ticking it makes the store stop asking for a code on this profile."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_epic_ticks_it_before_submitting(self):
        source = (self.ROOT / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
        block = source.split("async def _fill_code", 1)[1].split("\n    async def ", 1)[0]
        assert block.index("_remember_this_browser()") < block.index("submit.click()")

    def test_prime_ticks_it_before_submitting(self):
        source = (self.ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")
        block = source.split("otp_input.send_keys(self._last_totp)", 1)[1][:600]
        assert "_remember_this_browser()" in block

    def test_it_never_unticks_a_box_the_store_already_ticked(self):
        source = (self.ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
        block = source.split("async def _remember_this_browser", 1)[1].split("\n    async def ", 1)[0]
        assert "box.checked" in block


class TestBackupCodeScreen:
    """Epic keeps backup codes behind their own screen, verified live on 2026-09-11."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def test_the_screen_is_opened_before_a_code_is_typed(self):
        block = self.SOURCE.split("async def _fill_backup_code", 1)[1].split('\n    async def ', 1)[0]
        assert block.index("_open_backup_code_screen()") < block.index("_fill_code(")

    def test_no_code_is_spent_when_the_screen_does_not_open(self):
        block = self.SOURCE.split("async def _fill_backup_code", 1)[1].split('\n    async def ', 1)[0]
        opened = block.index("_open_backup_code_screen()")
        assert block.index("_mark_code_used(") > opened
        assert "leaving your codes alone" in block

    def test_it_clicks_epics_own_option(self):
        block = self.SOURCE.split("async def _open_backup_code_screen", 1)[1].split('\n    async def ', 1)[0]
        assert "#option-backupCode" in block and "another way" in block

    def test_eight_boxes_mean_the_backup_screen(self):
        # The authenticator screen has six numeric boxes, the backup one eight text boxes.
        block = self.SOURCE.split("BACKUP_SCREEN_JS", 1)[1][:400]
        assert "boxes.length > 6" in block


class TestAccountPicker:
    """After a half-finished sign-in Epic asks which account to continue with."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def test_the_login_loop_answers_it(self):
        assert "/id/login/switch-account" in self.SOURCE

    def test_it_clicks_the_account_tile(self):
        # Verified live: that screen has no Continue button, only a list of accounts.
        block = self.SOURCE.split("/id/login/switch-account", 1)[1][:700]
        assert "[id^=\"account-\"]" in block


class TestRecoveryCodeBookkeeping:
    """One code per sign-in, never the same one twice, and no crash once they run out."""

    def _claimer(self, tmp_path, monkeypatch):
        # Patch the claimer module's own cfg: reloading config elsewhere leaves it holding the old one.
        from src.core import claimer as claimer_module
        from src.stores.epic import EpicGamesClaimer

        monkeypatch.setattr(claimer_module.cfg, "_data_dir", tmp_path)
        return EpicGamesClaimer()

    def test_it_starts_with_the_first_code(self, tmp_path, monkeypatch):
        claimer = self._claimer(tmp_path, monkeypatch)
        assert claimer._next_unused_code(["aaa", "bbb", "ccc"], "used.txt") == "aaa"

    def test_a_spent_code_is_skipped(self, tmp_path, monkeypatch):
        claimer = self._claimer(tmp_path, monkeypatch)
        claimer._mark_code_used("aaa", "used.txt", ["aaa", "bbb", "ccc"])
        assert claimer._next_unused_code(["aaa", "bbb", "ccc"], "used.txt") == "bbb"

    def test_running_out_is_not_a_crash(self, tmp_path, monkeypatch):
        claimer = self._claimer(tmp_path, monkeypatch)
        for code in ("aaa", "bbb"):
            claimer._mark_code_used(code, "used.txt", ["aaa", "bbb"])
        assert claimer._next_unused_code(["aaa", "bbb"], "used.txt") is None

    def test_no_codes_configured_is_not_a_crash(self, tmp_path, monkeypatch):
        claimer = self._claimer(tmp_path, monkeypatch)
        assert claimer._next_unused_code([], "used.txt") is None

    def test_the_login_loop_tries_a_code_before_giving_up(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
        loop = source.split("otp_tried = 0", 1)[1].split('if "login/review"', 1)[0]
        assert loop.index("_fill_backup_code()") < loop.index("mfa_manual = True")


class TestTheCheckoutCaptcha:
    """Issue #61: a captcha inside Epic's checkout frame held the order and nobody was told."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
    CLAIM = SOURCE.split("async def _claim_game", 1)[1].split("async def _handle_new_checkout", 1)[0]
    CHECKOUT = SOURCE.split("async def _handle_new_checkout", 1)[1].split("\n    async def ", 1)[0]

    def test_the_checkout_frame_is_searched_too(self):
        # The page check only sees the top document, verified against a real browser.
        look = self.SOURCE.split("async def _checkout_challenge_present", 1)[1].split("\n    async def ", 1)[0]
        assert "_find_purchase_frame(" in look and "CHALLENGE_JS" in look

    def test_it_is_checked_before_the_page_text_can_read_as_success(self):
        assert self.CHECKOUT.index("_clear_checkout_challenge(title)") < self.CHECKOUT.index("already_done = await")

    def test_the_final_check_waits_for_it_too(self):
        verify = self.CHECKOUT.split("Step 4: Verify claim success", 1)[1]
        assert verify.index("_clear_checkout_challenge(title)") < verify.index("success = await")

    def test_nothing_leaves_the_page_while_one_is_up(self):
        # Checking ownership opens the product page, which would drop an order still waiting.
        assert self.CLAIM.index("_clear_checkout_challenge(title)") < self.CLAIM.index("_confirm_in_library(url)")

    def test_a_claim_that_did_not_land_reaches_you(self):
        tail = self.CLAIM.split("_confirm_in_library(url)", 1)[1]
        assert 'notify_game["status"] = "notified"' in tail
        assert "needs_you(self.store_name)" in tail
        assert 'notify_game["status"] = "failed"' not in tail


class TestNoConsentIsGivenForYou:
    """Seen live on 28.09: after Get, Epic's final step offers "Share my email with Ubisoft"."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def test_only_the_licence_box_is_ever_ticked(self):
        assert 'input[type="checkbox"]' not in self.SOURCE
        assert self.SOURCE.count("querySelector('input#agree')") == 2


class TestTheEpicSignInPage:
    """Issue #72 (PR #74 by @privatepenguinzero): the sign-in page is read with CSS lookups, never a text guess."""

    STORES = Path(__file__).resolve().parent.parent / "src" / "stores"

    @pytest.mark.parametrize("name", ["epic.py", "epic_fab.py"])
    def test_css_selectors_go_to_select_not_find(self, name):
        # find() is a text search; select() is the CSS lookup these fields need.
        source = (self.STORES / name).read_text(encoding="utf-8")
        assert not re.findall(r"""\.find\(\s*["'](#|\[|[a-z]+\[|[a-z]+#)""", source)

    def test_no_text_fallback_can_reach_sign_in_with_apple(self):
        source = (self.STORES / "epic.py").read_text(encoding="utf-8")
        press = source.split("async def _press_sign_in", 1)[1].split("\n    async def ", 1)[0]
        assert 'find("Sign in"' not in press and 'select("#sign-in"' in press
