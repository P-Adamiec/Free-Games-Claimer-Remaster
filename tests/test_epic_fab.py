"""Fab limited-time free asset detection.

Pins two live traps: the payload keeps each listing's original price, and only
`isLimitedFreeContent` may authorise a claim.
"""

import re
from pathlib import Path

import pytest

from src.stores.epic_fab import FabClaimer, _seller_name, parse_free_listings

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
            "uid": "abc", "seller": "", "listing_type": "",
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
