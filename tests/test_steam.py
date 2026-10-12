"""Steam: what may be claimed on a store page."""

import asyncio
from pathlib import Path

import pytest

from src.stores.steam import (DEMO_JS, KEY_OUTCOMES, REMEMBER_ME_JS, REMEMBER_ME_STATE_JS, SteamClaimer,
                              activation_outcome, base_game_button_js, is_waiting_steam_key)

SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "steam.py").read_text(encoding="utf-8")


class TestRememberMe:
    """Issue #65: read live on 4.10, the box is a div with role="checkbox" and starts ticked."""

    def test_the_box_is_found_by_its_role_inside_the_login_form(self):
        assert '[role="checkbox"]' in REMEMBER_ME_JS and 'data-featuretarget="login"' in REMEMBER_ME_JS
        assert 'input[type="checkbox"]' not in SOURCE

    def test_a_ticked_box_is_never_clicked(self):
        # Clicking a box Steam already ticks would switch "Remember me" off.
        assert REMEMBER_ME_JS.index("=== 'true'") < REMEMBER_ME_JS.index("box.click()")

    def test_the_result_is_read_back_after_react_applies_it(self):
        assert "aria-checked" in REMEMBER_ME_STATE_JS
        block = SOURCE.split("# --- Remember Me", 1)[1].split("# --- Submit", 1)[0]
        assert block.index("REMEMBER_ME_JS") < block.index("self.sleep(0.5)") < block.index("REMEMBER_ME_STATE_JS")


class TestDemosAreNeverTheGiveaway:
    """Issue #62: a game's demo was added while checking a DLC's base game.

    Replayed in a real browser on the Explosive Odds page: with the demo button reading
    "Add to Library", the old base-game helper clicked it, and the price check took the demo
    block for a free one. Both now pass the demo by.
    """

    def test_demo_blocks_are_known_by_steams_own_markup(self):
        assert ".demo_above_purchase" in DEMO_JS and "#demoGameBtn" in DEMO_JS

    def test_every_script_that_judges_or_clicks_uses_it(self):
        for name in ("is_unclaimed_f2p", "price_check_raw", "claimed_raw"):
            call = SOURCE.split(f"{name} = await self.page.evaluate(", 1)[1][:200]
            assert "DEMO_JS" in call, name

    def test_the_base_game_helper_skips_demo_buttons(self):
        script = base_game_button_js(False)
        assert DEMO_JS in script and script.count("isDemoBtn(") >= 3

    def test_no_backspace_hides_in_a_regex(self):
        # A \b in a plain Python string reaches the browser as a backspace and the regex never matches.
        assert chr(8) not in SOURCE
        assert "price_check_raw = await self.page.evaluate(r'''" in SOURCE


class TestBaseGameInADryRun:
    """TODO 48: a dry run added a DLC's free base game to the library."""

    @pytest.mark.parametrize("dry", [True, False])
    def test_the_flag_reaches_the_page(self, dry):
        assert base_game_button_js(dry).endswith("})(%s))" % ("true" if dry else "false"))

    @pytest.mark.parametrize("dry", [True, False])
    def test_the_only_click_waits_for_a_real_run(self, dry):
        script = base_game_button_js(dry).replace(DEMO_JS, "")
        assert script.count(".click()") == 1 and "if (!dry) btn.click()" in script
        assert script.count("take(") == 3

    def test_the_helper_passes_the_setting_and_stops_before_adding(self):
        helper = SOURCE.split("async def _ensure_base_game", 1)[1].split("\n    async def ", 1)[0]
        assert "base_game_button_js(cfg.dryrun)" in helper
        dry = helper.index('add_res.get("success") and cfg.dryrun')
        assert dry < helper.index("return True", dry) < helper.index("✓ Added the free base game")


