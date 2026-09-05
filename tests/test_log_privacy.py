"""Account names in the log and in notifications.

The README used to ask you to delete your e-mail from the log before sending it
to a bug report. That only protects the people who remember to do it.
"""

from pathlib import Path

import pytest

from src.core.claimer import mask_account

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
