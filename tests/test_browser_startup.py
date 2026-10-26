"""First tab handling at browser start (issue #38).

nodriver reads Chrome's target list once and then assumes a page is there, so a
slow first tab used to end the whole store run with "coroutine raised StopIteration".
"""

import asyncio
from contextlib import contextmanager
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


class TestChromeReadiness:
    """A busy disk must not turn one slow Chrome into three killed launches."""

    @pytest.fixture
    def endpoint(self, monkeypatch):
        clock = SimpleNamespace(now=0.0, calls=[], options=None)

        async def sleep(seconds):
            clock.now += seconds

        async def ready(client, url, **kwargs):
            clock.calls.append((url, kwargs["timeout"]))
            if clock.now < 5:
                raise httpx.ConnectError("not listening yet")
            return httpx.Response(200, json={"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test"},
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

    def test_a_five_second_start_finishes_with_the_same_process(self, endpoint):
        clock, _ = endpoint
        process = SimpleNamespace(pid=123, returncode=None)
        asyncio.run(cm.wait_for_chrome(process, 9222, 30))
        assert clock.now == 5
        assert process.returncode is None
        assert len(clock.calls) == 11
        assert clock.options == {"trust_env": False}
        assert all(url == "http://127.0.0.1:9222/json/version" for url, _ in clock.calls)

    def test_a_ready_chrome_does_not_wait_for_the_whole_timeout(self, endpoint):
        clock, _ = endpoint
        clock.now = 5
        asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), 9222, 30))
        assert clock.now == 5 and len(clock.calls) == 1

    def test_a_process_that_never_listens_has_a_bounded_deadline(self, endpoint):
        clock, _ = endpoint
        with pytest.raises(TimeoutError, match="CHROME_STARTUP_TIMEOUT=2s"):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), 9222, 2))
        assert clock.now == 2
        assert clock.calls[-1][1] == 0.5

    def test_a_dead_chrome_fails_without_waiting_or_connecting(self, endpoint):
        clock, _ = endpoint
        with pytest.raises(RuntimeError, match="exit=12"):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=12), 9222, 30))
        assert clock.now == 0 and not clock.calls

    @pytest.mark.parametrize("body,status", [("[]", 200), ("{}", 200), ("not json", 200), ("{}", 503)])
    def test_only_a_ready_debugging_endpoint_counts(self, endpoint, monkeypatch, body, status):
        clock, client = endpoint

        async def unusable(self, url, **kwargs):
            return httpx.Response(status, content=body, request=httpx.Request("GET", url))
        monkeypatch.setattr(client, "get", unusable)
        with pytest.raises(TimeoutError):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), 9222, 1))
        assert clock.now == 1

    def test_cancellation_reaches_the_caller(self, endpoint, monkeypatch):
        _, client = endpoint

        async def cancelled(self, url, **kwargs):
            raise asyncio.CancelledError
        monkeypatch.setattr(client, "get", cancelled)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(cm.wait_for_chrome(SimpleNamespace(returncode=None), 9222, 30))


