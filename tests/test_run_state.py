"""What one run remembers about the person at the keyboard.

A prompt nobody answered ends that store's manual work for the run; a prompt you did
answer costs it nothing, so the next screen gets the full wait again.
"""

import pytest

from src.core import run_state


@pytest.fixture(autouse=True)
def _clean_run():
    run_state.reset_run_state()
    yield
    run_state.reset_run_state()


class TestWhoIsWaiting:
    def test_a_fresh_run_waits_for_everyone(self):
        assert run_state.waits_for_nobody("itchio") is False

    def test_an_unanswered_prompt_closes_that_store(self):
        run_state.mark_unanswered("itchio")
        assert run_state.waits_for_nobody("itchio") is True

    def test_it_closes_nothing_else(self):
        run_state.mark_unanswered("itchio")
        assert run_state.waits_for_nobody("gog") is False

    def test_answering_leaves_the_store_open(self):
        run_state.mark_answered("itchio")
        assert run_state.waits_for_nobody("itchio") is False

    def test_answering_reopens_a_closed_store(self):
        # You were away, then you came back: the next screen still gets its full wait.
        run_state.mark_unanswered("itchio")
        run_state.mark_answered("itchio")
        assert run_state.waits_for_nobody("itchio") is False

    @pytest.mark.parametrize("written,asked", [("Itch.io", "itch.io"), ("GOG", "gog"), ("gog", "GOG")])
    def test_the_name_is_matched_however_it_is_written(self, written, asked):
        run_state.mark_unanswered(written)
        assert run_state.waits_for_nobody(asked) is True

    def test_a_blank_name_is_ignored(self):
        run_state.mark_unanswered("")
        assert run_state.waiting_for_you() == {}
        assert run_state.waits_for_nobody("") is False


class TestWhatWasMissed:
    def test_nothing_missed_by_default(self):
        assert run_state.waiting_for_you() == {}

    def test_skips_add_up_per_store(self):
        run_state.needs_you("itchio")
        run_state.needs_you("itchio", 6)
        run_state.needs_you("fanatical")
        assert run_state.waiting_for_you() == {"itchio": 7, "fanatical": 1}

    def test_the_caller_cannot_change_it_by_accident(self):
        run_state.needs_you("itchio")
        run_state.waiting_for_you()["itchio"] = 99
        assert run_state.waiting_for_you() == {"itchio": 1}


class TestEveryRunStartsClean:
    def test_reset_clears_both(self):
        # The container runs for weeks; a store that was waiting this morning gets another go.
        run_state.mark_unanswered("itchio")
        run_state.needs_you("itchio", 3)
        run_state.reset_run_state()
        assert run_state.waits_for_nobody("itchio") is False
        assert run_state.waiting_for_you() == {}
