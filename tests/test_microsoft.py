"""What counts as a Microsoft code, and what counts as a giveaway.

The price rules come from the store's own answers, read live while this was written:
a Game Pass title reports price 0 with "Included", a free to play one reports price 0
with no list price at all, and only a normally paid game at zero is a giveaway.
"""

from pathlib import Path

import main
import pytest

from src.stores.gamerpower import classify_target
from src.stores.microsoft import (
    free_to_keep,
    is_microsoft_code,
    product_id_from_url,
    read_product,
)


class TestWhichCodesAreOurs:

    @pytest.mark.parametrize("store", ["microsoft store", "xbox", "Xbox", " Microsoft Store "])
    def test_prime_tagged_it_for_microsoft(self, store):
        assert is_microsoft_code('{"external_store": "%s"}' % store) is True

    @pytest.mark.parametrize("extra", [
        '{"external_store": "gog"}',
        '{"external_store": "legacy games"}',
        '{"external_store": ""}',
        '{"something": "else"}',
        "not json at all",
        "",
        None,
    ])
    def test_everything_else_is_left_alone(self, extra):
        assert is_microsoft_code(extra) is False


class TestFindingTheProduct:

    @pytest.mark.parametrize("url,expected", [
        ("https://www.xbox.com/en-US/games/store/graveyard-keeper/9NBLGGH4R6ZP", "9NBLGGH4R6ZP"),
        ("https://www.microsoft.com/en-us/p/some-game/9NKX70BBCDRN", "9NKX70BBCDRN"),
        ("https://www.xbox.com/en-US/games/store/x/9MTLKM2DJMZ2?activetab=pivot", "9MTLKM2DJMZ2"),
        ("https://www.xbox.com/en-US/games/store/graveyard-keeper/", ""),
        ("", ""),
    ])
    def test_the_store_id_is_read_from_the_address(self, url, expected):
        assert product_id_from_url(url) == expected

    def test_the_list_price_comes_from_the_sku_when_the_product_omits_it(self):
        # Exactly the shape the store returned for a Game Pass title.
        payload = {"Payload": {"ProductId": "9NCJWHHMVHR0", "Title": "Keeper", "Price": 0.0, "MSRP": None,
                               "DisplayPrice": "Included", "SkusSummary": [{"MSRP": 19.99}]}}
        product = read_product(payload)
        assert product["msrp"] == 19.99
        assert product["price"] == 0.0
        assert product["title"] == "Keeper"

    def test_an_empty_answer_is_not_a_crash(self):
        assert read_product({})["product_id"] == ""
        assert read_product(None)["title"] == ""


class TestOnlyAPaidGameAtZeroCounts:

    def test_a_paid_game_given_away_is_claimed(self):
        assert free_to_keep({"price": 0.0, "msrp": 19.99, "display_price": "Free"}) is True

    def test_game_pass_is_not_a_giveaway(self):
        # You lose it with the subscription, and it reports zero like a real giveaway does.
        assert free_to_keep({"price": 0.0, "msrp": 19.99, "display_price": "Included"}) is False

    def test_free_to_play_is_not_a_giveaway(self):
        assert free_to_keep({"price": 0.0, "msrp": None, "display_price": "Free"}) is False

    def test_a_game_that_still_costs_money_is_not_claimed(self):
        assert free_to_keep({"price": 63.99, "msrp": 63.99, "display_price": "$63.99"}) is False

    def test_a_discount_short_of_free_is_not_claimed(self):
        assert free_to_keep({"price": 0.99, "msrp": 19.99, "display_price": "$0.99"}) is False

    def test_nothing_readable_means_no(self):
        assert free_to_keep({}) is False
        assert free_to_keep({"price": "unknown", "msrp": "unknown"}) is False


class TestTheStoreIsWiredIn:

    @pytest.mark.parametrize("url", [
        "https://www.xbox.com/en-US/games/store/graveyard-keeper/9NBLGGH4R6ZP",
        "https://www.microsoft.com/en-us/p/some-game/9NKX70BBCDRN",
    ])
    def test_gamerpower_finds_are_routed_here(self, url):
        assert classify_target(url) == "microsoft"

    def test_it_is_a_store_you_can_pick(self):
        assert "microsoft" in main.ALL_CLAIMERS
        assert {"ms", "xbox", "microsoft-store"} <= set(main._ALIASES)

    def test_gamerpower_hands_its_finds_over(self):
        assert "microsoft" in main.GP_TARGETS

    def test_it_runs_by_default(self):
        assert "microsoft" in main.DEFAULT_STORES

    @pytest.mark.parametrize("method", ["run", "redeem_pending_codes"])
    def test_nothing_opens_without_something_to_take(self, method):
        # As a default it runs for people with no Microsoft account, so an empty run must cost nothing.
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "microsoft.py").read_text(encoding="utf-8")
        body = source.split(f"async def {method}(", 1)[1].split("\n    async def ", 1)[0]
        assert body.index("return") < body.index("start_browser()")


