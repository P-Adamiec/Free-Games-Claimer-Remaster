"""Epic claim verification (issue #39).

Walking Epic's checkout only proves what that page displayed, so a claim counts
only once the product page itself reports the game as owned.
"""

import asyncio
import contextlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import nodriver as uc
import pytest

from src.core.claimer import account_key, older_than
from src.stores.epic import SAVED_ACCOUNT_FINDER, URL_CLAIM, EpicGamesClaimer, saved_account_finder, text_finder, tile_finder
from src.stores.epic import (PAGE_STATE_JS, checkout_total_is_zero, is_owned, is_region_locked,
                             offers_a_purchase, region_locked_status, title_from_slug)


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
    """After a half-finished sign-in Epic asks which saved account to continue with, with or without credentials."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def test_it_looks_for_your_address_in_lower_case(self):
        assert json.dumps("me@example.com") in saved_account_finder(" Me@Example.com ")

    def test_without_an_address_it_looks_for_nothing_by_name(self):
        assert "const want = \"\";" in saved_account_finder(None)

    def test_it_only_acts_on_epics_login_pages_and_tiles_you_can_see(self):
        finder = saved_account_finder("")
        assert "'/id/login/'" in finder and "getBoundingClientRect" in finder and "__" not in finder

    def test_several_accounts_and_no_match_is_never_a_guess(self):
        # The old fallback took the first tile, which could claim on someone else's account.
        assert "tiles.length === 1 ? tiles[0] : null" in SAVED_ACCOUNT_FINDER
        assert SAVED_ACCOUNT_FINDER.count("tiles[0]") == 1

    def test_the_other_buttons_on_the_list_are_never_matched(self):
        # Seen live under the tiles: "Use another account" would start a fresh sign-in, so it must stay out of reach.
        assert "querySelectorAll('[id^=\"account-\"]')" in SAVED_ACCOUNT_FINDER
        assert not any(name.startswith("account-") for name in ("use-different-account", "manage-accounts"))

    def test_the_tile_is_clicked_with_the_mouse(self, monkeypatch):
        from src.stores import epic as epic_module

        monkeypatch.setattr(epic_module.cfg, "eg_email", "me@example.com")
        calls = []

        async def click(finder, label, allow_script=False, tries=1):
            calls.append((finder, label, allow_script, tries))
            return True

        claimer = SimpleNamespace(_cdp_click_found=click)
        assert asyncio.run(EpicGamesClaimer._pick_saved_account(claimer, wait=10)) is True
        assert calls == [(saved_account_finder("me@example.com"), "saved account", True, 10)]

    def test_no_list_is_no_pick(self):
        async def click(finder, label, allow_script=False, tries=1):
            return False

        assert asyncio.run(EpicGamesClaimer._pick_saved_account(SimpleNamespace(_cdp_click_found=click))) is False

    def test_the_login_loop_answers_it_before_the_code_screen(self):
        # Epic can show the list on the MFA address, which the code screen check would take for itself.
        block = self.SOURCE.split("otp_tried = 0", 1)[1]
        assert block.index("await self._pick_saved_account()") < block.index("await self._mfa_prompt_present()")


class TestSigningInWithoutCredentials:
    """Without EG_EMAIL and EG_PASSWORD a saved account is picked, and you are only asked when that does not sign in."""

    class _Page:
        def __init__(self, signed_in_after):
            # How many sign-in checks after the pick until Epic's redirect lands on the store; None never.
            self.signed_in_after, self.picked, self.checks_since_pick = signed_in_after, False, 0

        async def get(self, url):
            pass

        async def evaluate(self, script):
            if "isloggedin" not in script:
                return ""
            if not self.picked:
                return False
            self.checks_since_pick += 1
            return self.signed_in_after is not None and self.checks_since_pick >= self.signed_in_after

    def _sign_in(self, tmp_path, monkeypatch, picker=True, signed_in_after=1):
        from src.core import claimer as claimer_module
        from src.stores import epic as epic_module

        monkeypatch.setattr(claimer_module.cfg, "_data_dir", tmp_path)
        monkeypatch.setattr(epic_module.cfg, "eg_email", None)
        monkeypatch.setattr(epic_module.cfg, "eg_password", None)
        claimer = EpicGamesClaimer()
        claimer.page = page = self._Page(signed_in_after)
        claimer.vnc_waits, claimer.picks = [], []

        async def nothing(*args, **kwargs):
            return False

        async def pick(wait=1):
            claimer.picks.append(wait)
            page.picked = picker
            return picker

        async def vnc(check_fn, **kwargs):
            claimer.vnc_waits.append(check_fn)
            return False

        claimer.sleep = claimer._human_challenge_present = claimer._navigate_organically_to_login = nothing
        claimer._pick_saved_account, claimer._wait_for_vnc_login = pick, vnc
        return claimer, asyncio.run(claimer._ensure_logged_in())

    def test_a_picked_account_signs_in_without_waking_you(self, tmp_path, monkeypatch):
        claimer, signed_in = self._sign_in(tmp_path, monkeypatch, signed_in_after=3)
        assert signed_in is True and claimer.picks == [10] and claimer.vnc_waits == []

    def test_a_pick_that_never_lands_still_asks_you(self, tmp_path, monkeypatch):
        claimer, signed_in = self._sign_in(tmp_path, monkeypatch, signed_in_after=None)
        assert signed_in is False and len(claimer.vnc_waits) == 1
        assert claimer.page.checks_since_pick == 15

    def test_no_list_asks_you_straight_away(self, tmp_path, monkeypatch):
        claimer, signed_in = self._sign_in(tmp_path, monkeypatch, picker=False)
        assert signed_in is False and len(claimer.vnc_waits) == 1 and claimer.page.checks_since_pick == 0


class TestSigningInWithCredentials:
    """The login loop looks for the saved-account list only where Epic shows it, and hands you an unclear choice."""

    LOGIN = "https://www.epicgames.com/id/login?lang=en-US"
    SWITCH = "https://www.epicgames.com/id/login/switch-account"

    class _Page:
        def __init__(self, url):
            self.url = url

        async def get(self, url):
            pass

        async def evaluate(self, script):
            if "window.location.href" in script:
                return self.url
            return False if "isloggedin" in script else ""

    def _sign_in(self, tmp_path, monkeypatch, url):
        from src.core import claimer as claimer_module
        from src.stores import epic as epic_module

        monkeypatch.setattr(claimer_module.cfg, "_data_dir", tmp_path)
        monkeypatch.setattr(epic_module.cfg, "eg_email", "me@example.com")
        monkeypatch.setattr(epic_module.cfg, "eg_password", "secret")
        claimer = EpicGamesClaimer()
        claimer.page = self._Page(url)
        claimer.picks, claimer.logins, claimer.notices = 0, 0, []

        async def nothing(*args, **kwargs):
            return False

        async def pick(wait=1):
            claimer.picks += 1
            return False

        async def login():
            claimer.logins += 1

        async def vnc(check_fn, custom_msg=None, **kwargs):
            claimer.notices.append(custom_msg)
            return False

        claimer.sleep = claimer._human_challenge_present = claimer._navigate_organically_to_login = nothing
        claimer._mfa_prompt_present = claimer._press_sign_in = nothing
        claimer._pick_saved_account, claimer._do_stealth_login, claimer._wait_for_vnc_login = pick, login, vnc
        return claimer, asyncio.run(claimer._ensure_logged_in())

    def test_the_password_page_is_not_searched_for_the_list(self, tmp_path, monkeypatch):
        # Each search that finds nothing writes a debug line, once a second for the whole wait.
        claimer, _ = self._sign_in(tmp_path, monkeypatch, self.LOGIN)
        assert claimer.picks == 0 and claimer.logins == 3

    def test_an_account_it_cannot_pick_is_handed_to_you_at_once(self, tmp_path, monkeypatch):
        claimer, signed_in = self._sign_in(tmp_path, monkeypatch, self.SWITCH)
        assert signed_in is False and claimer.logins == 1 and claimer.picks == 5
        assert len(claimer.notices) == 1 and "pick your account" in claimer.notices[0]


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
        assert self.CLAIM.index("_clear_checkout_challenge(title)") < self.CLAIM.index("_confirm_in_library(url,")

    def test_a_claim_that_did_not_land_is_tried_again_not_handed_to_you(self):
        # Only a captcha needs a person; the failed row is opened again on the next run.
        tail = re.sub(r"\s+", " ", self.CLAIM.split("_confirm_in_library(url,", 1)[1])
        assert 'notify_game["status"] = "failed"' in tail
        assert "yourself" not in tail and '"notified"' not in tail
        # Only a captcha left unanswered in this run reaches "Needed you", and it says so.
        assert tail.count("needs_you(") == 1
        assert 'if waits_for_nobody(self.store_name): needs_you(self.store_name, 1, "game", why=CAPTCHA_UNSOLVED)' in tail

    def test_a_checkout_that_did_not_finish_reloads_the_page_once(self):
        # 15.10: four failed checkouts reloaded their pages three times each on a session under watch.
        assert "_confirm_in_library(url, tries=3 if checkout_ok else 1)" in self.CLAIM

    def test_a_security_check_nobody_solved_reaches_needed_you(self):
        stuck = re.sub(r"\s+", " ", self.CLAIM.split("not cleared, trying again next run", 1)[1][:300])
        assert 'needs_you(self.store_name, 1, "game", why=CAPTCHA_UNSOLVED)' in stuck

    @pytest.mark.parametrize("module,loop", [("epic", "_claim_game(url"), ("epic_fab", "_claim_listing(item)")])
    def test_after_an_unsolved_captcha_the_store_opens_nothing_more(self, module, loop):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / f"{module}.py").read_text(encoding="utf-8")
        run = re.sub(r"\s+", " ", source.split("async def run(", 1)[1].split("\n    async def ", 1)[0])
        assert "if self._captcha_stops_store(" in run and "break" in run
        assert run.index("_captcha_stops_store(") < run.index(f"await self.{loop}")


class TestNoConsentIsGivenForYou:
    """Seen live on 28.09: after Get, Epic's final step offers "Share my email with Ubisoft"."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def test_only_the_licence_box_is_ever_ticked(self):
        assert 'input[type="checkbox"]' not in self.SOURCE
        assert self.SOURCE.count("querySelector('input#agree')") == 1
        # Both checkout flows tick it through the one finder.
        assert self.SOURCE.count("_cdp_click_found(FIND_LICENCE_BOX") == 2


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


