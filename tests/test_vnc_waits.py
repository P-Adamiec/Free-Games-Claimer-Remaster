"""Waiting for the person at the keyboard.

Two rules, both learned the hard way. Every wait has to honour VNC_LOGIN_TIMEOUT,
because someone may be away from the machine. And while it waits, the bot may only
look at the page: reloading or clicking every few seconds wipes the code you are typing.
"""

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from src.core.claimer import BaseClaimer

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "src").rglob("*.py"))

# Reading is fine, anything that changes the page under the user's hands is not.
TOUCHES_PAGE = ("page.get(", "click", "send_keys", "reload(")
# A check that does touch the page has to look at the address first and back off.
GUARDS = ("url_has_allowed_host(", "_code_screen_present(")


def _calls(name: str):
    """Every call to `name` across src/, as (path, node) pairs."""
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
               and node.func.attr == name:
                yield path, node


def _check_functions():
    """Every function handed to _wait_for_vnc_login, with its source."""
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        wanted = {
            ast.unparse(node.args[0]).split(".")[-1]
            for _, node in [(path, n) for n in ast.walk(tree)
                            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == "_wait_for_vnc_login" and n.args]
        }
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in wanted:
                body = "\n".join(lines[node.lineno - 1:node.end_lineno])
                yield f"{path.name}:{node.name}", body


class TestTheSettingReachesEveryWait:
    def test_the_scan_finds_the_waits(self):
        # Guards the scan itself: a rename must not turn these tests into a no-op.
        assert len(list(_calls("_wait_for_vnc_login"))) > 10

    @pytest.mark.parametrize("name", ["_wait_for_vnc_login", "_vnc_notice"])
    def test_no_call_hardcodes_a_number(self, name):
        for path, node in _calls(name):
            values = [kw.value for kw in node.keywords if kw.arg == "timeout"]
            values += node.args[2:3] if name == "_vnc_notice" else []
            for value in values:
                assert not isinstance(value, ast.Constant), \
                    f"{path.name}:{node.lineno} passes a fixed {ast.unparse(value)}, use VNC_LOGIN_TIMEOUT"


class TestTheBotOnlyLooksWhileYouType:
    def test_the_scan_finds_the_checks(self):
        assert len(list(_check_functions())) > 10

    @pytest.mark.parametrize("name,body", list(_check_functions()), ids=lambda v: v if isinstance(v, str) and ":" in v else "")
    def test_a_check_that_touches_the_page_backs_off_first(self, name, body):
        if not any(marker in body for marker in TOUCHES_PAGE):
            return
        assert any(guard in body for guard in GUARDS), (
            f"{name} acts on the page while the user is typing. Look at the address first "
            f"and return False on a sign-in or code screen."
        )


class TestSteamGuardUsesTheSharedWait:
    SOURCE = (ROOT / "src" / "stores" / "steam.py").read_text(encoding="utf-8")

    def test_it_no_longer_counts_seconds_on_its_own(self):
        block = self.SOURCE.split("async def _handle_steam_guard", 1)[1].split("\n    async def ", 1)[0]
        assert "_wait_for_vnc_login(" in block
        assert "range(120)" not in block


