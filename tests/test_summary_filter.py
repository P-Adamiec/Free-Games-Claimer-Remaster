"""The summary filter decides what reaches Discord/Apprise.

The filter is read out of main.py itself, so these tests fail if the real
expression changes, they can't drift into testing a copy.
"""

import re
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"

GAMES = [
    {"title": "Claimed", "url": "", "status": "claimed"},
    {"title": "Owned", "url": "", "status": "existed"},
    {"title": "Checked in", "url": "", "status": "already claimed today ✨"},
    {"title": "F2P", "url": "", "status": "skipped:f2p"},
    {"title": "Missing base", "url": "", "status": "failed:missing_base"},
    {"title": "Broken", "url": "", "status": "failed"},
    {"title": "Dry", "url": "", "status": "available (dry run)"},
    {"title": "Download once", "url": "", "status": "download only, nothing to claim 📥"},
    {"title": "Download again", "url": "", "status": "skipped:download-only"},
]


class _Cfg:
    def __init__(self, fails=False, owned=False):
        self.notify_claim_fails = fails
        self.notify_already_claimed = owned


@pytest.fixture(scope="module")
def summary_filter():
    src = MAIN_PY.read_text(encoding="utf-8")
    match = re.search(r"relevant_games = \[(.*?)\n\s*\]", src, re.S)
    assert match, "could not find the summary filter in main.py"
    expr = "[" + match.group(1) + "]"

    def run(cfg):
        # Pass the names as globals: before Python 3.12 a comprehension body cannot
        # see eval()'s locals, which made this test pass on 3.12 and fail on the
        # container's 3.11.
        scope = {
            "cfg": cfg,
            "keep_owned": cfg.notify_already_claimed,
            "result": {"games": GAMES},
        }
        return [g["title"] for g in eval(expr, scope)]  # noqa: S307 - the expression comes from our own source

    return run


def test_defaults_show_only_real_changes(summary_filter):
    assert summary_filter(_Cfg()) == ["Claimed", "Dry", "Download once"]


def test_claim_fails_can_be_switched_on(summary_filter):
    titles = summary_filter(_Cfg(fails=True))
    assert "Missing base" in titles and "Broken" in titles
    assert "Owned" not in titles


def test_already_claimed_can_be_switched_on(summary_filter):
    titles = summary_filter(_Cfg(owned=True))
    assert "Owned" in titles and "Checked in" in titles and "F2P" in titles
    assert "Broken" not in titles


def test_a_download_only_giveaway_is_reported_once(summary_filter):
    # Itch.io hands some giveaways out as a file with no way to claim them onto the
    # account. Worth saying once, not on every run for the rest of time.
    titles = summary_filter(_Cfg())
    assert "Download once" in titles
    assert "Download again" not in titles
    assert "Download again" in summary_filter(_Cfg(owned=True))


def test_both_switches_on_show_everything(summary_filter):
    assert set(summary_filter(_Cfg(fails=True, owned=True))) == {g["title"] for g in GAMES}


def test_dry_run_entries_are_never_filtered_out(summary_filter):
    for cfg in (_Cfg(), _Cfg(fails=True), _Cfg(owned=True), _Cfg(True, True)):
        assert "Dry" in summary_filter(cfg)


def test_entries_without_status_are_dropped(summary_filter):
    global GAMES
    original = GAMES
    try:
        GAMES = original + [{"title": "No status", "url": ""}]
        assert "No status" not in summary_filter(_Cfg(True, True))
    finally:
        GAMES = original
