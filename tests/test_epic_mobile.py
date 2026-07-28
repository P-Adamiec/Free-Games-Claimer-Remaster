"""Epic mobile giveaway detection.

Each weekly mobile game also carries a full-price entry for when the promo ends,
so only an active 0-price "Claim" may be picked up.
"""

import asyncio

from src.stores.epic_mobile import (
    PRODUCT_URL,
    _is_claimable_free,
    fetch_mobile_free_games,
    parse_free_games,
)


def _section(slug, title="Some Game", purchase=None, section_type="freeGame"):
    return {
        "type": section_type,
        "offers": [{
            "content": {
                "title": title,
                "mapping": {"slug": slug},
                "purchase": purchase if purchase is not None else [
                    {"purchaseType": "Claim", "price": {"decimalPrice": 0}},
                ],
            }
        }],
    }


class TestOfferFilter:
    def test_free_claim_is_taken(self):
        assert _is_claimable_free({"purchase": [{"purchaseType": "Claim", "price": {"decimalPrice": 0}}]})

    def test_paid_entry_is_rejected(self):
        assert not _is_claimable_free({"purchase": [{"purchaseType": "Purchase", "price": {"decimalPrice": 21}}]})

    def test_zero_price_purchase_is_not_a_claim(self):
        assert not _is_claimable_free({"purchase": [{"purchaseType": "Purchase", "price": {"decimalPrice": 0}}]})

    def test_promo_plus_future_price_is_still_free_now(self):
        assert _is_claimable_free({"purchase": [
            {"purchaseType": "Claim", "price": {"decimalPrice": 0}},
            {"purchaseType": "Purchase", "price": {"decimalPrice": 21}},
        ]})

    def test_missing_data(self):
        assert not _is_claimable_free({})
        assert not _is_claimable_free({"purchase": None})


class TestParsing:
    def test_builds_desktop_product_url_and_label(self):
        games = parse_free_games({"data": [_section("foretales-android-7c6e1c", "Foretales")]}, "android")
        assert games == [{
            "title": "Foretales (Android)",
            "url": PRODUCT_URL.format(slug="foretales-android-7c6e1c"),
            "platform": "android",
            "label": "Android",
        }]

    def test_ios_label(self):
        games = parse_free_games({"data": [_section("foretales-ios-7f68e5", "Foretales")]}, "ios")
        assert games[0]["title"] == "Foretales (iOS)"

    def test_other_sections_are_ignored(self):
        data = {"data": [_section("some-game", section_type="featured")]}
        assert parse_free_games(data, "android") == []

    def test_paid_offers_are_skipped(self):
        data = {"data": [_section("paid-game", purchase=[{"purchaseType": "Purchase", "price": {"decimalPrice": 21}}])]}
        assert parse_free_games(data, "android") == []

    def test_offer_without_slug_is_skipped(self):
        data = {"data": [{"type": "freeGame", "offers": [{"content": {"title": "No slug"}}]}]}
        assert parse_free_games(data, "android") == []

    def test_empty_and_broken_responses(self):
        assert parse_free_games({}, "android") == []
        assert parse_free_games({"data": None}, "android") == []


class TestPlatformSelection:
    def test_empty_list_means_no_platforms(self):
        # A falsy list must not silently fall back to "all platforms".
        assert asyncio.run(fetch_mobile_free_games(None, [])) == []

    def test_unknown_platform_is_ignored(self):
        assert asyncio.run(fetch_mobile_free_games(None, ["windows"])) == []

    def test_fetch_failure_does_not_raise(self):
        async def boom(url):
            raise RuntimeError("network down")

        assert asyncio.run(fetch_mobile_free_games(boom, ["android"])) == []

    def test_duplicate_urls_across_platforms_are_collapsed(self):
        payload = {"data": [_section("same-slug")]}

        async def fetch(url):
            return payload

        games = asyncio.run(fetch_mobile_free_games(fetch, ["android", "ios"]))
        assert len(games) == 1
