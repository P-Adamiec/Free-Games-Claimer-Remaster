"""The one two-factor rule, checked across every store.

Two codes from the authenticator secret, then a recovery code where there is one,
then the screen is left to you over VNC. Each store types into its own fields, so
only the order and the code itself are shared.
"""

import ast
import asyncio
from pathlib import Path

import pytest

from src.core.claimer import BaseClaimer, OTP_KEY_ATTEMPTS

ROOT = Path(__file__).resolve().parent.parent
STORES = sorted((ROOT / "src" / "stores").glob("*.py"))


class TestFreshCode:
    """A code refused inside its 30-second window stays refused, so the retry waits for a new one."""

    class _Totp:
        """Stands in for pyotp: hands out the next code each time the clock is asked."""

        codes = ["111111", "111111", "222222"]

        def __init__(self, secret):
            self.secret = secret
            self.calls = 0

        def now(self):
            code = self.codes[min(self.calls, len(self.codes) - 1)]
            self.calls += 1
            return code

    @pytest.fixture()
    def claimer(self, monkeypatch):
        from src.core import claimer as claimer_module

        monkeypatch.setattr(claimer_module.pyotp, "TOTP", self._Totp)
        # Real sleeps would make waiting for the next window take half a minute per test.
        monkeypatch.setattr(claimer_module.asyncio, "sleep", self._no_wait)
        return BaseClaimer.__new__(BaseClaimer)

    @staticmethod
    async def _no_wait(_seconds):
        return None

    def test_the_first_code_is_taken_as_it_is(self, claimer):
        assert asyncio.run(claimer._fresh_totp("SECRET")) == "111111"

    def test_a_repeat_is_waited_out(self, claimer):
        # The same digits again would earn the same refusal, so the retry is worth nothing.
        assert asyncio.run(claimer._fresh_totp("SECRET", "111111")) == "222222"

    def test_it_gives_up_waiting_rather_than_hanging(self, claimer, monkeypatch):
        class Stuck(self._Totp):
            codes = ["333333"]

        from src.core import claimer as claimer_module
        monkeypatch.setattr(claimer_module.pyotp, "TOTP", Stuck)
        assert asyncio.run(claimer._fresh_totp("SECRET", "333333")) == "333333"


class TestEveryStoreUsesTheSharedCode:
    """A store generating its own code would skip the wait and repeat a refused one."""

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_no_store_calls_the_clock_itself(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called = ast.unparse(node.func)
                assert "pyotp.TOTP" not in called or not called.endswith(".now"), \
                    f"{path.name} generates its own code, use _fresh_totp()"

    @pytest.mark.parametrize("name", ["epic.py", "epic_fab.py", "ubisoft.py", "prime.py",
                                      "gog.py", "gamerpower.py"])
    def test_the_store_asks_for_a_fresh_code(self, name):
        assert "_fresh_totp(" in (ROOT / "src" / "stores" / name).read_text(encoding="utf-8")


class TestTheAttemptCountIsShared:
    """Five files with their own number is how they drifted apart in the first place."""

    def test_two_goes_with_the_secret(self):
        assert OTP_KEY_ATTEMPTS == 2

    @pytest.mark.parametrize("name", ["epic.py", "epic_fab.py", "ubisoft.py", "prime.py",
                                      "gog.py", "gamerpower.py"])
    def test_no_store_hardcodes_its_own(self, name):
        assert "OTP_KEY_ATTEMPTS" in (ROOT / "src" / "stores" / name).read_text(encoding="utf-8")


class TestTheOrderIsTheSame:
    """The secret is free to retry, a recovery code is spent, so the secret goes first."""

    @pytest.mark.parametrize("name", ["epic.py", "gog.py", "gamerpower.py"])
    def test_a_code_is_only_spent_after_the_secret_ran_out(self, name):
        source = (ROOT / "src" / "stores" / name).read_text(encoding="utf-8")
        # The count is read in the login flow; the call that spends a code comes after it.
        uses = source.index("OTP_KEY_ATTEMPTS", source.index("OTP_KEY_ATTEMPTS") + 1)
        assert uses < source.index("self._fill_backup_code(")

    @pytest.mark.parametrize("name", ["epic.py", "gog.py", "gamerpower.py"])
    def test_the_secret_is_what_the_count_guards(self, name):
        source = (ROOT / "src" / "stores" / name).read_text(encoding="utf-8")
        uses = source.index("OTP_KEY_ATTEMPTS", source.index("OTP_KEY_ATTEMPTS") + 1)
        # What the count limits is the secret: the code that follows it fills in a TOTP.
        assert "_fill_totp(" in source[uses:]
