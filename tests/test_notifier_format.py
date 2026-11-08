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
        assert markdown_ready("json://localhost/path") == ["json://localhost/path"]

    @pytest.mark.parametrize("url,expected", [
        ("tgram://123:abc/456", "tgram://123:abc/456?format=html"),
        ("tgram://123:abc/456?silent=yes", "tgram://123:abc/456?silent=yes&format=html"),
        ("tgram://123:abc/456?format=markdown", "tgram://123:abc/456?format=markdown"),
    ])
    def test_telegram_is_told_to_take_html(self, url, expected):
        assert markdown_ready(url) == [expected]

    def test_several_services_are_handled_one_by_one(self):
        assert markdown_ready("ntfy://ntfy.sh/a, tgram://1:a/2") == [
            "ntfy://ntfy.sh/a?format=markdown", "tgram://1:a/2?format=html"]

    def test_nothing_configured_is_not_a_crash(self):
        assert markdown_ready("") == [] and markdown_ready(None) == []

    def test_apprise_really_switches_format(self):
        # The point of the rewrite: this is what makes the plugin send X-Markdown.
        ap = apprise.Apprise()
        for url in markdown_ready("ntfy://ntfy.sh/topic"):
            ap.add(url)
        assert [s.notify_format for s in ap] == [apprise.NotifyFormat.MARKDOWN]


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

    def test_telegram_gets_bold_links_not_raw_markdown(self, monkeypatch):
        # Apprise 2 sends Telegram its own Markdown by default, which shows "[Astral Ascent](https://…)" as text.
        import json

        import requests
        sent = []

        class Reply:
            status_code = 200
            content = b'{"ok": true, "result": {"id": 1, "username": "bot", "first_name": "bot"}}'
            text = content.decode()
            headers = {}

            def json(self):
                return json.loads(self.content)

        def post(url, *args, **kwargs):
            body = kwargs.get("data") or kwargs.get("json")
            sent.append(json.loads(body) if isinstance(body, (str, bytes)) else body)
            return Reply()

        monkeypatch.setattr(requests, "post", post)
        monkeypatch.setattr(requests.Session, "post", lambda self, url, *a, **k: post(url, *a, **k))
        ap = apprise.Apprise()
        for url in markdown_ready("tgram://123456789:abcdefghijklmnopqrstuvwxyz0123456/12345"):
            ap.add(url)
        assert ap.notify(body=SUMMARY, title="T", body_format=apprise.NotifyFormat.MARKDOWN)
        message = next(s for s in sent if s and "text" in s)
        assert message["parse_mode"] == "HTML"
        assert '<a href="https://store.epicgames.com/en-US/p/astral-ascent">' in message["text"]
        assert "**" not in message["text"] and "](" not in message["text"]

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

    def _take(self, monkeypatch, tmp_path, on, failed=True, debug=True):
        import asyncio
        import logging

        import src.core.claimer as claimer
        monkeypatch.setattr(claimer.cfg, "debug_screenshots", on)
        monkeypatch.setattr(claimer.cfg, "debug", debug)

        class Store:
            store_name, logger = "steam", logging.getLogger("test")

            def __init__(self):
                self.page, self.sent = self, []

            async def send(self, command):
                import base64
                return base64.b64encode(b"jpg").decode()

            def screenshot_path(self, *parts):
                return tmp_path.joinpath(*parts)

            async def notify(self, message, **kwargs):
                self.sent.append((message, kwargs.get("screenshot_path")))

        store = Store()
        shot = asyncio.run(claimer.BaseClaimer.take_screenshot(store, "steam_fail_Game", failed=failed))
        return shot, store.sent

    def test_the_screenshot_goes_out_when_asked(self, monkeypatch, tmp_path):
        shot, sent = self._take(monkeypatch, tmp_path, True)
        import re
        assert re.fullmatch(r"steam_fail_Game_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.jpg", shot.name)
        assert shot.exists() and sent == [(f"📸 steam: `{shot.name}`", shot)]

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

    def test_screenshots_older_than_three_months_are_deleted(self, monkeypatch, tmp_path):
        import os
        import time

        old, recent, note = tmp_path / "epic_fail_old.jpg", tmp_path / "epic_fail_new.png", tmp_path / "notes.txt"
        for f in (old, recent, note):
            f.write_bytes(b"x")
        stamp = time.time() - 91 * 86400
        os.utime(old, (stamp, stamp))
        os.utime(note, (stamp, stamp))
        shot, _ = self._take(monkeypatch, tmp_path, False)
        assert not old.exists() and recent.exists() and note.exists() and shot.exists()

    def test_without_debug_no_screenshot_is_taken_or_sent(self, monkeypatch, tmp_path):
        shot, sent = self._take(monkeypatch, tmp_path, True, debug=False)
        assert shot is None and sent == [] and not list(tmp_path.iterdir())

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