class TestClaimsAreConfirmed:
    """TODO 50: Steam reported "claimed" right after the click, without looking at the library."""

    class _Stub:
        def __init__(self, reads, url="https://store.steampowered.com/app/1/"):
            self.reads, self.url, self.loads, self.gates = list(reads), url, [], 0
            self.page = self

        async def get(self, url):
            self.loads.append(url)

        async def evaluate(self, script):
            if "location.href" in script:
                return self.url
            return self.reads.pop(0) if self.reads else False

        async def sleep(self, seconds):
            pass

        async def _handle_age_gate(self):
            self.gates += 1
            self.url = "https://store.steampowered.com/app/1/"

        async def _page_brief(self):
            return "stub"

    def _in_library(self, stub):
        return asyncio.run(SteamClaimer._in_library(stub, "https://store.steampowered.com/app/1/"))

    def test_owned_on_a_later_read_counts(self):
        stub = self._Stub([False, True])
        assert self._in_library(stub) is True and len(stub.loads) == 2

    def test_never_owned_is_not_a_claim(self):
        stub = self._Stub([False] * 5)
        assert self._in_library(stub) is False and len(stub.loads) == 3

    def test_an_unreadable_answer_is_not_owned(self):
        # nodriver hands back an object, not False, for some answers.
        assert self._in_library(self._Stub([{"type": "boolean"}, None, "true"])) is False

    def test_the_age_gate_is_passed_first(self):
        stub = self._Stub([True], url="https://store.steampowered.com/agecheck/app/1/")
        assert self._in_library(stub) is True and stub.gates == 1

    def test_the_claim_is_reported_only_after_the_library_says_so(self):
        claim = SOURCE.split("async def _claim_game", 1)[1].split("\n    async def ", 1)[0]
        check = claim.index("await self._in_library(current_url)")
        assert claim.index("if claim_error:") < check < claim.index("✓ Claimed '%s'!") < claim.index('status="claimed"')

    def test_the_base_game_is_reported_only_after_the_library_says_so(self):
        helper = SOURCE.split("async def _ensure_base_game", 1)[1].split("\n    async def ", 1)[0]
        assert helper.index("await self._in_library(base_url)") < helper.index('"status": "claimed"')


class TestAFailedSignInStopsTheRun:
    """TODO 2: a Steam sign-in nobody finished still sent the run through every game."""

    class _Stub:
        def __init__(self, logged_in, vnc_answer):
            self.page = self
            self.logged_in, self.vnc_answer = list(logged_in), vnc_answer
            self.user, self.notify_games, self._signed_out = None, [], False
            self._extract_game_id = SteamClaimer._extract_game_id

        async def evaluate(self, script):
            if "location.href" in script:
                return "https://store.steampowered.com/app/1/x/"
            return '{"loggedIn": %s}' % ("true" if (self.logged_in.pop(0) if self.logged_in else False) else "false")

        async def get(self, url):
            pass

        async def sleep(self, seconds):
            pass

        async def _dismiss_cookie_banner(self):
            pass

        async def _wait_for_vnc_login(self, check_fn, custom_msg=None):
            return self.vnc_answer

        def log_signed_in(self, username=None):
            pass

    @pytest.fixture(autouse=True)
    def _no_credentials(self, monkeypatch):
        # The module's own cfg: another test may have reloaded src.core.config since steam.py imported it.
        import src.stores.steam as steam
        monkeypatch.setattr(steam.cfg, "steam_username", None)
        monkeypatch.setattr(steam.cfg, "steam_password", None)

    def _sign_in(self, stub):
        return asyncio.run(SteamClaimer._ensure_logged_in(stub, "https://store.steampowered.com/"))

    def test_nobody_at_vnc_is_a_failed_sign_in(self):
        assert self._sign_in(self._Stub([False, False], vnc_answer=False)) is False

    def test_a_valid_session_is_a_sign_in(self):
        assert self._sign_in(self._Stub([True], vnc_answer=False)) is True

    def test_a_sign_in_finished_over_vnc_counts_even_if_the_page_is_unclear(self):
        assert self._sign_in(self._Stub([False, False], vnc_answer=True)) is True

    def test_the_game_is_left_alone_and_the_run_told(self):
        stub = self._Stub([False, False], vnc_answer=False)

        async def no_sign_in(url):
            return False
        stub._ensure_logged_in = no_sign_in
        asyncio.run(SteamClaimer._claim_game(stub, {"url": "https://store.steampowered.com/app/1/x/", "title": "X"}))
        assert stub._signed_out and stub.notify_games == []

    def test_the_loop_stops_once_signed_out(self):
        run = SOURCE.split("    async def run(", 1)[1].split("\n    async def ", 1)[0]
        after = run.split("if self._signed_out:", 1)[1][:500]
        assert "len(todo) - index" in after and "needs_you(self.store_name, left)" in after and "break" in after

    def test_keys_stay_queued_without_a_sign_in(self):
        keys = SOURCE.split("async def redeem_pending_keys", 1)[1].split("\n    async def ", 1)[0]
        stop = keys.index("if not await self._ensure_logged_in(URL_STORE):")
        assert stop < keys.index("return", stop) < keys.index("self._activate_key(")


