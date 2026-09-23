"""How a notification reaches each service.

The message is written in markdown. Telegram wanted HTML and got the raw `**`
characters instead (issue #50), and ntfy needs the format named in its own address
before it will render anything. Both are checked here against the real Apprise, not
a stand-in, because the bug was in what Apprise does with what we hand it.
"""

from pathlib import Path

import apprise
import pytest
from apprise.conversion import convert_between

from src.core.notifier import format_game_list, markdown_ready, markdown_safe

ROOT = Path(__file__).resolve().parent.parent

# The shape every summary has: a bold store, a bullet, a bold link, a status.
SUMMARY = ("**Epic Games** (Not Weak.):\n"
           "• **[Astral Ascent](https://store.epicgames.com/en-US/p/astral-ascent)**: claimed")


class TestNtfyIsToldToRenderMarkdown:
    """ntfy reads the format from its address, so the address is where it has to be said."""

    @pytest.mark.parametrize("url,expected", [
        ("ntfy://ntfy.sh/topic", "ntfy://ntfy.sh/topic?format=markdown"),
        ("ntfys://user:pass@host/topic", "ntfys://user:pass@host/topic?format=markdown"),
        ("ntfy://ntfy.sh/topic?priority=high", "ntfy://ntfy.sh/topic?priority=high&format=markdown"),
    ])
    def test_an_ntfy_address_gains_the_format(self, url, expected):
        assert markdown_ready(url) == [expected]

    def test_a_format_you_chose_yourself_is_left_alone(self):
        assert markdown_ready("ntfy://ntfy.sh/topic?format=text") == ["ntfy://ntfy.sh/topic?format=text"]

    def test_other_services_are_not_touched(self):
        assert markdown_ready("tgram://123:abc/456") == ["tgram://123:abc/456"]

    def test_several_services_are_handled_one_by_one(self):
        assert markdown_ready("ntfy://ntfy.sh/a, tgram://1:a/2") == [
            "ntfy://ntfy.sh/a?format=markdown", "tgram://1:a/2"]

    def test_nothing_configured_is_not_a_crash(self):
        assert markdown_ready("") == [] and markdown_ready(None) == []

    def test_apprise_really_switches_format(self):
        # The point of the rewrite: this is what makes the plugin send X-Markdown.
        ap = apprise.Apprise()
        for url in markdown_ready("ntfy://ntfy.sh/topic"):
            ap.add(url)
        assert [s.notify_format for s in ap.servers] == [apprise.NotifyFormat.MARKDOWN]


class TestTheMessageSaysItIsMarkdown:
    """Without this, Apprise treats the body as plain text and Telegram shows the asterisks."""

    SOURCE = (ROOT / "src" / "core" / "notifier.py").read_text(encoding="utf-8")

    def test_the_send_declares_the_format(self):
        block = self.SOURCE.split("async def send_apprise", 1)[1]
        assert "body_format=apprise.NotifyFormat.MARKDOWN" in block

    def test_a_service_that_wants_html_gets_html(self):
        out = convert_between(apprise.NotifyFormat.MARKDOWN, apprise.NotifyFormat.HTML, SUMMARY)
        assert "<strong>Epic Games</strong>" in out
        assert '<a href="https://store.epicgames.com/en-US/p/astral-ascent">' in out
        assert "**" not in out

    def test_a_service_that_wants_text_loses_nothing(self):
        # ntfy and Discord read plain text; they must get exactly what they got before.
        assert convert_between(apprise.NotifyFormat.MARKDOWN, apprise.NotifyFormat.TEXT, SUMMARY) == \
            convert_between(apprise.NotifyFormat.TEXT, apprise.NotifyFormat.TEXT, SUMMARY)


def _html(title, status="claimed"):
    md = format_game_list([{"title": title, "url": "https://example.com/x", "status": status}])
    return convert_between(apprise.NotifyFormat.MARKDOWN, apprise.NotifyFormat.HTML, md)


class TestTitlesCannotBreakTheMessage:
    """Since the summary is sent as markdown, a title is no longer just text."""

    def test_angle_brackets_never_become_an_html_tag(self):
        # Telegram rejects the whole message over one tag it does not know.
        html = _html("Doom <Remastered>")
        assert "<Remastered>" not in html
        assert "Remastered" in html

    def test_underscores_at_a_word_edge_do_not_turn_into_italics(self):
        assert "<em>" not in _html("_Hidden_ Temple")

    @pytest.mark.parametrize("text", ["Super_Cool_Game", "Star*Chaser", "failed:missing_base",
                                      "[DLC] Starter Pack", "Call of Duty: Warzone", "Tom & Jerry"])
    def test_ordinary_text_is_left_exactly_as_it_was(self, text):
        assert markdown_safe(text) == text

    def test_an_ordinary_line_looks_the_same_as_before(self):
        before = "• **[Astral Ascent](https://example.com/x)**: claimed"
        assert format_game_list([{"title": "Astral Ascent", "url": "https://example.com/x",
                                   "status": "claimed"}]) == before