class TestNothingFromACodeLeaks:
    """A log file goes into bug reports, and screenshots write their path into it."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "microsoft.py").read_text(encoding="utf-8")

    def test_a_screenshot_is_never_named_after_a_code(self):
        # Part of a GOG recovery code reached the log this way once, through the saved path.
        for line in self.SOURCE.splitlines():
            if "take_screenshot" in line:
                assert "code" not in line, line.strip()

    def test_the_account_is_masked_wherever_it_becomes_the_user(self):
        for line in self.SOURCE.splitlines():
            if "self.user = " in line and "ms_email" in line:
                assert "mask_account(" in line, line.strip()


class TestAMissedGiveawayStillReachesYou:
    """A giveaway runs for days, so one the bot could not take has to be reported by default."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "microsoft.py").read_text(encoding="utf-8")
    FILTERED_WORDS = ("fail", "skip", "already", "exist", "download", "missing_base")

    def test_a_giveaway_left_for_you_is_reported_as_notified(self):
        # "notified" is the shared word for "found it, take it yourself", and the filter lets it through.
        block = self.SOURCE.split("async def _record_claim", 1)[1].split("\n    async def ", 1)[0]
        assert '"status": "notified"' in block
        for word in self.FILTERED_WORDS:
            assert word not in "notified", f"notified would be filtered out by {word}"

    def test_a_claim_that_did_not_happen_is_called_what_it_is(self):
        # One state, one name: the wording for you is a separate decision from the outcome.
        assert "unfinished" not in self.SOURCE
        press = self.SOURCE.split("async def _press_get", 1)[1].split("\n    async def ", 1)[0]
        assert 'return "failed"' in press

    def test_the_store_is_marked_as_needing_you(self):
        block = self.SOURCE.split("async def _record_claim", 1)[1].split("\n    async def ", 1)[0]
        assert "needs_you(self.store_name)" in block

    def test_only_a_get_button_is_ever_pressed(self):
        # "Buy" and the Game Pass offer sit right next to it, and neither is free.
        action = self.SOURCE.split("PAGE_ACTION_JS", 1)[1].split('"""', 2)[1]
        assert "startsWith('get')" in action
        assert "game pass" in action
        assert "'buy'" not in action

class TestTakingTheGame:
    """The claim path, pinned where live checks proved what matters (Fortnite, Warzone, Forza)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "microsoft.py").read_text(encoding="utf-8")
    PRESS = SOURCE.split("async def _press_get", 1)[1].split("\n    async def _owned_now", 1)[0]

    def test_a_price_in_the_window_stops_the_order(self):
        # The catalogue can be stale, so the till has the final say before anything is confirmed.
        assert 'if not till.get("free")' in self.PRESS
        assert "is not free" in self.PRESS

    def test_a_broken_window_gets_a_fresh_page_not_another_click(self):
        # Pressing Get again only re-reads the same broken window, a reload makes a new one.
        retry = self.PRESS.split("window, till = await self._open_purchase_window()", 1)[1]
        assert "await self._open(url)" in retry

    def test_the_account_page_decides_whether_it_worked(self):
        # The window closes itself on success, so ownership is read back from the store page.
        assert "_owned_now(url)" in self.PRESS
        owned = self.SOURCE.split("async def _owned_now", 1)[1].split("\n    async def ", 1)[0]
        assert "PAGE_OWNED_JS" in owned

    def test_the_read_only_check_never_presses_anything(self):
        script = self.SOURCE.split("PAGE_OWNED_JS", 1)[1].split('"""', 2)[1]
        assert ".click()" not in script

class TestSigningIn:
    """Two-step sign-in, pinned where the live run proved it (code screen, one password per profile)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "microsoft.py").read_text(encoding="utf-8")

    def test_no_pretend_backup_codes(self):
        # Microsoft issues one recovery code for the account, not codes to sign in with.
        assert "ms_otp_codes" not in self.SOURCE
        assert "used_microsoft_codes" not in self.SOURCE
        for path in ("src/core/config.py", ".env.example", "README.md"):
            text = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
            assert "MS_OTP_CODES" not in text, path

    def test_both_places_look_for_the_same_code_box(self):
        present = self.SOURCE.split("async def _two_factor_present", 1)[1].split("\n    async def ", 1)[0]
        fill = self.SOURCE.split("async def _fill_totp", 1)[1].split("\n    async def ", 1)[0]
        assert "self.CODE_FIELDS" in present and "self.CODE_FIELDS" in fill

    def test_the_secret_gets_two_tries_and_then_stops(self):
        fill = self.SOURCE.split("async def _fill_totp", 1)[1].split("\n    async def ", 1)[0]
        assert "range(OTP_KEY_ATTEMPTS)" in fill

    def test_a_signed_in_profile_is_not_asked_for_the_password_again(self):
        # Judging the session before Microsoft finished redirecting made it retype the password.
        ensure = self.SOURCE.split("async def _ensure_logged_in", 1)[1].split("\n    async def ", 1)[0]
        assert "_settled_login_state()" in ensure


class TestPrimeAnnouncesTheCodes:
    """Prime hands out the codes, and its line should say whether a store will redeem them."""

    PRIME = (Path(__file__).resolve().parent.parent / "src" / "stores" / "prime.py").read_text(encoding="utf-8")
    BRANCH = PRIME.split("GOG and Microsoft redeem their own codes", 1)[1].split("take_screenshot", 1)[0]

    def test_microsoft_codes_are_announced_like_gog_ones(self):
        assert "Microsoft, pending auto-redeem" in self.BRANCH
        assert "GOG, pending auto-redeem" in self.BRANCH

    def test_the_promise_is_only_made_when_that_store_runs(self):
        # Saying "pending auto-redeem" for a store left out of STORES promised work nobody would do.
        assert 'is_store_active("gog")' in self.BRANCH
        assert 'is_store_active("microsoft")' in self.BRANCH

    def test_one_list_decides_which_codes_are_microsoft(self):
        assert "MICROSOFT_CODE_STORES" in self.BRANCH
