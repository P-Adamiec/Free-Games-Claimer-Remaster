"""First tab handling at browser start (issue #38).

nodriver reads Chrome's target list once and then assumes a page is there, so a
slow first tab used to end the whole store run with "coroutine raised StopIteration".
"""

import asyncio
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.core import claimer as cm
from src.core.claimer import open_first_tab


class FakeBrowser:
    """Stands in for nodriver's Browser: the page target may show up late, or never."""

    def __init__(self, tabs_after: int = 0, raise_times: int = 0, no_window: bool = False):
        self.tabs_after = tabs_after      # how many update_targets() calls before a page appears
        self.raise_times = raise_times    # how often get() blows up the way nodriver does
        self.no_window = no_window        # Chrome has no window, so a new tab has nowhere to go
        self.updates = 0
        self.calls: list[tuple[str, str]] = []

    @property
    def tabs(self):
        return ["page"] if self.updates >= self.tabs_after else []

    async def update_targets(self):
        self.updates += 1

    async def get(self, url, new_tab: bool = False, new_window: bool = False):
        mode = "window" if new_window else "tab" if new_tab else "existing"
        self.calls.append((url, mode))
        if mode == "tab" and self.no_window:
            raise Exception("Failed to open new tab - no browser is open")
        if mode == "existing" and self.raise_times:
            self.raise_times -= 1
            raise RuntimeError("coroutine raised StopIteration")
        return f"tab:{url}"


def _open(browser, **kwargs):
    return asyncio.run(open_first_tab(browser, delay=0, **kwargs))


class TestOpenFirstTab:
    def test_a_ready_browser_is_used_straight_away(self):
        b = FakeBrowser()
        assert _open(b) == "tab:about:blank"
        assert b.calls == [("about:blank", "existing")]
        assert b.updates == 0

    def test_a_late_tab_is_waited_for(self):
        b = FakeBrowser(tabs_after=3)
        assert _open(b) == "tab:about:blank"
        assert b.updates == 3
        assert b.calls == [("about:blank", "existing")]

    def test_a_tab_that_never_arrives_is_asked_for(self):
        # Chrome restored a session without a single page target, so create one outright.
        b = FakeBrowser(tabs_after=99)
        assert _open(b, attempts=3) == "tab:about:blank"
        assert b.calls == [("about:blank", "tab")]
        assert b.updates == 3

    def test_the_reported_crash_is_retried_not_raised(self):
        b = FakeBrowser(raise_times=1)
        assert _open(b) == "tab:about:blank"
        assert b.calls == [("about:blank", "existing"), ("about:blank", "existing")]

    def test_a_windowless_chrome_gets_a_window(self):
        # Observed live: Target.createTarget answers "no browser is open" with no window around.
        b = FakeBrowser(tabs_after=99, no_window=True)
        assert _open(b, attempts=1) == "tab:about:blank"
        assert b.calls == [("about:blank", "tab"), ("about:blank", "window")]

    def test_a_browser_that_never_answers_reaches_the_caller(self):
        # Nothing left to try, so start_browser()'s retry loop sweeps Chrome and starts over.
        b = FakeBrowser(tabs_after=99, raise_times=99)
        b.get = _always_fail
        with pytest.raises(RuntimeError):
            _open(b, attempts=1)


async def _always_fail(*args, **kwargs):
    raise RuntimeError("browser is gone")