class TestOneUnansweredPromptIsEnough:
    """Asking a store again after nobody answered costs an hour and lowers its trust in us."""

    @pytest.fixture()
    def claimer(self, monkeypatch):
        import asyncio as _asyncio

        from src.core import claimer as claimer_module
        from src.core import run_state

        run_state.reset_run_state()
        sent = []

        async def _no_wait(_seconds):
            return None

        monkeypatch.setattr(claimer_module.asyncio, "sleep", _no_wait)
        monkeypatch.setattr("src.core.notifier.notify", lambda *a, **kw: _asyncio.sleep(0))
        obj = claimer_module.BaseClaimer.__new__(claimer_module.BaseClaimer)
        obj.store_name = "itchio"
        obj.notified = sent
        yield obj
        run_state.reset_run_state()

    def test_the_first_wait_runs_and_then_closes_the_store(self, claimer):
        import asyncio

        from src.core import run_state

        async def _never() -> bool:
            return False

        assert asyncio.run(claimer._wait_for_vnc_login(_never, timeout=10)) is False
        assert run_state.waits_for_nobody("itchio") is True

    def test_the_next_wait_gives_up_at_once(self, claimer):
        import asyncio

        from src.core import run_state

        polled = []

        async def _never() -> bool:
            polled.append(1)
            return False

        run_state.mark_unanswered("itchio")
        assert asyncio.run(claimer._wait_for_vnc_login(_never, timeout=10)) is False
        assert polled == [], "the page was polled again for a store nobody is watching"

    def test_answering_keeps_the_store_open_for_the_next_screen(self, claimer):
        import asyncio

        from src.core import run_state

        async def _done() -> bool:
            return True

        run_state.mark_unanswered("itchio")
        run_state.mark_answered("itchio")
        assert asyncio.run(claimer._wait_for_vnc_login(_done, timeout=10)) is True
        assert run_state.waits_for_nobody("itchio") is False


