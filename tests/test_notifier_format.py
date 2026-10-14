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

from src.core.claimer import mask_account
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


class TestTheMaskedAccountCannotBreakTheMessage:
    """The mask's "***" in "p***@gmail.com" opened bold on Discord and left a stray "**" on the line below (2026-10-12)."""

    LINES = "**AliExpress** ({}):\n• **[AliExpress Daily Check-in](https://example.com/x)**: claimed 40"

    @staticmethod
    def _html(md):
        return convert_between(apprise.NotifyFormat.MARKDOWN, apprise.NotifyFormat.HTML, md)

    def test_the_mask_is_kept_but_said_as_text(self):
        assert markdown_safe(mask_account("pawel@gmail.com")) == r"p\*\*\*@gmail.com"

    def test_the_summary_keeps_its_bold_parts_where_they_belong(self):
        html = self._html(self.LINES.format(markdown_safe(mask_account("pawel@gmail.com"))))
        assert "<strong>AliExpress</strong> (p***@gmail.com):" in html
        assert '<strong><a href="https://example.com/x">' in html and "<em>" not in html

    def test_main_escapes_the_account_in_the_header(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        assert "markdown_safe(mask_account(result.get('user')))" in main


class TestDiscordLinkPreviews:
    """PR #81 by @ckl: DISCORD_SUPPRESS_EMBEDS sends Discord's flag 4, so links come without preview cards."""

    def _sent(self, monkeypatch, tmp_path, suppress, screenshot):
        import asyncio
        import types

        import src.core.notifier as notifier
        sent = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, data=None, files=None):
                sent.append(json if json is not None else data)
                return types.SimpleNamespace(status_code=204, text="")

        monkeypatch.setattr(notifier.httpx, "AsyncClient", Client)
        monkeypatch.setattr(notifier.cfg, "discord_webhook", "https://discord.invalid/api/webhooks/1/x")
        monkeypatch.setattr(notifier.cfg, "discord_suppress_embeds", suppress)
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"png")
        asyncio.run(notifier.send_discord("https://example.com", screenshot_path=shot if screenshot else None))
        return sent[0]

    @pytest.mark.parametrize("screenshot", [False, True])
    def test_the_flag_goes_with_every_message(self, monkeypatch, tmp_path, screenshot):
        assert self._sent(monkeypatch, tmp_path, True, screenshot)["flags"] == 4

    @pytest.mark.parametrize("screenshot", [False, True])
    def test_false_brings_the_preview_cards_back(self, monkeypatch, tmp_path, screenshot):
        assert "flags" not in self._sent(monkeypatch, tmp_path, False, screenshot)


class TestFailureScreenshots:
    """DEBUG_SCREENSHOTS sends the screenshot a failure leaves, never one taken after a claim or a skip; off by default."""

    def _take(self, monkeypatch, tmp_path, on, failed=True):
        import asyncio
        import logging

        import src.core.claimer as claimer
        monkeypatch.setattr(claimer.cfg, "debug_screenshots", on)

        class Store:
            store_name, logger = "steam", logging.getLogger("test")

            def __init__(self):
                self.page, self.sent = self, []

            async def save_screenshot(self, path):
                Path(path).write_bytes(b"png")

            def screenshot_path(self, *parts):
                return tmp_path.joinpath(*parts)

            async def notify(self, message, **kwargs):
                self.sent.append((message, kwargs.get("screenshot_path")))

        store = Store()
        shot = asyncio.run(claimer.BaseClaimer.take_screenshot(store, "steam_fail_Game", failed=failed))
        return shot, store.sent

    def test_the_screenshot_goes_out_when_asked(self, monkeypatch, tmp_path):
        shot, sent = self._take(monkeypatch, tmp_path, True)
        assert shot.exists() and sent == [("📸 steam: `steam_fail_Game.png`", shot)]

    def test_a_screenshot_after_a_claim_stays_on_disk(self, monkeypatch, tmp_path):
        shot, sent = self._take(monkeypatch, tmp_path, True, failed=False)
        assert shot.exists() and sent == []

    @pytest.mark.parametrize("store,name", [
        ("steam.py", 'f"steam_paid_skip_{filenamify(page_title)}"'), ("steam.py", 'f"steam_{filenamify(page_title)}"'),
        ("prime.py", 'f"prime_{filenamify(title)}"'), ("gamerpower.py", 'f"fanatical_{filenamify(title)}"'),
        ("gamerpower.py", 'f"itchio_{filenamify(title)}"'), ("gamerpower.py", 'f"indiegala_{filenamify(title)}"'),
    ])
    def test_claims_and_skips_are_not_failures(self, store, name):
        source = (ROOT / "src" / "stores" / store).read_text(encoding="utf-8")
        assert f"take_screenshot({name})" not in source and f"take_screenshot({name}, failed=False)" in source

    def test_nothing_is_sent_by_default(self, monkeypatch, tmp_path):
        shot, sent = self._take(monkeypatch, tmp_path, False)
        assert shot.exists() and sent == []

    def test_apprise_gets_it_as_an_attachment(self, monkeypatch, tmp_path):
        import asyncio

        import src.core.notifier as notifier
        calls = []

        class Apprise:
            def add(self, url):
                pass

            def notify(self, **kwargs):
                calls.append(kwargs.get("attach"))

        monkeypatch.setattr(notifier.apprise, "Apprise", Apprise)
        monkeypatch.setattr(notifier.cfg, "notify_url", "json://localhost")
        shot = tmp_path / "shot.png"
        asyncio.run(notifier.send_apprise("x", attach=shot))
        asyncio.run(notifier.send_apprise("x"))
        assert calls == [str(shot), None]