class TestOneBrowserPerProfile:
    """A browser left over from an earlier run must go before the new one opens (issue #38)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
    # Everything between the seeded preferences and the first launch attempt.
    BEFORE_LAUNCH = SOURCE.split("Failed to seed Chrome preferences", 1)[1].split("for attempt in range(1, 4):", 1)[0]

    def test_a_leftover_browser_is_closed_before_the_new_one_starts(self):
        assert "_sweep_orphan_chrome" in self.BEFORE_LAUNCH

    def test_processes_go_before_lock_files(self):
        # Seen live: deleting the lock first let a second Chrome open on a profile the old one still held.
        assert self.BEFORE_LAUNCH.index("_sweep_orphan_chrome") < self.BEFORE_LAUNCH.index("_clear_profile_locks")

    def test_the_first_failure_leaves_evidence(self):
        # A run that recovers on the third attempt used to print nothing about the first two.
        assert "State at the first failure" in self.SOURCE


class TestWaitForChrome:
    """The wait the slow-start rescue gives a Chrome nodriver gave up on (PR #99)."""

    @pytest.fixture
    def endpoint(self, monkeypatch):
        clock = SimpleNamespace(now=0.0, calls=[], options=None, ready_at=5.0)

        async def sleep(seconds):
            clock.now += seconds

        async def ready(client, url, **kwargs):
            clock.calls.append((url, kwargs["timeout"]))
            if clock.now < clock.ready_at:
                raise httpx.ConnectError("not listening yet")
            return httpx.Response(200, json={"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"},
                                  request=httpx.Request("GET", url))

        class Client:
            def __init__(self, **kwargs):
                clock.options = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            get = ready

        monkeypatch.setattr(cm, "time", SimpleNamespace(monotonic=lambda: clock.now))
        monkeypatch.setattr(cm, "asyncio", SimpleNamespace(sleep=sleep))
        monkeypatch.setattr(cm.httpx, "AsyncClient", Client)
        return clock, Client

    def test_a_slow_chrome_is_waited_for_on_its_own_port(self, endpoint):
        clock, _ = endpoint
        waited = asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), "127.0.0.1", 9222, 60))
        assert waited == 5 and len(clock.calls) == 11
        assert clock.options == {"trust_env": False}
        assert {url for url, _ in clock.calls} == {"http://127.0.0.1:9222/json/version"}

    def test_a_chrome_that_never_answers_has_a_deadline(self, endpoint):
        clock, _ = endpoint
        clock.ready_at = 999
        with pytest.raises(TimeoutError):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), "127.0.0.1", 9222, 2))
        assert clock.now == 2

    def test_a_chrome_that_died_is_not_waited_for(self, endpoint):
        clock, _ = endpoint
        with pytest.raises(RuntimeError, match="exit 12"):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=12), "127.0.0.1", 9222, 60))
        assert clock.now == 0 and not clock.calls

    @pytest.mark.parametrize("body,status", [("[]", 200), ("{}", 200), ("not json", 200), ("{}", 503)])
    def test_only_a_ready_control_endpoint_counts(self, endpoint, monkeypatch, body, status):
        _, client = endpoint

        async def unusable(self, url, **kwargs):
            return httpx.Response(status, content=body, request=httpx.Request("GET", url))
        monkeypatch.setattr(client, "get", unusable)
        with pytest.raises(TimeoutError):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), "127.0.0.1", 9222, 1))


class _Abandoned(SimpleNamespace):
    """A browser nodriver gave up on; hashable like the real one, since nodriver keeps them in a set."""

    __hash__ = object.__hash__


