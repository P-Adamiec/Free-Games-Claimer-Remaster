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
        run_state.needs_you("itchio", 1, "giveaway")
        run_state.needs_you("itchio", 6, "giveaway")
        run_state.needs_you("fanatical", 1, "giveaway")
        signed = run_state.NOT_SIGNED_IN
        assert run_state.waiting_for_you() == {("itchio", signed, "giveaway"): 7, ("fanatical", signed, "giveaway"): 1}

    def test_each_reason_and_unit_is_counted_on_its_own(self):
        # "Needed you" has to say what was missing and what waits, not only how much.
        run_state.needs_you("steam", 2, "game")
        run_state.needs_you("steam", 3, "key")
        run_state.needs_you("epic", 2, "game", why=run_state.CAPTCHA_UNSOLVED)
        assert run_state.waiting_for_you() == {("steam", run_state.NOT_SIGNED_IN, "game"): 2,
                                               ("steam", run_state.NOT_SIGNED_IN, "key"): 3,
                                               ("epic", run_state.CAPTCHA_UNSOLVED, "game"): 2}

    def test_the_caller_cannot_change_it_by_accident(self):
        run_state.needs_you("itchio", 1, "giveaway")
        run_state.waiting_for_you()[("itchio", run_state.NOT_SIGNED_IN, "giveaway")] = 99
        assert run_state.waiting_for_you() == {("itchio", run_state.NOT_SIGNED_IN, "giveaway"): 1}


class TestTheSentenceSaysWhatWaits:
    """No shorthand: the reason as a sentence, and what the number counts."""

    @pytest.mark.parametrize("why,count,item,sentence", [
        (run_state.CAPTCHA_UNSOLVED, 3, "game", "a captcha was not solved, so 3 games wait for the next run."),
        (run_state.CAPTCHA_UNSOLVED, 1, "game", "a captcha was not solved, so 1 game waits for the next run."),
        (run_state.NOT_SIGNED_IN, 3, "key", "the sign-in was not finished, so 3 keys wait for the next run."),
        (run_state.NO_ANSWER, 1, "giveaway", "nobody answered over VNC, so 1 giveaway waits for the next run."),
        (run_state.NOT_SIGNED_IN, 0, "game", "the sign-in was not finished, so nothing was claimed."),
    ])
    def test_it_reads_as_a_sentence(self, why, count, item, sentence):
        assert run_state.describe_wait(why, count, item) == sentence


class TestAnUnsolvedCaptchaStopsTheStore:
    """A captcha nobody solved: the store's other games wait for the next run instead of drawing more checks."""

    class _Store:
        store_name = "epic"

        class logger:
            @staticmethod
            def warning(*args):
                pass

    @staticmethod
    def _holds(ends=None):
        from src.core.claimer import BaseClaimer
        return BaseClaimer._captcha_holds(TestAnUnsolvedCaptchaStopsTheStore._Store(), ends)

    def test_a_store_you_answered_goes_on(self):
        assert self._holds() is False and run_state.waiting_for_you() == {}

    def test_after_an_unsolved_captcha_the_rest_is_counted_and_left(self):
        from src.core.claimer import BaseClaimer
        run_state.mark_unanswered("epic")
        assert self._holds() is True
        BaseClaimer._captcha_held(self._Store(), 3, "game")
        assert run_state.waiting_for_you() == {("epic", run_state.CAPTCHA_UNSOLVED, "game"): 3}

    def test_an_item_on_its_last_run_is_still_tried(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        run_state.mark_unanswered("epic")
        assert self._holds(datetime.now(timezone.utc) + timedelta(hours=2)) is False
        assert self._holds(datetime.now(timezone.utc) + timedelta(days=3)) is True

    def test_a_single_run_holds_everything_after_nobody_answered(self, monkeypatch):
        # Audit 4.11: RUN_ONCE took every known end for a last run, so after an unanswered puzzle it opened every checkout.
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(run_state, "_next_run_source", None)
        run_state.mark_unanswered("epic")
        assert self._holds(datetime.now(timezone.utc) + timedelta(hours=2)) is True

    def test_nothing_held_says_nothing(self):
        from src.core.claimer import BaseClaimer
        BaseClaimer._captcha_held(self._Store(), 0, "game")
        assert run_state.waiting_for_you() == {}

    def test_another_store_is_not_held_back(self):
        run_state.mark_unanswered("fab")
        assert self._holds() is False


class TestEveryRunStartsClean:
    def test_reset_clears_both(self):
        # The container runs for weeks; a store that was waiting this morning gets another go.
        run_state.mark_unanswered("itchio")
        run_state.needs_you("itchio", 3, "giveaway")
        run_state.reset_run_state()
        assert run_state.waits_for_nobody("itchio") is False
        assert run_state.waiting_for_you() == {}
