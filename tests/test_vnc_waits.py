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
        assert "_wait_out_challenge(" in branch
        assert "_press_sign_in()" in branch

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
        body = self.SOURCE.split("async def _human_challenge_present", 1)[1].split("\n    async def ", 1)[0]
        assert "evaluate(CHALLENGE_JS)" in body

    def test_a_widget_hidden_by_style_is_not_a_challenge(self):
        assert "visibility === 'hidden'" in self.JS
        assert "parseFloat(st.opacity) < 0.1" in self.JS

    def test_a_widget_something_else_covers_is_not_a_challenge(self):
        assert "elementFromPoint" in self.JS

    def test_every_provider_goes_through_that_same_check(self):
        assert "&& seen(f)" in self.JS
        assert "widgets.some(seen)" in self.JS

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

        def __init__(self, present):
            self.present, self.asked = list(present), False

        async def _human_challenge_present(self):
            return self.present.pop(0) if self.present else True

        def _vnc_notice(self, *args):
            return "notice"

        async def _wait_for_vnc_login(self, check_fn, **kwargs):
            self.asked = True
            return True

    def _wait(self, present, ask, settle=0):
        stub = self._Stub(present)
        cleared = asyncio.run(BaseClaimer._wait_out_challenge(stub, "Epic", settle=settle, ask=ask))
        return cleared, stub.asked

    def test_without_asking_a_check_that_stays_is_given_up(self):
        assert self._wait([], ask=False) == (False, False)

    def test_asking_is_still_the_default(self):
        assert self._wait([], ask=True) == (True, True)

    def test_a_check_that_passes_by_itself_counts_either_way(self):
        assert self._wait([False], ask=False, settle=2) == (True, False)
