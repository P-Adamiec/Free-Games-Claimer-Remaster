"""First tab handling at browser start (issue #38).

nodriver reads Chrome's target list once and then assumes a page is there, so a
slow first tab used to end the whole store run with "coroutine raised StopIteration".
"""

import asyncio

import pytest

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
