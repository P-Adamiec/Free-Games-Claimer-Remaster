"""Account names in the log and in notifications.

The README used to ask you to delete your e-mail from the log before sending it
to a bug report. That only protects the people who remember to do it.
"""

import ast
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import redact as redaction
from src.core.redact import RedactFilter, hide, hide_account, hide_settings, mask_account, mask_code, redact

ROOT = Path(__file__).resolve().parent.parent


class TestMaskAccount:
    @pytest.mark.parametrize("raw,masked", [
        ("pawel@gmail.com", "p***@gmail.com"),
        ("picktheweak+itchio@gmail.com", "p***@gmail.com"),   # the +tag is part of the address
        ("a@b.c", "a***@b.c"),
        ("@nodomain", "***@nodomain"),
    ])
    def test_an_address_keeps_only_its_first_letter_and_domain(self, raw, masked):
        assert mask_account(raw) == masked

    @pytest.mark.parametrize("nickname", ["Not Weak.", "PawelA", "PickTheWeak", "AliExpress User"])
    def test_a_nickname_stays_readable(self, nickname):
        # Only an address is personal data; masking display names would make the log useless.
        assert mask_account(nickname) == nickname

    @pytest.mark.parametrize("empty", ["", None])
    def test_nothing_in_nothing_out(self, empty):
        assert mask_account(empty) == ""