class TestTheSideStoreLoopBacksOff:
    """The hours-long case: eight giveaways, each re-opening the same sign-in page."""

    SOURCE = (ROOT / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_it_asks_before_touching_the_site(self):
        block = self.SOURCE.split("for store, game in work:", 1)[1].split("except Exception", 1)[0]
        assert block.index("waits_for_nobody(store)") < block.index("_process_side_store(store, game)")

    def test_the_skip_is_counted_for_the_summary(self):
        block = self.SOURCE.split("for store, game in work:", 1)[1].split("except Exception", 1)[0]
        assert "needs_you(store, " in block

    def test_each_site_keeps_its_own_tally(self):
        # One claimer serves four sites, so Itch.io timing out must not silence Fanatical.
        assert "SIDE_STORE_KEYS" in self.SOURCE
        assert "store_key=side_store_key(label)" in self.SOURCE


class TestTheFormIsWaitedFor:
    """A human check vanishes a moment before the page reloads, so one look finds nothing."""

    SOURCE = (ROOT / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_the_retry_polls_instead_of_looking_once(self):
        block = self.SOURCE.split("async def _resubmit_when_form_returns", 1)[1] \
            .split("\n    async def ", 1)[0]
        assert "for _ in range(" in block and "_resubmit_login(" in block

    def test_the_login_path_uses_the_waiting_version(self):
        block = self.SOURCE.split("async def _confirm_side_login", 1)[1].split("\n    async def ", 1)[0]
        assert "_resubmit_when_form_returns(" in block

    def test_giving_up_says_so(self):
        block = self.SOURCE.split("async def _resubmit_when_form_returns", 1)[1] \
            .split("\n    async def ", 1)[0]
        assert "No sign-in form came back" in block


class TestEpicLeavesTheChallengeOnScreen:
    """You were sent to the browser to solve a captcha and found the store page instead."""

    SOURCE = (ROOT / "src" / "stores" / "epic.py").read_text(encoding="utf-8")

    def test_the_bot_finishes_the_sign_in_once_you_clear_the_check(self):
        # You solved the captcha and the filled form just sat there waiting for a click.
        branch = self.SOURCE.split("Captcha / security challenge detected", 1)[1].split("break", 1)[0]
        assert "_wait_out_challenge(" in branch and "_resume_after_check()" in branch
        resume = self.SOURCE.split("async def _resume_after_check", 1)[1].split("\n    async def ", 1)[0]
        assert "_press_sign_in()" in resume

    def test_a_captcha_hands_over_before_any_navigation(self):
        loop = self.SOURCE.split("The wait loop can also run out of time", 1)[1]
        handover = loop.index("if mfa_manual or challenge_blocked")
        assert handover < loop.index("await self.page.get(URL_CLAIM)")


class TestOnlyAVisibleChallengeCounts:
    """Epic keeps a full size hCaptcha frame on every sign-in page, hidden by style until needed.

    Checked against a real browser: the plain sign-in page reads as no challenge, while a
    visible widget, the Cloudflare wording and its page title all still read as one.
    """

    SOURCE = (ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
    JS = SOURCE.split("CHALLENGE_JS = ", 1)[1].split('"""', 2)[1]

    def test_the_page_check_runs_this_script(self):
        body = self.SOURCE.split("async def _human_challenge_reason", 1)[1].split("\n    async def ", 1)[0]
        assert "evaluate(CHALLENGE_JS)" in body
        present = self.SOURCE.split("async def _human_challenge_present", 1)[1].split("\n    async def ", 1)[0]
        assert "self._human_challenge_reason()" in present

    def test_a_widget_hidden_by_style_is_not_a_challenge(self):
        assert "visibility === 'hidden'" in self.JS
        assert "parseFloat(st.opacity) < 0.1" in self.JS

    def test_a_widget_something_else_covers_is_not_a_challenge(self):
        assert "elementFromPoint" in self.JS

    def test_every_provider_goes_through_that_same_check(self):
        assert "&& seen(f)" in self.JS
        assert "widgets.find(seen)" in self.JS

    def test_the_page_wide_interstitials_still_count(self):
        assert "just a moment" in self.JS
        assert "verify you are human" in self.JS


class TestAChallengeSeenElsewhere:
    """A checkout frame hides its captcha from the page check, so the wait can be told where to look."""

    SOURCE = (ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
    WAIT = SOURCE.split("async def _wait_out_challenge", 1)[1].split("\n    async def ", 1)[0]

    def test_the_default_is_still_the_page(self):
        assert "present = present_fn or self._human_challenge_present" in self.WAIT

    def test_both_the_settle_and_the_handover_use_it(self):
        assert self.WAIT.count("await present()") == 2
        assert "_human_challenge_present()" not in self.WAIT.split("present = ", 1)[1]


class TestRecaptchaOnUbisoft:
    """Ubisoft's sign-in shows reCAPTCHA Enterprise when it doubts a login, and the bot never saw it.

    Checked in a real browser: a visible picture challenge (bframe) and the checkbox count, a hidden
    bframe and an invisible reCAPTCHA's corner badge do not.
    """

    JS = (ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8").split("CHALLENGE_JS = ", 1)[1].split('"""', 2)[1]

    def test_the_picture_challenge_is_a_challenge(self):
        assert "(anchor|bframe)" in self.JS

    def test_the_invisible_badge_is_not(self):
        assert "size=invisible" in self.JS
        assert "!badge(f) && seen(f)" in self.JS


class TestAWaitThatNeverAsks:
    """A game you already have is not worth your time on a security check, so its wait only lets the check pass by itself."""

    class _Stub:
        logger = logging.getLogger("test")
        store_name = "epic"

        def __init__(self, present):
            self.present, self.asked, self.shots = list(present), False, []

        async def _human_challenge_present(self):
            return self.present.pop(0) if self.present else True

        async def _human_challenge_reason(self):
            return "frame: https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html"

        async def take_screenshot(self, name, failed=True):
            self.shots.append((name, failed))

        def _vnc_notice(self, *args):
            return "notice"

        async def _wait_for_vnc_login(self, check_fn, **kwargs):
            self.asked = True
            return True

    def _wait(self, present, ask, settle=0, stub=None):
        stub = stub or self._Stub(present)
        cleared = asyncio.run(BaseClaimer._wait_out_challenge(stub, "Epic", settle=settle, ask=ask))
        return cleared, stub.asked

    def test_before_asking_it_names_the_check_and_takes_a_screenshot(self, caplog):
        # take_screenshot() itself keeps it to DEBUG=true and sends it with DEBUG_SCREENSHOTS, like a failure.
        stub = self._Stub([])
        with caplog.at_level(logging.DEBUG, logger="test"):
            assert self._wait([], ask=True, stub=stub) == (True, True)
        assert "hcaptcha.html" in caplog.text
        assert stub.shots == [("challenge_epic", True)]

    def test_a_check_that_clears_in_the_last_two_seconds_never_pings_you(self, monkeypatch):
        async def no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        # Looks at 0, 2 ... 10 s see the check; it is gone by 12 s, where the old loop asked without looking.
        stub = self._Stub([True] * 6 + [False])
        assert self._wait([], ask=True, settle=12, stub=stub) == (True, False)

    def test_only_a_check_that_went_to_you_counts_as_handed_over(self, monkeypatch):
        async def no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        early = self._Stub([True] * 3 + [False])
        asked = self._Stub([])
        self._wait([], ask=True, settle=12, stub=early)
        self._wait([], ask=True, settle=0, stub=asked)
        assert early._handed_over is False and asked._handed_over is True

    @staticmethod
    def _checkout(reasons_by_title, monkeypatch, ask_setting=False, clears=True, ends=None, handed=True, answer=(-1, 0)):
        from src.stores import epic as epic_module
        from src.stores.epic import EpicGamesClaimer

        monkeypatch.setattr(epic_module.cfg, "eg_captcha_ask", ask_setting)
        waited, current = [], {}

        async def reason():
            return current["reason"]

        async def present():
            return True

        async def wait_out(*args, **kwargs):
            waited.append((current["title"], kwargs["settle"], kwargs["ask"]))
            # The real wait sets it only when the check outlasted its settle time and went to you over VNC.
            stub._handed_over = handed and kwargs["ask"]
            return clears

        async def answer_lengths():
            read.append(current["title"])
            return list(answer)

        read = []
        stub = type("Stub", (), {"_meeting": False, "_with_you": False, "_claiming_url": "https://store.epicgames.com/p/galaxy-kart", "_seen_at_prompt": None, "_book": EpicGamesClaimer._book, "store_name": "epic", "_checkout_challenge_reason": staticmethod(reason),
                                 "_checkout_challenge_present": staticmethod(present),
                                 "_wait_out_challenge": staticmethod(wait_out),
                                 "_captcha_answer_lengths": staticmethod(answer_lengths)})()
        stub._checkout_asked, stub._checkout_puzzles, stub._checkout_stuck, stub._ends_now = set(), set(), set(), ends
        stub._checkout_handed = {}
        stub.answers_read = read
        for title, text in reasons_by_title:
            current.update(title=title, reason=text)
            assert asyncio.run(EpicGamesClaimer._clear_checkout_challenge(stub, title)) is clears
        return waited, stub

    PUZZLE = "checkout frame, frame: https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html"
    CLOUDFLARE = "page title: just a moment..."

    def test_a_checkout_puzzle_waits_for_the_next_run_without_asking_you(self, monkeypatch):
        # Owner, 2026-10-30: a puzzle is not his to solve by default; the next run usually passes without one.
        waited, stub = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch, clears=False)
        assert waited == [("Galaxy Kart", 12, False)] and stub._checkout_puzzles == {"Galaxy Kart"}

    def test_a_check_that_stayed_is_not_waited_for_again_in_the_same_claim(self, monkeypatch):
        # After the checkout gave up, the claim looked once more: a second 12 s or 180 s and a second count.
        waited, stub = self._checkout([("Galaxy Kart", self.PUZZLE), ("Galaxy Kart", self.PUZZLE)], monkeypatch, clears=False)
        assert waited == [("Galaxy Kart", 12, False)] and stub._checkout_stuck == {"Galaxy Kart"}

    def test_a_puzzle_that_went_by_itself_does_not_stop_the_run(self, monkeypatch):
        waited, stub = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch)
        assert waited == [("Galaxy Kart", 12, False)] and not stub._checkout_puzzles and not stub._checkout_stuck

    def test_with_eg_captcha_ask_a_puzzle_is_yours_once_per_game(self, monkeypatch):
        # 2026-10-29: a puzzle waited out for three minutes only expired, twice, and nobody was asked.
        waited, _ = self._checkout([("Galaxy Kart", self.PUZZLE), ("Galaxy Kart", self.PUZZLE), ("Sker Ritual", self.PUZZLE)],
                                   monkeypatch, ask_setting=True)
        assert waited == [("Galaxy Kart", 12, True), ("Galaxy Kart", 12, False), ("Sker Ritual", 12, True)]

    @pytest.mark.parametrize("lengths,note", [
        ([-1, 2187], "answer field filled (2187 characters)"), ([0, -1], "answer field empty"),
        ([-1, -1], "no answer field"), ([None], "answer field unreadable"), ([], "answer field unreadable")])
    def test_the_answer_field_is_told_by_its_length_only(self, lengths, note):
        from src.core.claimer import captcha_answer_note
        assert captcha_answer_note(lengths) == note

    def test_the_answer_field_is_read_only_after_a_puzzle_handed_to_you_went(self, monkeypatch):
        # Solved and expired both vanish; the field is logged so live runs can tell them apart later.
        _, asked = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch, ask_setting=True)
        _, waited_only = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch)
        _, stayed = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch, ask_setting=True, clears=False)
        assert asked.answers_read == ["Galaxy Kart"] and waited_only.answers_read == [] and stayed.answers_read == []
        # Gone is not solved: the game is not marked as stopped here, the order that follows decides.
        assert not asked._checkout_puzzles and asked._checkout_handed == {"Galaxy Kart": 1}

    def test_a_puzzle_gone_before_it_went_to_you_is_not_yours(self, monkeypatch):
        # It cleared within the first 12 s, no VNC notice was sent, so a later failure is not a captcha you missed.
        _, stub = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch, ask_setting=True, handed=False)
        assert stub.answers_read == [] and stub._checkout_handed == {}

    def test_a_filled_answer_field_is_a_solve(self, monkeypatch):
        _, stub = self._checkout([("Galaxy Kart", self.PUZZLE)], monkeypatch, ask_setting=True, answer=(-1, 2187))
        assert stub._checkout_handed == {}

    def test_on_its_last_run_a_game_is_asked_twice_never_a_third_time(self, monkeypatch):
        # Each puzzle expires after about two minutes and a new one can follow; every one used to ping you again.
        from datetime import datetime, timedelta, timezone

        from src.core import run_state
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        monkeypatch.setattr(run_state, "run_started", lambda: None)
        ends = datetime.now(timezone.utc) + timedelta(hours=2)
        waited, stub = self._checkout([("Galaxy Kart", self.PUZZLE)] * 3, monkeypatch, ends=ends)
        assert [ask for _, _, ask in waited] == [True, True, False] and stub._checkout_handed == {"Galaxy Kart": 2}

    def test_after_a_puzzle_any_later_check_of_that_game_is_only_waited_for(self, monkeypatch):
        # #95 asked for one ping per claim, so a Cloudflare check after the puzzle does not ping again.
        waited, _ = self._checkout([("Galaxy Kart", self.PUZZLE), ("Galaxy Kart", self.CLOUDFLARE)], monkeypatch, ask_setting=True)
        assert waited == [("Galaxy Kart", 12, True), ("Galaxy Kart", 180, False)]

    @pytest.mark.parametrize("ask_setting", [False, True])
    def test_a_check_that_passes_by_itself_keeps_its_three_minutes(self, monkeypatch, ask_setting):
        # #95: Cloudflare's check in the checkout clears by itself after about two minutes.
        waited, stub = self._checkout([("Sker Ritual", self.CLOUDFLARE)], monkeypatch, ask_setting)
        assert waited == [("Sker Ritual", 180, True)] and not stub._checkout_puzzles

    def _nobody_answered(self, stub, settle, monkeypatch):
        from src.core import run_state
        sleeps = []

        async def count_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", count_sleep)
        run_state.reset_run_state()
        run_state.mark_unanswered("epic")
        try:
            return self._wait([], ask=True, settle=settle, stub=stub), sleeps
        finally:
            run_state.reset_run_state()

    def test_a_captcha_nobody_answered_is_not_waited_for_again(self, monkeypatch):
        stub = self._Stub([])
        result, sleeps = self._nobody_answered(stub, 180, monkeypatch)
        assert result == (False, False) and sleeps == [] and stub.shots == [("challenge_epic", True)]

    def test_a_check_that_passes_by_itself_is_still_waited_out_after_nobody_answered(self, monkeypatch):
        class Cloudflare(self._Stub):
            async def _human_challenge_reason(self):
                return "page title: just a moment..."

        stub = Cloudflare([True] * 5 + [False])
        result, sleeps = self._nobody_answered(stub, 180, monkeypatch)
        assert result == (True, False) and len(sleeps) == 5

    @pytest.mark.parametrize("reason,needs_you", [
        ("frame: https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html", True),
        ("checkout frame, frame: https://www.google.com/recaptcha/api2/bframe", True),
        ("widget: FunCaptcha", True), ("page text: geo.captcha-delivery.com", True), ("widget: talon_frame_login_prod", True),
        ("page title: just a moment...", False), ("Cloudflare challenge form", False),
        ("frame: https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile", False),
        ("widget: cf-turnstile", False), ("page text: verify you are human", False), ("", False),
    ])
    def test_which_checks_need_a_person(self, reason, needs_you):
        from src.core.claimer import challenge_needs_you
        assert challenge_needs_you(reason) is needs_you

    def test_even_a_wait_that_never_asks_keeps_a_screenshot(self):
        stub = self._Stub([])
        self._wait([], ask=False, stub=stub)
        assert stub.shots == [("challenge_epic", True)]

    def test_epic_checkout_waits_out_a_slow_check_without_asking(self, monkeypatch):
        # #95: Epic's checkout check passed by itself after about two minutes, but the bot asked you after 12 seconds.
        async def no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        # 180 s is what the checkout gives a Cloudflare check (test_a_check_that_passes_by_itself_keeps_its_three_minutes).
        assert self._wait([True] * 65 + [False], ask=True, settle=180) == (True, False)

    def test_without_asking_a_check_that_stays_is_given_up(self):
        assert self._wait([], ask=False) == (False, False)

    def test_asking_is_still_the_default(self):
        assert self._wait([], ask=True) == (True, True)

    def test_a_check_that_passes_by_itself_counts_either_way(self):
        assert self._wait([False], ask=False, settle=2) == (True, False)

    def test_itch_io_waits_out_a_slow_cloudflare_check_without_asking(self, monkeypatch):
        # #59: itch.io's check passed by itself after up to 2.5 minutes, but the bot asked you after 12 seconds.
        import re

        async def no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        source = (ROOT / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
        settle = int(re.search(r'_wait_out_challenge\("Itch\.io", store_key="itchio", settle=(\d+)\)', source).group(1))
        assert self._wait([True] * 75 + [False], ask=True, settle=settle) == (True, False)

    def test_a_store_nobody_answered_is_not_asked_again_nor_said_to_be(self, caplog):
        # IndieGala, 20.10: the log said "requesting manual solve via VNC" again after nobody answered the first time.
        from src.core import run_state
        run_state.reset_run_state()
        run_state.mark_unanswered("epic")
        stub = self._Stub([])
        try:
            with caplog.at_level(logging.WARNING, logger="test"):
                assert self._wait([], ask=True, stub=stub) == (False, False)
            assert not any("requesting manual solve" in r.getMessage() for r in caplog.records)
            assert stub.shots == [("challenge_epic", True)]
        finally:
            run_state.reset_run_state()
