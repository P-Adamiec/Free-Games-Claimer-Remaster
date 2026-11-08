"""Fab limited-time free asset detection.

Pins two live traps: the payload keeps each listing's original price, and only
`isLimitedFreeContent` may authorise a claim.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.stores.epic_fab import CHECKOUT_CONSENT_OPEN_JS, CHECKOUT_STATE_JS, FabClaimer, _seller_name, parse_free_listings

# What /i/blades/free_content_blade served on 2026-08-08, trimmed to the fields used.
BLADE = {
    "blockContentType": "listings",
    "isLimitedFreeContent": True,
    "size": "listings_large",
    "url": "/limited-time-free",
    "searchQuery": None,
    "title": "Limited-Time Free (Until August 11 at 9:59 AM ET)",
    "tiles": [
        {"listing": {
            "uid": "a7000a7c-c5fe-45f3-b8df-3e85c576b6c1", "title": "Dragon Cave",
            "listingType": "3d-model", "user": {"isSeller": True, "sellerName": "Denys Rutkovskyi", "uid": "1bbfb042-cfae-4c42-b90f-b32774bdc270"},
            "startingPrice": {"price": 79.99, "offerId": "e108e623d8d54e649fe9a2f3b8d5c1df"},
        }},
        {"listing": {
            "uid": "9d27e228-5ee3-4bb5-a4a2-87ba386cb53a", "title": "Surface Forge v1.1.5",
            "listingType": "material", "user": {"isSeller": True, "sellerName": "Arghanion's Puzzlebox", "uid": "seller-2"},
            "startingPrice": {"price": 24.99, "offerId": "6535fdba83504fad966388c0c303b123"},
        }},
        {"listing": {
            "uid": "7daaf336-d19a-4510-8151-92818bb48cf6", "title": "Atlantis Ruins / 37 Assets",
            "listingType": "3d-model", "user": {"isSeller": True, "sellerName": "PackDev", "uid": "seller-3"},
            "startingPrice": {"price": 29.99, "offerId": "73c8307d171248df8028b9b11151c9b9"},
        }},
    ],
}


class TestRealBlade:
    def test_reads_all_three_assets(self):
        listings = parse_free_listings(BLADE)
        assert [item["title"] for item in listings] == [
            "Dragon Cave", "Surface Forge v1.1.5", "Atlantis Ruins / 37 Assets"]
        assert listings[0] == {
            "title": "Dragon Cave",
            "url": "https://www.fab.com/listings/a7000a7c-c5fe-45f3-b8df-3e85c576b6c1",
            "uid": "a7000a7c-c5fe-45f3-b8df-3e85c576b6c1",
            "seller": "Denys Rutkovskyi",
            "listing_type": "3d-model",
            "ends": "",
        }

    def test_seller_comes_from_the_nested_object(self):
        # The live payload nests the seller; a plain str() would leak the whole dict.
        assert _seller_name({"sellerName": "PackDev", "isSeller": True}) == "PackDev"
        assert _seller_name("Legacy String") == "Legacy String"
        assert _seller_name(None) == "" and _seller_name({}) == ""

    def test_original_price_does_not_disqualify_an_asset(self):
        # The payload keeps the pre-promotion price, so a "price == 0" filter finds nothing.
        assert all(t["listing"]["startingPrice"]["price"] > 0 for t in BLADE["tiles"])
        assert len(parse_free_listings(BLADE)) == 3


class TestFreePromotionGuard:
    """The only signal that this block really is the giveaway."""

    @pytest.mark.parametrize("flag", [False, None, "true", 1, 0])
    def test_nothing_is_taken_without_the_flag(self, flag):
        assert parse_free_listings(dict(BLADE, isLimitedFreeContent=flag)) == []

    def test_missing_flag_takes_nothing(self):
        payload = {k: v for k, v in BLADE.items() if k != "isLimitedFreeContent"}
        assert parse_free_listings(payload) == []

    @pytest.mark.parametrize("block", ["banners", "creators", "", None])
    def test_other_block_contents_are_ignored(self, block):
        assert parse_free_listings(dict(BLADE, blockContentType=block)) == []


class TestBrokenPayloads:
    @pytest.mark.parametrize("payload", [{}, None, [], "not a dict", {"tiles": []}])
    def test_unusable_input_gives_an_empty_list(self, payload):
        assert parse_free_listings(payload) == []

    def test_tile_without_a_listing_id_is_skipped(self):
        payload = dict(BLADE, tiles=[
            {"listing": {"title": "No id here"}},
            {},
            {"listing": None},
            BLADE["tiles"][0],
        ])
        listings = parse_free_listings(payload)
        assert [item["uid"] for item in listings] == ["a7000a7c-c5fe-45f3-b8df-3e85c576b6c1"]

    def test_missing_title_still_yields_a_usable_entry(self):
        payload = dict(BLADE, tiles=[{"listing": {"uid": "abc"}}])
        assert parse_free_listings(payload) == [{
            "title": "Fab asset", "url": "https://www.fab.com/listings/abc",
            "uid": "abc", "seller": "", "listing_type": "", "ends": "",
        }]


class TestListingPageWording:
    """The page has the final word before anything is clicked."""

    FREE_PAGE = {
        "title": "Dragon Cave | Fab",
        "buttons": ["PERSONAL zł 182.91 Free* -100% Sale ends 08/11", "Buy now", "Add to cart"],
        "body": "Dragon Cave Free* -100%",
    }
    PAID_PAGE = {
        "title": "Some Asset | Fab",
        "buttons": ["PERSONAL zł 79.99", "Buy now", "Add to cart"],
        "body": "Some Asset regular price",
    }

    @pytest.fixture
    def claimer(self):
        return FabClaimer()

    def test_free_listing_is_recognised(self, claimer):
        assert claimer._is_free_now(self.FREE_PAGE)

    @pytest.mark.parametrize("page", [PAID_PAGE, {}, {"buttons": [], "body": ""}])
    def test_a_paid_page_is_never_claimed(self, claimer, page):
        assert not claimer._is_free_now(page)

    def test_shares_the_epic_browser_profile(self, claimer):
        # Riding on Epic's session is the whole reason Fab never opens a second login.
        assert claimer.store_name == "fab"
        assert claimer.profile_name == "epic"


class TestNotificationVocabulary:
    """Statuses drive the summary filter in main.py, so they may not drift into free text."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic_fab.py").read_text(encoding="utf-8")
    ALLOWED = re.compile(r"^(claimed|existed|notified|available \(dry run\)|(failed|skipped)(:[a-z-]+)?)$")

    def test_every_status_follows_the_shared_vocabulary(self):
        statuses = set(re.findall(r'"status": "([^"]+)"', self.SOURCE))
        statuses |= set(re.findall(r'status = "([^"]+)"', self.SOURCE))
        assert statuses, "no statuses found, the scan stopped matching"
        unexpected = sorted(s for s in statuses if not self.ALLOWED.match(s))
        assert not unexpected, f"statuses outside the shared vocabulary: {unexpected}"

    def test_vnc_prompts_are_prefixed_with_the_store_name(self):
        titles = re.findall(r'self\._vnc_notice\(\s*"([^"]+)"', self.SOURCE)
        assert titles, "no VNC prompts found, the scan stopped matching"
        assert all(t.startswith("Fab: ") for t in titles), titles