class TestAPaidGameIsNeverOrdered:
    """Audit: every product button but "Get" went to the old checkout, "Buy Now" included, and nothing read the price."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
    CLAIM = SOURCE.split("async def _claim_game", 1)[1].split("\n    async def ", 1)[0]
    FRAME = SOURCE.split("async def _handle_purchase_iframe", 1)[1].split("\n    def ", 1)[0]

    @pytest.mark.parametrize("state,sells", [
        ({"flow": "old_cta", "text": "buy now"}, True),
        ({"flow": "old_cta", "text": "pre-purchase"}, True),
        ({"flow": "old_cta", "text": "add to cart"}, True),
        ({"flow": "old_cta", "text": "€19.99"}, True),
        ({"flow": "old_cta", "text": "free"}, False),
        ({"flow": "new_get", "text": "get"}, False),
        ({"flow": "new_add", "text": "add to library"}, False),
        ({}, False),
    ])
    def test_a_button_that_sells_is_known(self, state, sells):
        assert offers_a_purchase(state) is sells

    @pytest.mark.parametrize("text,free", [
        ("Price €19.99 Total €19.99", False),
        ("Price €19.99 Discount -€19.99 Total €0.00", True),
        ("Subtotal €19.99 Total €0,00", True),
        ("TOTAL\nFree", True),
        ("Total $0", True),
        ("Total 1 234,00 zł", False),
        ("Total (VAT free) €19.99", False),
        ("Place Order", None),
        ("", None),
        (None, None),
    ])
    def test_only_a_total_of_zero_counts_as_free(self, text, free):
        assert checkout_total_is_zero(text) is free

    def test_a_selling_page_stops_before_anything_is_clicked(self):
        stop = self.CLAIM.index("if offers_a_purchase(state):")
        assert stop < self.CLAIM.index("if cfg.dryrun:") < self.CLAIM.index("_cdp_click_found(FIND_PURCHASE_CTA")

    def test_the_order_waits_for_a_free_total(self):
        check = self.FRAME.index("checkout_total_is_zero(")
        assert check < self.FRAME.index('return "not-free"') < self.FRAME.index("FIND_PLACE_ORDER")
        assert self.FRAME.index("if free is None:") < self.FRAME.index("FIND_PLACE_ORDER")

    def test_a_not_free_checkout_is_reported_as_skipped(self):
        after = self.CLAIM.split("checkout_ok = await self._handle_purchase_iframe(title)", 1)[1][:300]
        assert 'checkout_ok == "not-free"' in after and '"skipped:not-free"' in after


class TestEpicIsClickedLikeAHand:
    """Checkout captchas came seconds after a script click inside Epic's cross-origin checkout frame (2026-10-09)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def _block(self, name):
        block = self.SOURCE.split(f"async def {name}", 1)[1].split("\n    async def ", 1)[0]
        return re.sub(r"\(\s+", "(", re.sub(r"\s+", " ", block))

    def test_the_frame_buttons_are_clicked_with_the_mouse(self):
        new_flow = self._block("_handle_new_checkout")
        assert "_cdp_click_in_frame(ctx_id, FIND_ADD_TO_LIBRARY" in new_flow
        assert "_cdp_click_in_frame(ctx_id, FIND_I_ACCEPT" in new_flow
        old_flow = self._block("_handle_purchase_iframe")
        assert "_cdp_click_in_frame(ctx_id, FIND_PLACE_ORDER" in old_flow
        assert "_cdp_click_in_frame(ctx_id, FIND_I_ACCEPT" in old_flow

    @pytest.mark.parametrize("name", ["_handle_new_checkout", "_handle_purchase_iframe", "_click_page_button_by_text"])
    def test_no_script_click_is_left_in_the_checkout(self, name):
        # The parental PIN's "cont.click()" stays: its digits are typed by script anyway.
        block = self._block(name)
        assert "btn.click()" not in block and "po.click()" not in block and "agree.click()" not in block

    def test_a_frame_click_adds_where_the_frame_sits(self):
        block = self._block("_cdp_click_in_frame")
        assert 'f["x"] + box["x"]' in block and 'f["y"] + box["y"]' in block

    def test_every_mouse_click_goes_through_the_one_loop(self):
        assert self._block("_click_until_taken").count("_human_click(") == 1
        assert "_cdp_click_found(text_finder(" in self._block("_cdp_click_element_by_text")
        assert "_click_until_taken(" in self._block("_cdp_click_in_frame")
        assert "type_=\"mousePressed\"" not in self.SOURCE

    def test_a_covered_button_is_waited_for_not_clicked_by_script(self):
        # A covered button sits behind an overlay or the captcha; only the last try may fall back to a script click.
        new_flow = self._block("_handle_new_checkout")
        assert "last_try = attempt == 24" in new_flow and "allow_script=last_try" in new_flow
        assert "allow_script=attempt == 7" in self._block("_handle_purchase_iframe")

    def test_the_frame_click_point_is_checked_on_the_page(self):
        assert "FRAME_HIT_JS" in self._block("_cdp_click_in_frame")

    def test_the_cookie_banner_accept_all_is_never_matched(self):
        assert "includes('Accept')" not in self.SOURCE
        assert '_cdp_click_element_by_text("accept", timeout=1, exact=True)' in self._block("_handle_new_checkout")

    def test_the_script_click_skips_a_disabled_button(self):
        assert "if (el && !el.disabled) {{ el.click()" in self.SOURCE


