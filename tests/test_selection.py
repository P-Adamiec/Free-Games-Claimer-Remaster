"""The register of stores active in a run.

GamerPower reads it before delegating, so a user running STORES=steam,gamerpower
does not get Epic games claimed behind their back.
"""

import pytest

from src.core.selection import (apply_run_selection, is_store_active, reset_active_stores,
                                set_active_stores)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_active_stores()
    yield
    reset_active_stores()


class TestUnset:
    """Nothing recorded means a direct call, so nothing is restricted."""

    @pytest.mark.parametrize("store", ["steam", "epic", "gog", "anything"])
    def test_everything_passes(self, store):
        assert is_store_active(store)


class TestRecorded:
    def test_only_the_recorded_stores_pass(self):
        set_active_stores(["steam", "gamerpower"])
        assert is_store_active("steam")
        assert is_store_active("gamerpower")
        assert not is_store_active("epic")
        assert not is_store_active("gog")

    @pytest.mark.parametrize("given,asked", [
        ("Epic", "epic"),
        ("epic", "EPIC"),
        (" gog ", "gog"),
    ])
    def test_case_and_spacing_do_not_matter(self, given, asked):
        set_active_stores([given])
        assert is_store_active(asked)

    def test_empty_selection_blocks_everything(self):
        set_active_stores([])
        assert not is_store_active("steam")

    @pytest.mark.parametrize("junk", [None, "", "   "])
    def test_blank_entries_are_dropped(self, junk):
        set_active_stores(["steam", junk])
        assert is_store_active("steam")
        assert not is_store_active("")

    def test_reset_goes_back_to_unrestricted(self):
        set_active_stores(["steam"])
        reset_active_stores()
        assert is_store_active("epic")


class TestRunSelection:
    """What main.py records, which is not always the raw list of stores."""

    def test_a_mixed_selection_is_restricted(self):
        apply_run_selection(["steam", "gamerpower"])
        assert is_store_active("steam")
        assert not is_store_active("epic")

    def test_gamerpower_alone_restricts_nothing(self):
        # Picking only GamerPower expresses no preference between the stores it feeds.
        apply_run_selection(["gamerpower"])
        assert is_store_active("epic")
        assert is_store_active("steam")

    def test_the_defaults_still_allow_every_default(self):
        apply_run_selection(["steam", "epic", "fab", "prime", "gog", "ubisoft",
                             "aliexpress", "gamerpower"])
        assert is_store_active("epic")
        assert not is_store_active("unity")
