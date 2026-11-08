"""Shared test setup.

These tests cover only pure logic (no browser, no network, no store accounts),
so they run anywhere in well under a second.
"""

import sys
from pathlib import Path

# Import the app the same way main.py does, from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _a_clean_run():
    """Each test starts a run of its own: no prompt left unanswered, no puzzle kept for a noVNC meeting."""
    from src.core import run_state
    run_state.reset_run_state()
    yield
    run_state.reset_run_state()


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """Tests read the owner's .env, so its webhook and Apprise address are blanked: nothing a test does reaches a phone."""
    # 8.11: a test reached a real VNC prompt and its "manual login needed" went to the owner's Discord.
    from src.core.config import cfg
    monkeypatch.setattr(cfg, "discord_webhook", "")
    monkeypatch.setattr(cfg, "notify_url", "")


@pytest.fixture(autouse=True)
def _human_checks_in_tmp(monkeypatch, tmp_path):
    """A test that meets a human check writes it to its own folder, never to the real data/."""
    from src.core import human_checks
    monkeypatch.setattr(human_checks, "_path", lambda: tmp_path / human_checks.STATE_FILE)