class TestActivatingKeysOnSteam:
    """Fanatical giveaways hand out Steam keys; the bot enters them on Steam's own key page."""


    # Word for word what Steam showed on 4.10 for an invalid key.
    INVALID = ("The product code you've entered is not valid or is not a product code. Please double check to see "
               "if you've mistyped your key. I, L, and 1 can look alike, as can V and Y, and 0 and O.")

    @pytest.mark.parametrize("text,receipt,outcome", [
        ("", True, "activated"),
        (INVALID, False, "invalid"),
        ("This Steam account already owns the product(s) contained in this offer.", False, "owned"),
        ("The product code you've entered has already been activated by a different Steam account.", False, "used"),
        ("There have been too many recent activation attempts from this account or Internet address.", False,
         "rate-limited"),
        ("This product requires ownership of another product before activation.", False, "needs-base"),
        ("", False, "unknown"),
    ])
    def test_steams_answer_is_read(self, text, receipt, outcome):
        assert activation_outcome(text, receipt) == outcome

    def test_every_final_answer_has_a_status(self):
        assert set(KEY_OUTCOMES) == {"activated", "owned", "used", "invalid", "needs-base", "region"}
        assert KEY_OUTCOMES["needs-base"][1] == "failed:missing_base"

    @pytest.mark.parametrize("status,code,extra,waiting", [
        ("claimed", "AAAAA-BBBBB-CCCCC", '{"external_store": "steam"}', True),
        ("claimed and activated", "AAAAA-BBBBB-CCCCC", '{"external_store": "steam"}', False),
        ("claimed", "", '{"external_store": "steam"}', False),
        ("claimed", "TKT8ED0BC5D94D2EBC", '{"external_store": "gog"}', False),
        ("claimed", "AAAAA-BBBBB-CCCCC", None, False),
        ("claimed", "AAAAA-BBBBB-CCCCC", "not json", False),
    ])
    def test_only_keys_left_for_steam_are_picked(self, status, code, extra, waiting):
        assert is_waiting_steam_key(status, code, extra) is waiting

    def test_a_dry_run_activates_nothing(self):
        block = SOURCE.split("async def _activate_key", 1)[1].split("\n    async def ", 1)[0]
        assert block.index("if cfg.dryrun:") < block.index("send_keys(key)")

    def test_the_key_never_reaches_the_log_whole(self):
        block = SOURCE.split("async def _activate_key", 1)[1].split("\n    async def ", 1)[0]
        assert "key[:5]" in block and '", key)' not in block and "%s\", key" not in block

    def test_it_runs_after_the_sites_that_hand_keys_out(self):
        main = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        assert main.index("claim_side_stores(routed)") < main.index("redeem_pending_keys()")
        assert '"Steam" in store_names' in main.split("redeem_pending_keys()", 1)[0][-700:]
