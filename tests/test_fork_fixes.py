"""[fork:fix] Tests for the upstream fixes in this fork: Prime stops when signing in fails, GOG
actually unticks the newsletters, and Chrome gets time to start after a reboot."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.stores import prime as prime_mod
from src.stores.gog import GOGClaimer
from src.stores.prime import PrimeGamingClaimer

ROOT = Path(__file__).resolve().parent.parent


async def _none(*_a, **_k):
    return None


def _prime(signed_in: bool, vnc_ok: bool) -> PrimeGamingClaimer:
    claimer = PrimeGamingClaimer.__new__(PrimeGamingClaimer)
    claimer.sleep = _none
    claimer.log_signed_in = lambda *a, **k: None
    claimer._vnc_notice = lambda *a, **k: ""
    claimer._is_signed_in = lambda: asyncio.sleep(0, signed_in)
    claimer._wait_for_vnc_login = lambda *a, **k: asyncio.sleep(0, vnc_ok)
    claimer._click_sign_in = lambda: asyncio.sleep(0, False)
    claimer._code_screen_present = lambda: asyncio.sleep(0, False)
    claimer.page = SimpleNamespace(get=_none, evaluate=lambda *_a: asyncio.sleep(0, json.dumps({"found": False})))
    return claimer


@pytest.fixture
def no_credentials(monkeypatch):
    monkeypatch.setattr(prime_mod, "cfg", SimpleNamespace(pg_email=None, pg_password=None))


def test_prime_reports_success_when_already_signed_in(no_credentials):
    assert asyncio.run(_prime(signed_in=True, vnc_ok=False)._ensure_logged_in()) is True


def test_prime_reports_failure_after_an_unanswered_login(no_credentials):
    assert asyncio.run(_prime(signed_in=False, vnc_ok=False)._ensure_logged_in()) is False


def test_prime_stops_before_claiming_when_not_signed_in():
    source = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")
    step = source.split("# Step 2:", 1)[1].split("# Step 3:", 1)[0]
    assert "if not await self._ensure_logged_in():" in step and "return" in step
    # Every other caller acts on the result too.
    assert source.count("await self._ensure_logged_in(") == source.count("if not await self._ensure_logged_in(")


def test_gog_unsubscribe_unticks_the_newsletters():
    source = (ROOT / "src" / "stores" / "gog.py").read_text(encoding="utf-8")
    block = source.split("async def _unsubscribe_newsletter", 1)[1].split("# [fork:fix] end", 1)[0]
    assert ".click()" in block and "box.checked" in block
    assert "Promotions and hot deals" in source
    assert "await self._unsubscribe_newsletter()" in source


def test_gog_newsletter_script_is_valid_javascript_shape():
    names = json.dumps(list(GOGClaimer.NEWSLETTERS))
    assert names.startswith('["Promotions')


def test_chrome_gets_time_after_a_reboot():
    """[fork:fix] Right after a host reboot Chrome needs more than nodriver's few seconds; wait 10 s and 20 s."""
    source = (ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
    block = source.split("for attempt in range(1, 4):", 1)[1].split("if launch_error is not None:", 1)[0]
    assert "await asyncio.sleep(10 * attempt)" in block
