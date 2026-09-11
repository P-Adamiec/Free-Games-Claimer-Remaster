"""Waiting for the person at the keyboard.

Two rules, both learned the hard way. Every wait has to honour VNC_LOGIN_TIMEOUT,
because someone may be away from the machine. And while it waits, the bot may only
look at the page: reloading or clicking every few seconds wipes the code you are typing.
"""

import ast
from pathlib import Path

import pytest

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
        assert "needs_you(store)" in block

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
