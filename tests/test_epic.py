"""Epic claim verification (issue #39).

Walking Epic's checkout only proves what that page displayed, so a claim counts
only once the product page itself reports the game as owned.
"""

from pathlib import Path

import pytest

from src.stores.epic import PAGE_STATE_JS, is_owned


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