class TestCheckoutOrdersOnlyAtZero:
    """Epic's checkout frame also sells assets, so Fab orders only when its total reads as zero (audit 2026-10-14)."""

    class _Stub:
        def __init__(self, texts):
            self.texts, self.clicked = list(texts), []

        async def sleep(self, seconds):
            pass

        async def _checkout_document(self):
            return object()

        async def _clear_checkout_challenge(self, title):
            return True

        async def _checkout_eval(self, document, script):
            if script == CHECKOUT_STATE_JS:
                return {"text": self.texts.pop(0) if len(self.texts) > 1 else self.texts[0], "buttons": ["Place Order"]}
            if script == CHECKOUT_CONSENT_OPEN_JS:
                return False
            return None

        async def _click_in_checkout(self, document, finder, label):
            self.clicked.append("Place Order")
            return True

    def _checkout(self, *texts):
        stub = self._Stub(texts)
        return asyncio.run(FabClaimer._complete_checkout(stub)), stub.clicked

    def test_a_price_is_never_ordered(self):
        assert self._checkout("Order Summary Asset Price €79.99 Total €79.99 Place Order") == ("not-free", [])

    def test_a_zero_total_is_ordered(self):
        assert self._checkout("Order Summary Asset Price €79.99 Discount -€79.99 Total €0.00 Place Order") == (
            True, ["Place Order"])

    def test_a_total_that_never_shows_is_not_ordered(self):
        assert self._checkout("Loading your order") == (False, [])

    def test_the_total_may_arrive_after_a_moment(self):
        assert self._checkout("Loading your order", "Total €0.00 Place Order") == (True, ["Place Order"])

    def test_the_newer_checkout_without_a_total_line_is_ordered(self):
        # Live, 20.10: all three free assets stopped here as "failed:checkout".
        text = ("Checkout N N*** Armored Stone Golem - Dark Fantasy Boss PLN 0.00 This is free. "
                "Add it to your library to get started. Add to library")
        stub = self._Stub([text])
        assert asyncio.run(FabClaimer._complete_checkout(stub, "Armored Stone Golem - Dark Fantasy Boss")) is True
        assert stub.clicked == ["Place Order"]