class TestALongMessageArrivesInParts:
    """Telegram refuses more than 4096 characters and Apprise sends a long message as it is, so it never arrived."""

    LINE = "• **[Agent A: A puzzle in disguise (Android)](https://store.epicgames.com/en-US/p/agent-a-897342)**: claimed"

    def test_a_short_message_is_one_part(self):
        from src.core.notifier import split_message
        assert split_message("a\nb", 4000) == ["a\nb"]

    def test_parts_stay_under_the_limit_and_keep_every_line_whole(self):
        from src.core.notifier import split_message
        message = "\n".join([self.LINE] * 60)
        parts = split_message(message, 4000)
        assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
        assert all(line == self.LINE for p in parts for line in p.split("\n"))
        assert "\n".join(parts) == message

    def test_a_line_longer_than_the_limit_is_cut_not_refused(self):
        from src.core.notifier import split_message
        parts = split_message("x" * 9000, 4000)
        assert [len(p) for p in parts] == [4000, 4000, 1000]

    def test_apprise_sends_each_part_and_the_screenshot_once(self, monkeypatch, tmp_path):
        import asyncio

        import src.core.notifier as notifier
        calls = []

        class Apprise:
            def add(self, url):
                pass

            def notify(self, **kwargs):
                calls.append((len(kwargs["body"]), kwargs.get("attach")))
                return True

        monkeypatch.setattr(notifier.apprise, "Apprise", Apprise)
        monkeypatch.setattr(notifier.cfg, "notify_url", "tgram://token/chat")
        shot = tmp_path / "shot.png"
        asyncio.run(notifier.send_apprise("\n".join([self.LINE] * 60), attach=shot))
        assert len(calls) > 1 and all(size <= notifier.APPRISE_MAX for size, _ in calls)
        assert [attach for _, attach in calls] == [str(shot)] + [None] * (len(calls) - 1)

    def test_a_refused_part_is_logged_as_a_warning_not_as_sent(self, monkeypatch, caplog):
        import asyncio
        import logging

        import src.core.notifier as notifier

        class Apprise:
            def add(self, url):
                pass

            def notify(self, **kwargs):
                return False

        monkeypatch.setattr(notifier.apprise, "Apprise", Apprise)
        monkeypatch.setattr(notifier.cfg, "notify_url", "tgram://token/chat")
        with caplog.at_level(logging.DEBUG, logger="fgc.notifier"):
            asyncio.run(notifier.send_apprise("x"))
        assert any(r.levelno == logging.WARNING and "could not deliver" in r.getMessage() for r in caplog.records)
        assert not any("notification sent" in r.getMessage() for r in caplog.records)

    def test_discord_uses_the_same_split(self):
        source = (ROOT / "src" / "core" / "notifier.py").read_text(encoding="utf-8")
        assert "split_message(message, DISCORD_MAX)" in source.split("async def send_discord", 1)[1].split("\ndef ", 1)[0]