class TestTheFrameClickAimsAtItsOwnFrame:
    """15.10: Epic moved its checkout to /checkout, and a selector measured the hidden hCaptcha frame instead."""

    # Read live on 15.10: the Checkout frame's corner, and "Add to library" inside it.
    ORIGIN = json.dumps({"x": 92.5, "y": 57.5, "w": 1078, "top": 57.5, "bottom": 517.5})
    BUTTON = {"x": 561, "y": 423, "key": "BUTTON|Add to library"}

    def _to_page(self, owner_answers, owner_fails=False):
        sent = []

        class Page:
            async def send(self, command):
                method = next(command)["method"]
                sent.append(method)
                if method == "DOM.getFrameOwner":
                    if owner_fails:
                        raise RuntimeError("No frame for given id found")
                    return uc.cdp.dom.BackendNodeId(7), None
                if method == "DOM.resolveNode":
                    return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("iframe"))
                return SimpleNamespace(value=owner_answers.pop(0)), None

        point = {}

        async def click_until_taken(read, run_js, wheel_point, to_page, finder, label, tries=1, allow_script=False):
            point["at"] = await to_page(self.BUTTON)
            return False

        stub = SimpleNamespace(page=Page(), _click_until_taken=click_until_taken)
        asyncio.run(EpicGamesClaimer._cdp_click_in_frame(stub, 1, "() => null", "add to library",
                                                         frame_id=uc.cdp.page.FrameId("checkout")))
        return point["at"], sent

    def test_the_point_is_the_frames_own_corner_plus_the_button(self):
        at, sent = self._to_page([self.ORIGIN, True])
        assert at == (654, 480)
        assert sent.count("DOM.getFrameOwner") == 2

    def test_a_point_off_the_frame_is_not_clicked(self):
        assert self._to_page([self.ORIGIN, False])[0] is None

    def test_a_frame_the_page_cannot_place_is_not_clicked(self):
        assert self._to_page([], owner_fails=True)[0] is None

    def test_the_checkout_frame_is_found_by_its_address(self):
        node = lambda fid, url, kids=(): SimpleNamespace(frame=SimpleNamespace(id_=fid, url=url), child_frames=list(kids))
        tree = node("top", "https://store.epicgames.com/en-US/p/bad-cheese-4256d9", [
            node("blank", "about:blank"), node("checkout", "https://store.epicgames.com/checkout?locale=en-US")])
        assert EpicGamesClaimer.__new__(EpicGamesClaimer)._find_purchase_frame(tree) == "checkout"

    def test_the_old_checkout_waits_in_the_frame_tree(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
        old_flow = source.split("async def _handle_purchase_iframe", 1)[1].split("\n    def ", 1)[0]
        assert "querySelector('#webPurchaseContainer" not in old_flow and "FIND_CHECKOUT_FRAME" not in source


class TestEveryClickIsChecked:
    """A click counts once two reads show the button gone or replaced; a cover is waited out, never clicked through."""

    CLICKABLE = {"x": 10, "y": 10, "key": "BUTTON|Get"}

    class _Stub:
        def __init__(self, boxes, script_ok=True, after_script=None):
            self.boxes, self.clicks, self.scripts, self.wheels = list(boxes), 0, 0, []
            self.script_ok, self.after_script = script_ok, after_script or [{"reason": "missing"}]

        async def sleep(self, seconds):
            pass

        async def _human_click(self, x, y, w=0, h=0):
            self.clicks += 1

        async def _wheel_scroll(self, x, y, dy):
            self.wheels.append(dy)

        _script_click_js = staticmethod(EpicGamesClaimer._script_click_js)

        async def _bring_into_view(self, read, run_js, wheel_point, finder, label):
            return await EpicGamesClaimer._bring_into_view(self, read, run_js, wheel_point, finder, label)

        async def _click_took(self, read, label, key="", before=""):
            return await EpicGamesClaimer._click_took(self, read, label, key, before)

    def _run(self, stub, tries=1, allow_script=False):
        async def read():
            return stub.boxes.pop(0) if len(stub.boxes) > 1 else stub.boxes[0]

        async def run_js(expression):
            if "el.click()" in expression:
                stub.scripts += 1
                stub.boxes = list(stub.after_script)
                return stub.script_ok
            return None

        async def wheel_point():
            return (640, 360)

        async def as_is(box):
            return box["x"], box["y"]

        return asyncio.run(EpicGamesClaimer._click_until_taken(
            stub, read, run_js, wheel_point, as_is, "() => null", "button", tries=tries, allow_script=allow_script))

    def test_a_click_that_takes_counts_once(self):
        stub = self._Stub([self.CLICKABLE, {"reason": "missing"}])
        assert self._run(stub) is True and stub.clicks == 1 and stub.scripts == 0

    def test_one_calm_read_is_not_enough(self):
        # A re-render can drop the button for one moment and put it straight back.
        stub = self._Stub([self.CLICKABLE, {"reason": "missing"}] + [self.CLICKABLE] * 9)
        assert self._run(stub) is False and stub.clicks == 1

    def test_a_click_that_did_not_take_is_made_again(self):
        stub = self._Stub([self.CLICKABLE] * 9 + [self.CLICKABLE, {"reason": "missing"}])
        assert self._run(stub, tries=2) is True and stub.clicks == 2

    def test_never_more_than_two_mouse_clicks_per_button(self):
        stub = self._Stub([self.CLICKABLE])
        assert self._run(stub, tries=6) is False and stub.clicks == 2 and stub.scripts == 0

    def test_two_clicks_that_did_nothing_may_end_in_a_script_click_when_allowed(self):
        stub = self._Stub([self.CLICKABLE])
        assert self._run(stub, tries=3, allow_script=True) is True and stub.clicks == 2 and stub.scripts == 1

    def test_another_button_where_the_clicked_one_was_means_it_took(self):
        # "get" gone, the loose text match now finds "Get it later": that is not the button that was clicked.
        stub = self._Stub([self.CLICKABLE, {"x": 5, "y": 5, "key": "BUTTON|Get it later"}])
        assert self._run(stub) is True and stub.clicks == 1

    @pytest.mark.parametrize("cover", ["covered"])
    def test_a_covered_button_is_never_clicked_through(self, cover):
        stub = self._Stub([{"reason": cover}])
        assert self._run(stub, tries=3, allow_script=True) is False and stub.clicks == 0 and stub.scripts == 0

    def test_a_hidden_box_is_clicked_by_script_and_must_change(self):
        stub = self._Stub([{"reason": "hidden"}])
        assert self._run(stub, tries=3) is True and stub.scripts == 1

    def test_a_hidden_box_that_stays_hidden_did_not_take(self):
        stub = self._Stub([{"reason": "hidden"}], after_script=[{"reason": "hidden"}])
        assert self._run(stub, tries=3) is False and stub.scripts == 1

    def test_a_missing_button_is_waited_for_never_scripted(self):
        stub = self._Stub([{"reason": "missing"}])
        assert self._run(stub, tries=3, allow_script=True) is False and stub.scripts == 0

    def test_an_off_screen_button_is_scrolled_to_with_the_wheel(self):
        stub = self._Stub([{"reason": "offscreen", "dy": 900}, {"reason": "offscreen", "dy": 300},
                           self.CLICKABLE, {"reason": "missing"}])
        assert self._run(stub) is True and stub.wheels == [900, 300] and stub.clicks == 1

    def test_a_wheel_that_moves_nothing_falls_back_to_a_jump(self):
        stub = self._Stub([{"reason": "offscreen", "dy": 900}, {"reason": "offscreen", "dy": 900},
                           self.CLICKABLE, {"reason": "missing"}])
        assert self._run(stub) is True and stub.wheels == [900] and stub.clicks == 1

    def test_every_checkout_starts_counting_from_zero(self):
        claim = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8") \
            .split("async def _claim_game", 1)[1].split("\n    async def ", 1)[0]
        assert "self._click_counts = {}" in claim


class TestTextFinder:
    def test_exact_matches_only_the_whole_text(self):
        finder = text_finder("accept", exact=True)
        assert 'norm(b) === "accept"' in finder and "includes(" not in finder.split("return all.find", 1)[1]

    def test_loose_also_takes_a_longer_label(self):
        assert 'norm(b).includes("get")' in text_finder("Get")


class TestGamePagesAreOpenedSparingly:
    """Issue #86: Cloudflare's "One more step" page was read as the game, and every run opened every game page again."""

    URL = "https://store.epicgames.com/en-US/p/out-of-sight"

    class _Stub:
        store_name, user = "epic", "u"
        _open_game_page = EpicGamesClaimer._open_game_page
        _click_game_tile = EpicGamesClaimer._click_game_tile

        def __init__(self, challenge=(), cleared=False, tile=False, path="/"):
            self.challenge, self.cleared, self.tile, self.path = list(challenge), cleared, tile, path
            self.notify_games, self._platform_labels = [], {}
            self.loads, self.asked, self.reads = [], [], 0
            self.page = self

        async def get(self, url):
            self.loads.append(url)

        async def evaluate(self, script):
            return self.path if "location.pathname" in script else "Out of Sight"

        async def sleep(self, seconds):
            pass

        async def _human_challenge_present(self):
            return self.challenge.pop(0) if self.challenge else False

        async def _wait_out_challenge(self, label, store_key=None, ask=True):
            self.asked.append(ask)
            return self.cleared

        async def _cdp_click_found(self, finder, label):
            return self.tile

        async def _click_page_button_by_text(self, *args, **kwargs):
            return False

        async def _page_state(self, tries=15):
            self.reads += 1
            return {"flow": "owned", "text": "in library"}

    @pytest.fixture
    def row(self, monkeypatch):
        import src.stores.epic as epic
        row = SimpleNamespace(status="unknown", title="out-of-sight", extra=None, commits=0)

        class Session:
            async def commit(self):
                row.commits += 1

        @contextlib.asynccontextmanager
        async def session():
            yield Session()

        async def get_or_create(session, **kwargs):
            return row, False

        monkeypatch.setattr(epic, "async_session", session)
        monkeypatch.setattr(epic, "get_or_create", get_or_create)
        return row

    @staticmethod
    def _checked(days):
        return json.dumps({"page_checked": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()})

    def _claim(self, stub, from_list=False):
        asyncio.run(EpicGamesClaimer._claim_game(stub, self.URL, from_list=from_list))
        return stub

    def test_a_game_you_have_is_left_alone_for_three_days(self, row):
        row.status, row.title, row.extra = "existed", "Out of Sight", self._checked(1)
        stub = self._claim(self._Stub())
        assert stub.loads == [] and stub.notify_games == [{"title": "Out of Sight", "url": self.URL, "status": "existed"}]

    @pytest.mark.parametrize("days", [None, 4])
    def test_then_its_page_is_read_again(self, row, days):
        row.status, row.extra = "existed", self._checked(days) if days else None
        stub = self._claim(self._Stub())
        assert stub.loads == [self.URL] and stub.reads == 1
        assert not older_than(json.loads(row.extra)["page_checked"], 1)

    def test_a_game_blocked_in_your_region_is_left_alone_quietly(self, row):
        row.status, row.extra = "skipped:region", self._checked(1)
        stub = self._claim(self._Stub())
        assert stub.loads == [] and stub.notify_games == []

    def test_a_check_on_a_game_you_have_never_asks_you(self, row):
        row.status, row.title, row.extra = "existed", "Out of Sight", self._checked(4)
        stub = self._claim(self._Stub(challenge=[True]))
        assert stub.asked == [False] and stub.reads == 0
        assert stub.notify_games == [{"title": "Out of Sight", "url": self.URL, "status": "existed"}]
        assert row.commits == 1 and not older_than(json.loads(row.extra)["page_checked"], 1)

    def test_an_unsolved_check_names_the_game_not_the_check(self, row):
        # Earlier runs stored the check page's heading as the game's name.
        row.status, row.title = "failed", "One more step"
        stub = self._claim(self._Stub(challenge=[True]))
        assert stub.asked == [True] and stub.reads == 0
        assert stub.notify_games == [{"title": "Out Of Sight", "url": self.URL, "status": "failed"}]

    def test_a_cleared_check_is_not_followed_by_a_second_load(self, row):
        # Cloudflare reloads the page itself once its check passes.
        stub = self._claim(self._Stub(challenge=[True], cleared=True))
        assert stub.loads == [self.URL] and stub.reads == 1
        assert row.status == "existed" and row.title == "Out of Sight"

    def test_the_ownership_check_stops_at_an_unsolved_check(self):
        stub = self._Stub(challenge=[True])
        assert asyncio.run(EpicGamesClaimer._confirm_in_library(stub, self.URL)) is False
        assert stub.loads == [self.URL]

    def test_a_game_on_the_list_is_opened_from_its_tile(self, row):
        stub = self._claim(self._Stub(tile=True, path="/en-US/p/out-of-sight"), from_list=True)
        assert stub.loads == [] and stub.reads == 1

    def test_without_a_tile_the_list_is_tried_then_the_address(self, row):
        stub = self._claim(self._Stub(), from_list=True)
        assert stub.loads == [URL_CLAIM, self.URL]

    def test_a_tile_that_leads_elsewhere_falls_back_to_the_address(self, row):
        stub = self._claim(self._Stub(tile=True, path="/en-US/p/something-else"), from_list=True)
        assert stub.loads == [self.URL]

    def test_mobile_games_skip_the_list(self):
        # Checked live: the free games list has no tile for them, so looking only cost a page load.
        run = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic.py").read_text(encoding="utf-8")
        assert '(game["url"], game["url"] not in self._platform_labels)' in run and 'todo.append((game["final_url"], False))' in run

    def test_a_game_from_gamerpower_is_opened_by_its_address(self, row):
        stub = self._claim(self._Stub(tile=True, path="/en-US/p/out-of-sight"))
        assert stub.loads == [self.URL]

    def test_the_slug_cannot_break_out_of_the_script(self):
        slug = 'x"); alert(1); ("'
        assert json.dumps("/" + slug) in tile_finder(slug)


class TestOneAccountKey:
    """Epic did not always show the profile name, and each time it was missing every owned game looked new (2026-10-14)."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_the_sign_in_setting_wins(self):
        assert account_key(" pawel@gmail.com ", "Not Weak.") == account_key("pawel@gmail.com", None) == "pawel@gmail.com"

    def test_without_one_the_name_on_the_page_is_used(self):
        assert account_key(None, "Not Weak.") == "Not Weak." and account_key("", None) == "unknown"

    @pytest.mark.parametrize("store,setting,count", [
        ("epic.py", "eg_email", 1), ("epic_fab.py", "eg_email", 2),
        ("steam.py", "steam_username", 5), ("ubisoft.py", "ubi_email", 2),
    ])
    def test_every_store_with_a_name_from_the_page_saves_under_its_setting(self, store, setting, count):
        source = (self.ROOT / "src" / "stores" / store).read_text(encoding="utf-8")
        assert source.count(f"account_key(cfg.{setting}, self.user)") == count
        assert 'user=self.user or "unknown"' not in source and '(self.user or "unknown")' not in source
