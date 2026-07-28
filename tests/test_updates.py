"""Update check: version comparison, throttling and the one-notice-per-version rule.

No network here, the GitHub call itself is stubbed; only the decisions are tested.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.core import updates
from src.version import __version__

RELEASE_JSON = {
    "tag_name": "v1.6",
    "html_url": "https://github.com/P-Adamiec/Free-Games-Claimer-Remaster/releases/tag/v1.6",
    "name": "v1.6",
}


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Keep the state file out of the real data directory."""
    monkeypatch.setattr(updates.cfg, "_data_dir", tmp_path)
    return tmp_path / updates.STATE_FILE


@pytest.fixture
def sent(monkeypatch):
    """Capture notifications instead of sending them."""
    messages = []

    async def fake_notify(message, **kwargs):
        messages.append(message)

    monkeypatch.setattr("src.core.notifier.notify", fake_notify)
    monkeypatch.setattr(updates.cfg, "notify_updates", True)
    return messages


def _stub_release(monkeypatch, release):
    async def fake_fetch():
        return release

    monkeypatch.setattr(updates, "fetch_latest_release", fake_fetch)


def _release(version="1.6"):
    return {"version": version, "tag": f"v{version}", "url": RELEASE_JSON["html_url"]}


def _run(**kwargs):
    return asyncio.run(updates.notify_if_update_available(**kwargs))


class TestVersionParsing:
    @pytest.mark.parametrize("text,expected", [
        ("1.5", (1, 5)),
        ("v1.5", (1, 5)),
        ("V1.5", (1, 5)),
        ("v1.3d", (1, 3)),
        (" v2.0.1 ", (2, 0, 1)),
        ("1", (1,)),
    ])
    def test_reads_the_number(self, text, expected):
        assert updates._parse_version(text) == expected

    @pytest.mark.parametrize("text", ["", None, "latest", "vX"])
    def test_unusable_input(self, text):
        assert updates._parse_version(text) == ()

    def test_compares_numerically_not_alphabetically(self):
        assert updates._parse_version("1.10") > updates._parse_version("1.9")
        assert updates._parse_version("v2.0") > updates._parse_version("1.99")
        assert updates._parse_version("1.5") == updates._parse_version("v1.5")


class TestReleasesUrl:
    def test_built_from_the_repo_link(self):
        assert updates._releases_url() == (
            "https://api.github.com/repos/P-Adamiec/Free-Games-Claimer-Remaster/releases/latest"
        )


class TestMessage:
    def test_names_both_versions_and_links_the_release(self):
        message = updates.build_message(_release("1.6"))
        assert "v1.6" in message
        assert f"v{__version__}" in message
        assert RELEASE_JSON["html_url"] in message
        assert "docker compose pull" in message


class TestNotifyDecision:
    def test_notifies_once_for_a_newer_release(self, state_file, sent, monkeypatch):
        _stub_release(monkeypatch, _release("99.0"))
        _run(at_startup=True)
        assert len(sent) == 1 and "v99.0" in sent[0]

        # Second start, same release: log only, no repeat notification.
        _run(at_startup=True)
        assert len(sent) == 1
        assert json.loads(state_file.read_text())["notified_version"] == "99.0"

    def test_new_release_after_one_was_announced_notifies_again(self, state_file, sent, monkeypatch):
        _stub_release(monkeypatch, _release("99.0"))
        _run(at_startup=True)
        _stub_release(monkeypatch, _release("99.1"))
        _run(at_startup=True)
        assert len(sent) == 2 and "v99.1" in sent[1]

    def test_silent_when_running_the_latest(self, state_file, sent, monkeypatch):
        _stub_release(monkeypatch, _release(__version__))
        _run(at_startup=True)
        assert sent == []

    def test_silent_when_running_something_newer(self, state_file, sent, monkeypatch):
        # e.g. the :dev image, which is ahead of the last published release.
        _stub_release(monkeypatch, _release("0.1"))
        _run(at_startup=True)
        assert sent == []

    def test_disabled_by_config(self, state_file, sent, monkeypatch):
        monkeypatch.setattr(updates.cfg, "notify_updates", False)
        _stub_release(monkeypatch, _release("99.0"))
        _run(at_startup=True)
        assert sent == []
        assert not state_file.exists(), "disabled check must not even touch GitHub or the state file"

    def test_network_failure_is_harmless(self, state_file, sent, monkeypatch):
        _stub_release(monkeypatch, None)
        _run(at_startup=True)
        assert sent == []
        assert json.loads(state_file.read_text())["last_result"] == "error"


class TestThrottling:
    def test_recent_successful_check_is_skipped(self, state_file, sent, monkeypatch):
        state_file.write_text(json.dumps({
            "last_check": datetime.now(timezone.utc).isoformat(),
            "last_result": "ok",
        }))
        called = []

        async def fake_fetch():
            called.append(1)
            return _release("99.0")

        monkeypatch.setattr(updates, "fetch_latest_release", fake_fetch)
        _run()
        assert called == [] and sent == []

    def test_startup_always_checks(self, state_file, sent, monkeypatch):
        state_file.write_text(json.dumps({
            "last_check": datetime.now(timezone.utc).isoformat(),
            "last_result": "ok",
        }))
        _stub_release(monkeypatch, _release("99.0"))
        _run(at_startup=True)
        assert len(sent) == 1

    def test_check_runs_again_after_a_day(self, state_file, sent, monkeypatch):
        state_file.write_text(json.dumps({
            "last_check": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
            "last_result": "ok",
        }))
        _stub_release(monkeypatch, _release("99.0"))
        _run()
        assert len(sent) == 1

    def test_failed_check_retries_sooner(self, state_file, sent, monkeypatch):
        state_file.write_text(json.dumps({
            "last_check": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "last_result": "error",
        }))
        _stub_release(monkeypatch, _release("99.0"))
        _run()
        assert len(sent) == 1

    def test_unreadable_state_file_does_not_block_the_check(self, state_file, sent, monkeypatch):
        state_file.write_text("not json at all")
        _stub_release(monkeypatch, _release("99.0"))
        _run()
        assert len(sent) == 1