class TestFabSeesAPuzzleInEpicsCheckout:
    """Fab looked only at its own page, so a puzzle inside Epic's checkout frame went unseen (2026-10-30)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic_fab.py").read_text(encoding="utf-8")

    def _clear(self, monkeypatch, reason, ask_setting=False):
        from src.stores import epic_fab
        monkeypatch.setattr(epic_fab.cfg, "eg_captcha_ask", ask_setting)
        waited = []

        async def read():
            return reason

        async def wait_out(label, **kwargs):
            waited.append((kwargs["settle"], kwargs["ask"]))
            return False

        stub = type("Stub", (), {"_meeting": False, "_with_you": False, "_claiming": {"uid": "abc"}, "_seen_at_prompt": None, "_book": FabClaimer._book, "store_name": "fab", "_checkout_puzzle": False, "_ends_now": None})()
        stub._checkout_challenge_reason, stub._wait_out_challenge = read, wait_out
        cleared = asyncio.run(FabClaimer._clear_checkout_challenge(stub, "Asset"))
        return cleared, waited, stub._checkout_puzzle

    @pytest.mark.parametrize("ask_setting,expected", [(False, (12, False)), (True, (12, True))])
    def test_a_puzzle_waits_for_the_next_run_unless_eg_captcha_ask(self, monkeypatch, ask_setting, expected):
        cleared, waited, flagged = self._clear(monkeypatch, "checkout frame, frame: https://newassets.hcaptcha.com/x", ask_setting)
        assert cleared is False and waited == [expected] and flagged is True

    def test_a_check_that_passes_by_itself_gets_three_minutes(self, monkeypatch):
        cleared, waited, flagged = self._clear(monkeypatch, "page title: just a moment...")
        assert waited == [(180, True)] and flagged is False

    def test_a_puzzle_that_went_by_itself_does_not_stop_the_run(self, monkeypatch):
        from src.stores import epic_fab
        monkeypatch.setattr(epic_fab.cfg, "eg_captcha_ask", False)

        async def read():
            return "checkout frame, frame: https://newassets.hcaptcha.com/x"

        async def gone(label, **kwargs):
            return True

        stub = type("Stub", (), {"_meeting": False, "_with_you": False, "_claiming": {"uid": "abc"}, "_seen_at_prompt": None, "_book": FabClaimer._book, "store_name": "fab", "_checkout_puzzle": False, "_ends_now": None})()
        stub._checkout_challenge_reason, stub._wait_out_challenge = read, gone
        assert asyncio.run(FabClaimer._clear_checkout_challenge(stub, "Asset")) is True and stub._checkout_puzzle is False

    def test_the_frame_is_looked_at_again_once_the_order_is_placed(self):
        # An EU account places the order with the waiver, and that can draw the puzzle too.
        checkout = self.SOURCE.split("async def _complete_checkout", 1)[1].split("\n    async def ", 1)[0]
        assert checkout.count('else "captcha"') == 2

    def test_the_frame_is_looked_at_right_after_add_to_library(self):
        checkout = self.SOURCE.split("async def _complete_checkout", 1)[1].split("\n    async def ", 1)[0]
        assert checkout.index("CHECKOUT_ADD_FINDER") < checkout.index("if not await self._clear_checkout_challenge(title):")
        assert 'return "captcha"' in checkout

    @pytest.mark.parametrize("handed,answer,noted", [(True, [-1, 0], True), (True, [-1, 2187], False), (False, [-1, 0], False)])
    def test_only_a_puzzle_that_went_to_you_and_left_no_answer_is_noted(self, monkeypatch, handed, answer, noted):
        # Not handed: it cleared in the first seconds, no VNC notice. A filled answer field: you solved it.
        from src.stores import epic_fab
        monkeypatch.setattr(epic_fab.cfg, "eg_captcha_ask", True)

        async def read():
            return "checkout frame, frame: https://newassets.hcaptcha.com/x"

        async def gone(label, **kwargs):
            stub._handed_over = handed
            return True

        async def lengths():
            return answer

        stub = type("Stub", (), {"_meeting": False, "_with_you": False, "_claiming": {"uid": "abc"}, "_seen_at_prompt": None, "_book": FabClaimer._book, "store_name": "fab", "_checkout_puzzle": False, "_ends_now": None, "_puzzle_asked": False})()
        stub._checkout_challenge_reason, stub._wait_out_challenge, stub._captcha_answer_lengths = read, gone, lengths
        assert asyncio.run(FabClaimer._clear_checkout_challenge(stub, "Asset")) is True
        assert stub._puzzle_asked is noted and stub._checkout_puzzle is False

    def test_the_answer_field_is_read_inside_parentheses(self):
        # Checked in Chrome 2.11: "return" and a line break returned nothing, so the field always read as missing.
        assert "return ({CAPTCHA_ANSWER_JS})" in self.SOURCE

    def test_after_a_puzzle_only_assets_you_lack_wait_for_the_next_run(self):
        run = self.SOURCE.split("async def run(", 1)[1].split("\n    async def ", 1)[0]
        stop = run.index("if self._checkout_puzzle and not cfg.eg_captcha_ask")
        assert stop < run.index("await self._already_recorded(item[\"uid\"])", stop) < run.index("await self._claim_listing(item)")



class TestAnExpiredPuzzleIsNotASolvedOne:
    """A puzzle handed to you vanishes when it expires too, about two minutes later (TODO 67)."""

    def _claim(self, monkeypatch, asked, viewers=frozenset(), scheduled=True):
        from src.core import run_state
        from src.stores import epic_fab
        monkeypatch.setattr(epic_fab.cfg, "dryrun", False)
        monkeypatch.setattr(epic_fab, "vnc_viewers", lambda: None if viewers is None else set(viewers))
        if scheduled:
            monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        run_state.reset_run_state()
        claimer, remembered = FabClaimer.__new__(FabClaimer), []
        claimer.notify_games = []
        # Two earlier assets already used both mouse clicks on these buttons.
        claimer._click_counts = {"add to library (checkout frame)": 2, "accept the waiver (checkout frame)": 2}

        async def nothing(*_a, **_kw):
            return None

        async def no(*_a, **_kw):
            return False

        async def yes(*_a, **_kw):
            return True

        async def empty(*_a, **_kw):
            return set()

        async def state(*_a, **_kw):
            return {}

        async def checkout(_title):
            claimer.counts_at_checkout = dict(claimer._click_counts)
            claimer._puzzle_asked = asked
            return True

        async def remember(*args):
            remembered.append(args)

        claimer.page = type("Page", (), {"get": staticmethod(nothing)})()
        for name, stub in (("sleep", nothing), ("_human_challenge_present", no), ("_page_state", state),
                           ("_owned_uids", empty), ("_already_recorded", no), ("_read_lately", no), ("_click_buy_now", yes),
                           ("_accept_eula", yes), ("_complete_checkout", checkout), ("_remember", remember)):
            monkeypatch.setattr(claimer, name, stub)
        monkeypatch.setattr(claimer, "_is_free_now", lambda _state: True)
        try:
            asyncio.run(claimer._claim_listing({"title": "Dragon Cave", "uid": "abc", "url": "https://www.fab.com/listings/abc"}))
            return claimer, remembered, run_state.waits_for_nobody("fab"), (run_state.waiting_for_you(), run_state.booked())
        finally:
            run_state.reset_run_state()

    def test_an_asset_missing_after_your_puzzle_is_a_captcha_left_for_the_next_run(self, monkeypatch):
        claimer, remembered, nobody, waiting = self._claim(monkeypatch, asked=True)
        # The rest of the run waits, instead of drawing a puzzle and a ping for every asset.
        assert claimer._checkout_puzzle is True and nobody is True and remembered == []
        # With a next run in 12 hours it waits for you in noVNC, listed apart: not failed, not "Needed you".
        needed, kept = waiting
        assert claimer.notify_games == [] and needed == {} and [item["title"] for item in kept["fab"]] == ["Dragon Cave"]

    def test_a_single_run_keeps_nothing_and_ends(self, monkeypatch):
        claimer, _, _, (needed, kept) = self._claim(monkeypatch, asked=True, scheduled=False)
        assert claimer.notify_games[0]["status"] == "failed:captcha" and kept == {} and needed

    def test_without_novnc_to_watch_it_is_a_captcha_for_the_next_run(self, monkeypatch):
        claimer, _, _, (needed, kept) = self._claim(monkeypatch, asked=True, viewers=None)
        assert claimer.notify_games[0]["status"] == "failed:captcha" and kept == {} and needed

    def test_every_asset_gets_its_own_mouse_clicks(self, monkeypatch):
        # Live 3.11: the third asset found the two clicks per button spent and was clicked by script.
        claimer, _, _, _ = self._claim(monkeypatch, asked=False)
        assert claimer.counts_at_checkout == {}

    def test_without_a_puzzle_it_stays_unconfirmed(self, monkeypatch):
        claimer, remembered, nobody, _ = self._claim(monkeypatch, asked=False)
        assert claimer.notify_games[0]["status"] == "failed:unconfirmed" and remembered == [] and nobody is False


class TestFabClicksLikeAHand:
    """Fab pressed every button by script (isTrusted false) on the Epic account; now Epic's own mouse loop, script only as a fallback."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "epic_fab.py").read_text(encoding="utf-8")

    class _Frame:
        """Epic's checkout frame at (100, 300) on the page, its button at (50, 20) inside it; reads follow `boxes`."""

        def __init__(self, boxes, hit=True):
            self.boxes, self.hit, self.calls, self._checkout_iframe = list(boxes), hit, [], object()
            self.__dict__["_click_counts"] = {}

        logger = __import__("logging").getLogger("test")

        async def sleep(self, seconds):
            pass

        async def _human_click(self, x, y, w=0, h=0):
            self.calls.append(("mouse", x, y))

        async def _bring_into_view(self, read, run_js, wheel_point, finder, label):
            return await read()

        async def _read_box(self, run_js, finder):
            return self.boxes.pop(0) if len(self.boxes) > 1 else self.boxes[0]

        async def _checkout_eval(self, node, script, *args):
            if script.startswith("function() {" + chr(10) + "    const f = this"):
                return {"x": 100, "y": 300, "w": 900, "top": 300, "bottom": 720}
            if "elementFromPoint" in script:
                return self.hit
            if "el.click(); return true" in script:
                self.calls.append(("script",))
                return True
            return None

    def _click(self, frame):
        from src.core.claimer import BaseClaimer
        from src.stores.epic_fab import CHECKOUT_ADD_FINDER
        for name in ("_click_until_taken", "_click_took", "_script_click_js"):
            setattr(frame, name, getattr(BaseClaimer, name).__get__(frame) if name != "_script_click_js" else BaseClaimer._script_click_js)
        return asyncio.run(FabClaimer._click_in_checkout(frame, object(), CHECKOUT_ADD_FINDER, "add to library")), frame.calls

    BUTTON = {"x": 50, "y": 20, "key": "BUTTON|Place Order"}

    def test_the_button_is_clicked_with_the_mouse_where_the_frame_puts_it(self):
        clicked, calls = self._click(self._Frame([self.BUTTON, {"reason": "missing"}]))
        # The spot in the frame (50, 20) plus where the frame sits on the page (100, 300).
        assert clicked is True and calls == [("mouse", 150, 320)]

    def test_a_button_the_waiver_covers_after_the_click_counts_as_clicked(self):
        # An EU account: the waiver opens over Place order, which stays enabled; pressing it again by script was the signal.
        clicked, calls = self._click(self._Frame([self.BUTTON, {"reason": "covered"}]))
        assert clicked is True and calls == [("mouse", 150, 320)]

    def test_a_spot_covered_on_the_page_is_never_clicked_through(self):
        clicked, calls = self._click(self._Frame([self.BUTTON], hit=False))
        assert clicked is False and calls == []

    def test_a_button_two_mouse_clicks_did_not_move_is_pressed_by_script(self):
        clicked, calls = self._click(self._Frame([self.BUTTON]))
        assert calls[:2] == [("mouse", 150, 320), ("mouse", 150, 320)] and calls[-1] == ("script",)

    def test_only_buttons_a_person_can_see_are_picked(self):
        from src.stores.epic_fab import CHECKOUT_ACCEPT_FINDER, CHECKOUT_ADD_FINDER
        assert "add to library|place order|get now" in CHECKOUT_ADD_FINDER and "getBoundingClientRect" in CHECKOUT_ADD_FINDER
        assert "getBoundingClientRect" in CHECKOUT_ACCEPT_FINDER

    @pytest.mark.parametrize("accept,expected", [(False, ("eula", ["Place Order"])), (True, (True, ["Place Order", "waiver"]))])
    def test_the_waiver_is_pressed_only_with_fab_accept_eula(self, monkeypatch, accept, expected):
        from src.stores import epic_fab

        monkeypatch.setattr(epic_fab.cfg, "fab_accept_eula", accept)
        clicked = []

        class Stub:
            async def sleep(self, seconds):
                pass

            async def _checkout_document(self):
                return object()

            async def _clear_checkout_challenge(self, title):
                return True

            async def _checkout_eval(self, document, script, *args):
                if script == CHECKOUT_STATE_JS:
                    return {"text": "Dragon Cave Price 0.00 Total 0.00", "buttons": ["Place Order"]}
                return script == CHECKOUT_CONSENT_OPEN_JS

            async def _click_in_checkout(self, document, finder, label):
                clicked.append("waiver" if "accept" in label else "Place Order")
                return True

        result = asyncio.run(FabClaimer._complete_checkout(Stub(), "Dragon Cave"))
        assert (result, clicked) == expected
