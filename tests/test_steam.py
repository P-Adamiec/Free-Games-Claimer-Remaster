"""Steam: what may be claimed on a store page."""

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src.stores.steam import (DEMO_JS, GAMES_ONLY, KEY_OUTCOMES, REMEMBER_ME_JS, REMEMBER_ME_STATE_JS, STORE_FREE_SEARCH,
                              SteamClaimer, activation_outcome, base_game_button_js, claim_order, free_search_games,
                              is_waiting_steam_key)

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

    def test_a_package_from_gamerpower_counts_as_landing_on_steam(self):
        # A /sub/ link was taken for "not Steam", and claim_order had already dropped the search's copy of it.
        stub = self._Stub([False, False], vnc_answer=False)
        stub.url = "https://store.steampowered.com/sub/469/"

        async def where(script):
            return stub.url if "location.href" in script else '{"loggedIn": false}'

        async def no_sign_in(url):
            return False
        stub.evaluate, stub._ensure_logged_in = where, no_sign_in
        asyncio.run(SteamClaimer._claim_game(stub, {"url": stub.url, "title": "Pack", "source": "gamerpower"}))
        assert stub._signed_out

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


def search_row(href, discount, final, title):
    """One result row in the shape Steam's search page rendered it on 13.10."""
    return (f'<a href="{href}" data-ds-appid="1" onmouseover="GameHover( this, event, {{&quot;type&quot;:&quot;app&quot;}} );" '
            f'class="search_result_row ds_collapse_flag " data-gpnav="item">'
            f'<div class="search_name ellipsis"><span class="title">{title}</span></div>'
            f'<div class="search_price_discount_combined responsive_secondrow" data-price-final="{final}">'
            f'<div class="discount_block search_discount_block" data-price-final="{final}" data-bundlediscount="0" '
            f'data-discount="{discount}"><div class="discount_pct">-{discount}%</div></div></div></a>')


class TestFreeGamesFromSteamsOwnSearch:
    """Issue #84: SteamDB asked bots to stop, so GamerPower comes first and Steam's own store search adds the rest."""

    FREE = "https://store.steampowered.com/app/4847440/MXGP_26__Scrubdesignz_Liveries/?snr=1_7_7_2300_150_1"

    class _Page:
        def __init__(self, answer):
            self.page, self.answer, self.loads = self, answer, []

        async def get(self, url):
            self.loads.append(url)

        async def evaluate(self, script):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        async def sleep(self, seconds):
            pass

    def _fetch(self, answer):
        stub = self._Page(answer)
        return asyncio.run(SteamClaimer._fetch_free_from_store(stub)), stub

    def test_a_paid_item_at_100_percent_off_is_found(self):
        games = free_search_games(search_row(self.FREE, "100", "0", "MXGP 26 &amp; Liveries"))
        assert games == [{"title": "MXGP 26 & Liveries", "url": self.FREE, "app_id": "4847440", "source": "steam"}]

    def test_free_to_play_and_partly_discounted_rows_are_left_out(self):
        # Without the sale filter Steam listed 69,684 free items and none of them at -100%.
        page = (search_row("https://store.steampowered.com/app/730/CounterStrike_2/", "0", "0", "Counter-Strike 2")
                + search_row("https://store.steampowered.com/app/2/Half_Off/", "50", "417", "Half Off")
                + search_row(self.FREE, "100", "0", "MXGP 26"))
        assert [g["app_id"] for g in free_search_games(page)] == ["4847440"]

    def test_a_package_row_counts_too(self):
        games = free_search_games(search_row("https://store.steampowered.com/sub/12345/?snr=1_7_7", "100", "0", "Pack"))
        assert games[0]["app_id"] == "12345" and "/sub/" in games[0]["url"]

    def test_links_that_are_not_result_rows_are_ignored(self):
        menu = '<a href="https://store.steampowered.com/app/1/" class="menuitem">data-discount="100" data-price-final="0"</a>'
        assert free_search_games(menu) == [] and free_search_games("") == [] and free_search_games(None) == []

    def test_the_search_asks_steam_for_free_items_on_sale(self):
        url = urlparse(STORE_FREE_SEARCH)
        query = parse_qs(url.query)
        assert url.hostname == "store.steampowered.com" and query["maxprice"] == ["free"] and query["specials"] == ["1"]

    def test_nothing_talks_to_steamdb_any_more(self):
        root = Path(__file__).resolve().parent.parent
        for path in [root / "main.py", *(root / "src").rglob("*.py")]:
            assert "steamdb.info" not in path.read_text(encoding="utf-8").lower(), path

    @pytest.mark.parametrize("dlc,address", [(True, STORE_FREE_SEARCH), (False, STORE_FREE_SEARCH + GAMES_ONLY)])
    def test_the_search_page_is_read_like_a_visit(self, monkeypatch, dlc, address):
        import src.stores.steam as steam
        monkeypatch.setattr(steam.cfg, "steam_claim_dlc", dlc)
        games, stub = self._fetch(json.dumps({"rows": search_row(self.FREE, "100", "0", "MXGP 26"), "shown": 1}))
        assert [g["app_id"] for g in games] == ["4847440"] and stub.loads == [address]

    def test_without_dlc_steam_is_asked_for_games_only(self):
        # Read live on 13.10: the four free DLC were all gone with this filter, and all there with category1=21.
        assert parse_qs(urlparse(STORE_FREE_SEARCH + GAMES_ONLY).query)["category1"] == ["998"]

    def test_gamerpower_comes_first_and_the_search_adds_the_rest(self):
        gamerpower = [{"title": "Pony Island", "final_url": "https://store.steampowered.com/app/405640/Pony_Island/"}]
        store = free_search_games(search_row("https://store.steampowered.com/app/405640/Pony_Island/?snr=1_7_7", "100", "0",
                                             "Pony Island") + search_row(self.FREE, "100", "0", "MXGP 26"))
        todo = claim_order(gamerpower, store)
        assert [(g["title"], g["source"]) for g in todo] == [("Pony Island", "gamerpower"), ("MXGP 26", "steam")]
        assert todo[0]["url"] == gamerpower[0]["final_url"]

    def test_the_age_gate_address_is_the_same_page(self):
        gamerpower = [{"title": "Game", "final_url": "https://store.steampowered.com/agecheck/app/1/"}]
        store = free_search_games(search_row("https://store.steampowered.com/app/1/Game/", "100", "0", "Game"))
        assert len(claim_order(gamerpower, store)) == 1

    def test_an_app_and_a_package_with_the_same_number_are_both_kept(self):
        gamerpower = [{"title": "Game", "final_url": "https://store.steampowered.com/app/12345/Game/"}]
        store = free_search_games(search_row("https://store.steampowered.com/sub/12345/", "100", "0", "Pack"))
        assert len(claim_order(gamerpower, store)) == 2 and claim_order([], store) == store

    def test_the_run_claims_in_that_order(self):
        run = SOURCE.split("    async def run(", 1)[1].split("\n    async def ", 1)[0]
        assert "todo = claim_order(extra_games or [], store_games)" in run

    @pytest.mark.parametrize("answer", [RuntimeError("tab closed"), {"type": "object"}, "not json"])
    def test_a_broken_search_page_never_stops_the_run(self, answer):
        assert self._fetch(answer)[0] == []

    def test_a_longer_list_is_reported(self, caplog):
        with caplog.at_level("WARNING", logger="fgc.steam"):
            self._fetch(json.dumps({"rows": search_row(self.FREE, "100", "0", "MXGP 26"), "shown": 50}))
        assert "only the first 50 were read" in caplog.text

    def test_rows_that_no_longer_read_are_reported(self, caplog):
        with caplog.at_level("WARNING", logger="fgc.steam"):
            self._fetch(json.dumps({"rows": "<div>new layout</div>", "shown": 4}))
        assert "none read as 100% off" in caplog.text