class TestChromeProcessOwnership:
    """Attach once, retain the real PID for shutdown, clean up every failed start."""

    @pytest.fixture
    def launch(self, monkeypatch, tmp_path):
        events = []
        process = SimpleNamespace(pid=123, returncode=None)
        browser = SimpleNamespace()
        state = SimpleNamespace(events=events, process=process, browser=browser,
                                claimer=cm.BaseClaimer(), profile=tmp_path / "epic", config=None)

        async def wait():
            events.append("reaped")
            return process.returncode
        process.wait = wait

        @contextmanager
        def timezone():
            events.append("timezone_enter")
            try:
                yield
            finally:
                events.append("timezone_exit")

        async def spawn(executable, *args, **kwargs):
            events.append("spawn")
            state.executable, state.args, state.streams = executable, args, kwargs
            return process

        async def ready(found, port, timeout):
            events.append("ready")
            assert found is process and timeout == 30 and port == 9222

        async def attach(*, config):
            events.append("attach")
            state.config = config
            return browser

        monkeypatch.setattr(cm, "chrome_timezone", timezone)
        monkeypatch.setattr(cm.cfg, "chrome_startup_timeout", 30)
        monkeypatch.setattr(cm.uc.util, "free_port", lambda: 9222)
        monkeypatch.setattr(cm.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(cm, "wait_for_chrome", ready)
        monkeypatch.setattr(cm.uc, "start", attach)
        monkeypatch.setattr(state.claimer, "_kill_process_tree", lambda pid: events.append(("kill", pid)))
        return state

    def test_the_same_profile_and_arguments_are_used_and_the_pid_is_kept(self, launch):
        s = launch
        flags = ["--restore-last-session", "--ignore-gpu-blocklist", "--enable-unsafe-webgpu"]
        asyncio.run(s.claimer._launch_chrome(s.profile, "/chrome", False, flags))
        assert s.events == ["timezone_enter", "spawn", "timezone_exit", "ready", "attach"]
        assert s.executable == "/chrome" and all(flag in s.args for flag in flags)
        assert f"--user-data-dir={s.profile}" in s.args and "--no-sandbox" in s.args
        assert "--headless=new" not in s.args and "--disable-gpu" not in s.args
        assert "--remote-debugging-port=9222" in s.args
        assert s.config.host == "127.0.0.1" and s.config.port == 9222
        assert s.browser._process is s.process and s.browser._process_pid == 123
        assert all(value == asyncio.subprocess.DEVNULL for value in s.streams.values())

    def test_normal_close_still_owns_the_attached_process(self, launch):
        s = launch
        s.browser.stop = lambda: s.events.append("stop")

        async def run():
            await s.claimer._launch_chrome(s.profile, "/chrome", True, ["--disable-gpu"])
            assert "--headless=new" in s.args and "--disable-gpu" in s.args
            await s.claimer.close_browser()
        asyncio.run(run())
        assert s.events[-2:] == ["stop", ("kill", 123)]
        assert s.claimer.browser is None

    @pytest.mark.parametrize("error", [TimeoutError("slow"), RuntimeError("exited"), asyncio.CancelledError()])
    def test_failed_or_cancelled_readiness_kills_and_reaps_the_process(self, launch, monkeypatch, error):
        s = launch

        async def fail(*args):
            raise error
        monkeypatch.setattr(cm, "wait_for_chrome", fail)
        with pytest.raises(type(error)):
            asyncio.run(s.claimer._launch_chrome(s.profile, "/chrome", False, []))
        assert s.events[-2:] == [("kill", 123), "reaped"]
        assert "attach" not in s.events and s.claimer.browser is None

    def test_failed_attach_also_cleans_up_the_chrome_it_started(self, launch, monkeypatch):
        s = launch

        async def fail(**kwargs):
            raise RuntimeError("attach failed")
        monkeypatch.setattr(cm.uc, "start", fail)
        with pytest.raises(RuntimeError, match="attach failed"):
            asyncio.run(s.claimer._launch_chrome(s.profile, "/chrome", False, []))
        assert s.events[-2:] == [("kill", 123), "reaped"]


class TestDefaultChromeLaunch:
    @pytest.mark.parametrize("headless", [False, True])
    def test_no_override_keeps_nodrivers_original_launch(self, monkeypatch, tmp_path, headless):
        claimer = cm.BaseClaimer()
        browser = SimpleNamespace()
        calls, events = [], []
        flags = ["--restore-last-session", "--disable-gpu"]

        @contextmanager
        def timezone():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        async def start(*, config):
            events.append("start")
            calls.append(config)
            return browser

        def unexpected(*args, **kwargs):
            pytest.fail("Default startup must not use the opt-in subprocess/readiness path")

        monkeypatch.setattr(cm.cfg, "chrome_startup_timeout", 0)
        monkeypatch.setattr(cm, "chrome_timezone", timezone)
        monkeypatch.setattr(cm.uc, "start", start)
        monkeypatch.setattr(cm, "wait_for_chrome", unexpected)
        monkeypatch.setattr(cm.asyncio, "create_subprocess_exec", unexpected)
        asyncio.run(claimer._launch_chrome(tmp_path, "/chrome", headless, flags))
        assert claimer.browser is browser
        assert events == ["enter", "start", "exit"]
        # The same Config uc.start() builds from these keywords: no host or port, so nodriver launches Chrome itself.
        [config] = calls
        args = config()
        assert config.host is None and config.port is None and config.browser_executable_path == "/chrome"
        assert all(flag in args for flag in flags) and f"--user-data-dir={tmp_path}" in args
        assert "--no-sandbox" in args and ("--headless=new" in args) == headless
        assert not any(arg.startswith("--remote-debugging") for arg in args)

    def test_each_start_logs_how_long_chrome_took(self, monkeypatch, tmp_path, caplog):
        async def start(*, config):
            return SimpleNamespace()

        monkeypatch.setattr(cm.cfg, "chrome_startup_timeout", 0)
        monkeypatch.setattr(cm.uc, "start", start)
        with caplog.at_level("DEBUG"):
            asyncio.run(cm.BaseClaimer()._launch_chrome(tmp_path, "/chrome", True, []))
        assert any(r.getMessage().startswith("Chrome answered after ") for r in caplog.records)


class TestLaunchAttempts:
    @pytest.mark.parametrize("failures", [1, 3])
    def test_the_existing_retry_and_profile_cleanup_are_kept(self, monkeypatch, tmp_path, failures):
        claimer = cm.BaseClaimer()
        calls, cleaned, sleeps, diagnosed = [], [], [], []
        page = SimpleNamespace()
        browser = SimpleNamespace()

        async def launch(*args):
            calls.append(args)
            if len(calls) <= failures:
                raise TimeoutError("not ready")
            claimer.browser = browser

        async def close():
            cleaned.append("close")

        async def sleep(seconds):
            sleeps.append(seconds)

        async def first_tab(found):
            assert found is browser
            return page

        async def send(command):
            pass
        page.send = send
        monkeypatch.setattr(cm.cfg, "browser_dir", tmp_path)
        monkeypatch.setattr(cm, "chrome_binary", lambda: "/chrome")
        monkeypatch.setattr(cm, "screen_is_managed", lambda: False)
        monkeypatch.setattr(cm, "screen_state", lambda: "test screen")
        monkeypatch.setattr(cm, "running_as", lambda: "test user")
        monkeypatch.setattr(cm, "free_memory", lambda: "test memory")
        monkeypatch.setattr(cm, "open_first_tab", first_tab)
        monkeypatch.setattr(cm.asyncio, "sleep", sleep)
        monkeypatch.setattr(claimer, "_launch_chrome", launch)
        monkeypatch.setattr(claimer, "close_browser", close)
        monkeypatch.setattr(claimer, "_sweep_orphan_chrome", lambda path: cleaned.append("sweep"))
        monkeypatch.setattr(claimer, "_clear_profile_locks", lambda path: cleaned.append("locks"))
        monkeypatch.setattr(claimer, "_log_launch_diagnostics", lambda *args: diagnosed.append(args))
        if failures == 3:
            with pytest.raises(RuntimeError, match="after 3 attempts"):
                asyncio.run(claimer.start_browser())
            assert len(calls) == 3 and sleeps == [2, 4] and len(diagnosed) == 1
        else:
            assert asyncio.run(claimer.start_browser()) is browser
            assert len(calls) == 2 and sleeps == [2] and not diagnosed
        assert cleaned == ["sweep", "locks"] + ["close", "sweep", "locks"] * failures