class TestEveryLogSiteUsesIt:
    """One unmasked call site is enough to put the address back in a public issue."""

    CLAIMER = (ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
    GAMERPOWER = (ROOT / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
    MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

    def test_the_shared_signed_in_line(self):
        block = self.CLAIMER.split("def log_signed_in", 1)[1].split("\n    def ", 1)[0]
        assert "mask_account(user)" in block

    def test_the_side_store_signed_in_line(self):
        block = self.GAMERPOWER.split("def _log_side_signed_in", 1)[1].split("\n    async def ", 1)[0]
        assert "mask_account(account)" in block

    @pytest.mark.parametrize("store", ["Fanatical", "Itch.io", "IndieGala"])
    def test_the_side_store_login_lines(self, store):
        line = f'logger.info("[{store}] Logging in as %s\u2026", mask_account(email))'
        assert line in self.GAMERPOWER

    def test_the_notification_header(self):
        assert "mask_account(result.get('user'))" in self.MAIN

    def test_the_database_lines_mask_the_account(self):
        # Epic falls back to EG_EMAIL when it shows no profile name, and these lines run for every game.
        source = (ROOT / "src" / "core" / "database.py").read_text(encoding="utf-8")
        assert source.count("mask_account(user)") == 2

    def test_no_call_site_was_left_behind(self):
        # Every "Logging in as" and "Signed in as" call has to go through the mask. The
        # arguments can sit on the next line, so judge the statement, not the line.
        for source in (self.CLAIMER, self.GAMERPOWER, self.MAIN):
            lines = source.splitlines()
            for i, line in enumerate(lines):
                if "Logging in as" not in line and "Signed in as:" not in line:
                    continue
                if line.strip().startswith("#"):
                    continue
                statement = " ".join(lines[i:i + 3])
                assert "mask_account" in statement, line


class TestNoCodeReachesTheLog:
    """A log file goes into bug reports, so not even part of a code belongs in it."""

    STORES = sorted((ROOT / "src" / "stores").glob("*.py")) + [ROOT / "src" / "core" / "claimer.py"]

    def _logged_arguments(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("debug", "info", "warning", "error", "exception"):
                continue
            for arg in node.args[1:]:
                yield arg

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_no_slice_of_a_code_is_logged(self, path):
        # GOG used to log code_to_use[:3] at info level, which is three characters too many.
        for arg in self._logged_arguments(path.read_text(encoding="utf-8")):
            for node in ast.walk(arg):
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                    assert "code" not in node.value.id.lower(), f"{path.name}: {ast.unparse(node)}"

    @pytest.mark.parametrize("path", STORES + [ROOT / "main.py"], ids=lambda p: p.name)
    def test_no_whole_game_code_is_logged(self, path):
        # A pasted log is enough to lose an unused code; mask_code() keeps only its start (TODO 52).
        for arg in self._logged_arguments(path.read_text(encoding="utf-8")):
            bare = (isinstance(arg, ast.Name) and arg.id in ("code", "key", "notify_game")) or \
                   (isinstance(arg, ast.Attribute) and arg.attr in ("code", "key", "notify_games"))
            # A store's result entries carry Prime's codes in their statuses and links.
            whole_results = ast.unparse(arg).endswith(".get('games')")
            assert not bare and not whole_results, f"{path.name}: {ast.unparse(arg)}"

    def test_a_game_code_keeps_only_its_start(self):
        assert mask_code("ABCDE-FGHIJ-KLMNO") == "ABCDE…"
        assert mask_code("") == mask_code(None) == mask_code("  ") == ""

    def test_the_notification_line_in_the_log_is_only_its_header(self):
        # The summary below the header carries whole codes, the log gets its first line.
        notifier = (ROOT / "src" / "core" / "notifier.py").read_text(encoding="utf-8")
        assert "message[:200]" not in notifier and 'message.split("\\n", 1)[0]' in notifier

    def test_the_log_says_where_the_whole_code_is(self):
        prime = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")
        assert "the whole code is in your notification and data/prime-gaming.json" in prime

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_no_whole_two_factor_code_is_logged(self, path):
        # Sign-in codes never reach the log, not even cut.
        banned = ("raw_code", "code_to_use", "otp_code", "backup_code", "recovery_code", "totp")
        for arg in self._logged_arguments(path.read_text(encoding="utf-8")):
            for node in ast.walk(arg):
                if isinstance(node, ast.Name):
                    assert node.id.lower() not in banned, path.name


class TestNoTokenReachesTheLog:
    """Issue #86: with DEBUG=true, urllib3 logged Apprise's request lines, and Telegram's carry the bot token."""

    def test_urllib3_is_quiet_unless_debug_libs_asks(self):
        import main
        assert "urllib3" in main.NOISY_LIBRARIES
        if not main.cfg.debug_libs:
            assert logging.getLogger("urllib3").level == logging.WARNING


class TestTheMask:
    """Secrets from .env and game codes are masked in every log line, whichever library writes it (#86)."""

    CFG = SimpleNamespace(
        eg_email="pawel@gmail.com", eg_password="Sekret-123", steam_username="pawelsteam",
        gog_otp_key="JBSWY3DPEHPK3PXP", itchio_otp_codes=["11112222", "33334444"], notify_login_request=True,
        discord_webhook="https://discord.com/api/webhooks/123456789012345678/AbCdEf-token_XYZ123",
        notify_url="tgram://123456789:AAH4fakeTelegramToken_abcdefghijk/987654321, ntfy://ntfy.sh/freegames2026",
    )

    @pytest.fixture(autouse=True)
    def _empty(self, monkeypatch):
        monkeypatch.setattr(redaction, "_hidden", {})
        monkeypatch.setattr(redaction, "_pattern", None)
        hide_settings(self.CFG)

    def test_passwords_and_two_factor_secrets_become_stars(self):
        assert redact("typed Sekret-123, key JBSWY3DPEHPK3PXP, code 11112222") == "typed ***, key ***, code ***"

    def test_sign_in_names_keep_their_first_letter(self):
        assert redact("user=pawel@gmail.com steam=pawelsteam") == "user=p***@gmail.com steam=p***"

    def test_the_telegram_token_leaves_the_request_line(self):
        line = redact('"POST /bot123456789:AAH4fakeTelegramToken_abcdefghijk/sendMessage HTTP/1.1" 200')
        assert "AAH4fakeTelegramToken" not in line and "/sendMessage" in line

    def test_the_discord_webhook_keeps_only_its_host(self):
        line = redact("HTTP Request: POST https://discord.com/api/webhooks/123456789012345678/AbCdEf-token_XYZ123")
        assert line == "HTTP Request: POST https://discord.com/api/webhooks/***/***"

    def test_an_ntfy_topic_is_a_secret_too(self):
        assert redact("POST /freegames2026 HTTP/1.1") == "POST /*** HTTP/1.1"

    def test_only_whole_words_and_nothing_short(self):
        hide("abcd")
        hide("ab")
        assert redact("abcd abcde xabcd ab") == "*** abcde xabcd ab"

    def test_a_name_read_from_a_page_is_masked_from_then_on(self):
        # Prime takes your first name from Amazon's header, Epic and GOG your profile name.
        from src.core.claimer import BaseClaimer
        store = SimpleNamespace(user=None, logger=logging.getLogger("test"))
        BaseClaimer.log_signed_in(store, "Paweł")
        assert store.user == "Paweł" and redact("DB hit: prime/Paweł/game") == "DB hit: prime/P***/game"

    def test_a_side_store_account_is_masked_too(self):
        from src.stores.gamerpower import GamerPowerClaimer
        GamerPowerClaimer._log_side_signed_in(SimpleNamespace(logger=logging.getLogger("test")), "Itch.io", "pawel_itch")
        assert redact("account pawel_itch") == "account p***"

    def test_a_three_letter_name_is_masked_but_unknown_is_not(self):
        hide_account("Ewa")
        hide_account("unknown")
        assert redact("Signed in as: Ewa, flow=unknown") == "Signed in as: E***, flow=unknown"

    @pytest.mark.parametrize("line,masked", [
        ("Fanatical says: account jan.kowalski+fgc@sub.example.co.uk is not confirmed",
         "Fanatical says: account j***@sub.example.co.uk is not confirmed"),
        ("https://user:Pa55word@host.example.com/path", "https://user:P***@host.example.com/path"),
        ("    @retry(stop=x) react@18.3.1 user@localhost", "    @retry(stop=x) react@18.3.1 user@localhost"),
        ("already p***@gmail.com", "already p***@gmail.com"),
    ])
    def test_any_email_address_is_masked_even_one_not_in_env(self, line, masked):
        assert redact(line) == masked

    def test_the_filter_masks_an_address_with_nothing_else_hidden(self, monkeypatch):
        monkeypatch.setattr(redaction, "_hidden", {})
        monkeypatch.setattr(redaction, "_pattern", None)
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "quoted %s", ("a.b@c.io",), None)
        RedactFilter().filter(record)
        assert record.getMessage() == "quoted a***@c.io"

    def test_a_saved_game_code_shows_its_first_characters(self):
        from src.core.database import ClaimedGame
        ClaimedGame(store="gog", user="u", game_id="g", title="t", code="ABCDE-FGHIJ-KLMNO")
        assert redact("[parameters: ('gog', 'ABCDE-FGHIJ-KLMNO')]") == "[parameters: ('gog', 'ABCDE…')]"

    def _record(self, error):
        try:
            raise error
        except ValueError:
            return logging.LogRecord("x", logging.ERROR, __file__, 1, "Fatal %s", ("error",), sys.exc_info())

    def test_a_traceback_with_a_secret_is_printed_masked(self):
        record = self._record(ValueError("login failed for pawel@gmail.com with Sekret-123"))
        RedactFilter().filter(record)
        text = record.getMessage()
        assert record.exc_info is None and text.startswith("Fatal error")
        assert "Sekret-123" not in text and "pawel@gmail.com" not in text and "p***@gmail.com" in text

    def test_a_clean_traceback_is_left_to_rich(self):
        record = self._record(ValueError("page not ready"))
        RedactFilter().filter(record)
        assert record.exc_info is not None and record.getMessage() == "Fatal error"

    def test_sql_goes_through_the_mask(self):
        # echo printed every statement a second time through a handler of its own, past the mask (seen live).
        assert "echo=" not in (ROOT / "src" / "core" / "database.py").read_text(encoding="utf-8")

    def test_no_library_prints_past_the_mask(self):
        # Only the root handler carries the mask; a library with a handler of its own would skip it.
        importlib.import_module("main")
        own = sorted(name for name, lg in logging.root.manager.loggerDict.items() if isinstance(lg, logging.Logger)
                     and any(not isinstance(h, logging.NullHandler) for h in lg.handlers))
        assert own == []

    def test_the_log_handler_uses_it(self):
        import main
        assert any(isinstance(f, RedactFilter) for f in main.handler.filters)