class TestSteamSearchSwitch:
    """STEAM_SEARCH=false leaves Steam to GamerPower, a safety switch should Steam ever mind the search."""

    class _Run:
        def __init__(self):
            self.started, self.searched, self.claimed, self._signed_out = False, False, [], False

        async def start_browser(self, **kwargs):
            self.started = True

        async def close_browser(self):
            pass

        async def _fetch_free_from_store(self):
            self.searched = True
            return []

        async def _claim_game(self, game):
            self.claimed.append(game["title"])

    @pytest.fixture(autouse=True)
    def _search_off(self, monkeypatch):
        import src.stores.steam as steam
        monkeypatch.setattr(steam.cfg, "steam_search", False)

    def test_nothing_from_gamerpower_starts_no_browser(self):
        stub = self._Run()
        asyncio.run(SteamClaimer.run(stub, extra_games=[]))
        assert not stub.started and not stub.searched

    def test_gamerpower_games_are_claimed_without_the_search(self):
        stub = self._Run()
        game = {"title": "Pony Island", "final_url": "https://store.steampowered.com/app/405640/Pony_Island/"}
        asyncio.run(SteamClaimer.run(stub, extra_games=[game]))
        assert stub.started and not stub.searched and stub.claimed == ["Pony Island"]


class TestOnlyRealGiveaways:
    """Read live on 13.10: a giveaway shows "-100%", a DLC free for good only "Free DLC" and "Download"."""

    CLAIM = SOURCE.split("async def _claim_game", 1)[1].split("\n    async def ", 1)[0]

    def test_the_price_check_knows_a_giveaway_by_its_discount(self):
        assert ("const giveaway = [...purchaseBlocks].some(b => !isDemoBlock(b) && (b.textContent || '').includes('-100%'));"
                in self.CLAIM)

    def test_a_page_without_the_discount_is_skipped_before_any_base_game_is_added(self):
        skip = self.CLAIM.index('if price_info and not price_info.get("giveaway"):')
        base = self.CLAIM.index("await self._ensure_base_game(current_url)")
        assert self.CLAIM.index('notify_game["status"] = "skipped:paid"') < skip < self.CLAIM.index('"skipped:not-a-giveaway"') < base

    def test_nothing_is_written_for_it(self):
        # A row counted as done would hide a later giveaway of the base game, GamerPower matches titles by fragment.
        assert "always_free" not in self.CLAIM

    def test_without_steam_dlc_a_dlc_page_is_left_alone_first(self):
        guard = self.CLAIM.index("if not cfg.steam_claim_dlc and")
        assert guard < self.CLAIM.index("price_check_raw = await") < self.CLAIM.index("await self._ensure_base_game(")