class TestSlowStartRescue:
    """Only the start nodriver gives up on is rescued; every normal start goes through nodriver untouched."""

    @pytest.fixture
    def launch(self, monkeypatch, tmp_path):
        state = SimpleNamespace(starts=[], waits=[], registry=set(), claimer=cm.BaseClaimer(), profile=tmp_path / "epic",
                                attached=SimpleNamespace(), wait_result=9.0,
                                first_error=Exception("Failed to connect to browser"))
        state.slow = _Abandoned(info=None, _process=SimpleNamespace(returncode=None, pid=123), _process_pid=123,
                                config=SimpleNamespace(user_data_dir=str(state.profile), host="127.0.0.1", port=9222))

        async def start(config=None, **kwargs):
            state.starts.append(config or kwargs)
            if config is None and state.first_error:
                state.registry.add(state.slow)
                raise state.first_error
            return state.attached

        async def wait(process, host, port, timeout):
            state.waits.append((process, host, port, timeout))
            if isinstance(state.wait_result, BaseException):
                raise state.wait_result
            return state.wait_result

        monkeypatch.setattr(cm, "chrome_timezone", nullcontext)
        monkeypatch.setattr(cm.uc, "start", start)
        monkeypatch.setattr(cm.uc.util, "get_registered_instances", lambda: state.registry)
        monkeypatch.setattr(cm, "wait_for_chrome", wait)
        return state

    @staticmethod
    def _run(s):
        return asyncio.run(s.claimer._launch_chrome(s.profile, headless=False, sandbox=False,
                                                    browser_executable_path="/chrome", browser_args=["--lang=en-US"]))

    def test_a_normal_start_is_nodrivers_own_call(self, launch):
        launch.first_error = None
        assert self._run(launch) is launch.attached
        assert launch.starts == [{"user_data_dir": str(launch.profile), "headless": False, "sandbox": False,
                                  "browser_executable_path": "/chrome", "browser_args": ["--lang=en-US"]}]
        assert not launch.waits

    def test_a_slow_start_is_attached_to_the_same_chrome(self, launch):
        browser = self._run(launch)
        assert browser is launch.attached and launch.starts[1] is launch.slow.config
        assert launch.waits == [(launch.slow._process, "127.0.0.1", 9222, cm.CHROME_START_WAIT)]
        assert browser._process is launch.slow._process and browser._process_pid == 123
        assert launch.slow not in launch.registry

    @pytest.mark.parametrize("change", ["dead", "answered", "other profile", "unregistered"])
    def test_anything_else_fails_the_way_it_always_did(self, launch, monkeypatch, change):
        if change == "dead":
            launch.slow._process.returncode = 0
        elif change == "answered":
            launch.slow.info = {"Browser": "Chrome"}
        elif change == "other profile":
            launch.slow.config.user_data_dir = "/elsewhere"
        else:
            monkeypatch.setattr(cm.uc.util, "get_registered_instances", lambda: set())
        with pytest.raises(Exception, match="Failed to connect"):
            self._run(launch)
        assert not launch.waits and len(launch.starts) == 1

    def test_a_cancelled_wait_ends_the_chrome_it_was_waiting_for(self, launch, monkeypatch):
        # Nothing owns that Chrome once the wait is cancelled: not self.browser, not nodriver's registry.
        killed = []
        launch.wait_result = asyncio.CancelledError()
        monkeypatch.setattr(launch.claimer, "_kill_process_tree", killed.append)
        with pytest.raises(asyncio.CancelledError):
            self._run(launch)
        assert killed == [123] and launch.slow not in launch.registry

    @pytest.mark.parametrize("error", [TimeoutError("quiet"), RuntimeError("exit 1")])
    def test_a_rescue_that_fails_hands_back_the_first_error(self, launch, error):
        launch.wait_result = error
        with pytest.raises(Exception, match="Failed to connect") as caught:
            self._run(launch)
        assert caught.value.__cause__ is error and launch.slow not in launch.registry


class TestNodriverStillWorksThisWay:
    """What the slow-start rescue reads from nodriver, checked on the installed version so an update that changes it fails here."""

    @pytest.fixture
    def quick(self, monkeypatch):
        from nodriver.core import browser as nodriver_browser
        real_sleep = asyncio.sleep

        async def no_wait(seconds, *args, **kwargs):
            await real_sleep(0)

        async def refused(self, endpoint):
            # Windows takes about two seconds to refuse a closed port; the answer itself is not what is checked.
            raise ConnectionRefusedError(endpoint)
        monkeypatch.setattr(nodriver_browser.asyncio, "sleep", no_wait)
        monkeypatch.setattr(nodriver_browser.HTTPApi, "get", refused)

    def test_a_start_it_gives_up_on_stays_registered_with_its_process(self, quick, tmp_path):
        async def run():
            # Python is no Chrome: it exits on Chrome's flags, so nodriver never gets an answer.
            with pytest.raises(Exception, match="Failed to connect"):
                await cm.uc.start(user_data_dir=str(tmp_path), headless=True, sandbox=False,
                                  browser_executable_path=sys.executable, browser_args=[])
            found = [b for b in cm.uc.util.get_registered_instances() if str(b.config.user_data_dir) == str(tmp_path)]
            try:
                assert len(found) == 1
                slow = found[0]
                assert slow.info is None and slow._process is not None and slow._process_pid == slow._process.pid
                assert slow.config.host == "127.0.0.1" and isinstance(slow.config.port, int)
                await slow._process.wait()
            finally:
                for b in found:
                    cm.uc.util.get_registered_instances().discard(b)
        asyncio.run(run())

    def test_a_start_with_host_and_port_attaches_instead_of_starting_chrome(self, quick, tmp_path):
        async def run():
            config = cm.uc.Config(user_data_dir=str(tmp_path), browser_executable_path=str(tmp_path / "no-chrome"),
                                  host="127.0.0.1", port=cm.uc.util.free_port())
            # A missing Chrome file would fail first if nodriver still tried to start one.
            with pytest.raises(Exception, match="Failed to connect"):
                await cm.uc.start(config=config)
            found = [b for b in cm.uc.util.get_registered_instances() if b.config is config]
            for b in found:
                cm.uc.util.get_registered_instances().discard(b)
            assert len(found) == 1 and found[0]._process is None
        asyncio.run(run())
