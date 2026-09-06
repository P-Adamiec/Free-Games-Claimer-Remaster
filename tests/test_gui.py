"""Focused tests for the local dashboard's privacy and control paths."""

import asyncio
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from src.gui import settings
from src.gui.server import start_dashboard
from src.gui.state import DashboardState, summarize_store_result


def test_secret_values_are_never_returned(monkeypatch):
    monkeypatch.setattr(settings.cfg, "ae_email", "private@example.invalid")
    monkeypatch.setattr(settings.cfg, "ae_password", "do-not-return")

    payload = settings.get_settings(["aliexpress"])

    assert "AE_EMAIL" not in payload["values"]
    assert "AE_PASSWORD" not in payload["values"]
    assert payload["configured"]["AE_EMAIL"] is True
    assert payload["configured"]["AE_PASSWORD"] is True
    assert "private@example.invalid" not in json.dumps(payload)
    assert "do-not-return" not in json.dumps(payload)


def test_settings_are_validated_persisted_and_applied(tmp_path, monkeypatch):
    target = tmp_path / "gui.env"
    monkeypatch.delenv("STORES", raising=False)
    monkeypatch.delenv("SCHEDULER_HOURS", raising=False)
    monkeypatch.setattr(settings.cfg, "stores", "")
    monkeypatch.setattr(settings.cfg, "scheduler_hours", 12)

    result = settings.save_settings(
        {"STORES": ["epic", "aliexpress"], "SCHEDULER_HOURS": 0},
        target,
    )

    assert result["changed"] == ["STORES", "SCHEDULER_HOURS"]
    assert settings.cfg.stores == "epic,aliexpress"
    assert settings.cfg.scheduler_hours == 0
    saved = target.read_text(encoding="utf-8")
    assert "STORES='epic,aliexpress'" in saved
    assert "SCHEDULER_HOURS='0'" in saved


def test_setup_is_marked_only_after_settings_are_saved(tmp_path, monkeypatch):
    settings_file = tmp_path / "gui.env"
    marker = tmp_path / "setup.json"
    monkeypatch.setattr(settings.cfg, "gui_setup_required", True)
    monkeypatch.setattr(settings.cfg, "stores", "epic")

    assert settings.get_setup_state(marker) == {"required": True, "complete": False}
    result = settings.complete_setup(
        {"STORES": ["epic", "aliexpress"]},
        settings_path=settings_file,
        marker_path=marker,
    )

    assert result["setup"] == {"required": True, "complete": True}
    assert settings.get_setup_state(marker)["complete"] is True
    assert json.loads(marker.read_text(encoding="utf-8")) == {"complete": True}


@pytest.mark.parametrize(
    "values",
    [
        {"STORES": ["unknown"]},
        {"STORES": []},
        {"SCHEDULER_HOURS": -1},
        {"SCHEDULER_FIXED_TIMES": "25:90"},
        {"SCHEDULER_TIMEZONE": "Nowhere/Invalid"},
        {"UNSAFE_SETTING": "value"},
    ],
)
def test_invalid_settings_are_rejected(tmp_path, values):
    with pytest.raises(settings.SettingsError):
        settings.save_settings(values, tmp_path / "gui.env")


def test_blank_secret_keeps_existing_value(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.cfg, "ae_password", "existing-secret")
    result = settings.save_settings({"AE_PASSWORD": ""}, tmp_path / "gui.env")

    assert result["changed"] == []
    assert settings.cfg.ae_password == "existing-secret"


def test_dashboard_state_only_exposes_safe_result_details():
    state = DashboardState()
    state.begin_run(["aliexpress"])
    state.begin_store("aliexpress")
    state.finish_store(
        "aliexpress",
        "15 coins collected",
        details={"kind": "coins", "claimedCoins": 15, "balance": 120},
    )
    state.finish_run()

    payload = state.snapshot(["aliexpress"], {"nextRun": None})
    ali = next(store for store in payload["stores"] if store["key"] == "aliexpress")
    assert payload["running"] is False
    assert ali["state"] == "success"
    assert ali["enabled"] is True
    assert ali["details"] == {"kind": "coins", "claimedCoins": 15, "balance": 120}
    assert set(ali) == {"name", "badge", "color", "key", "state", "message", "messageKey", "lastRun", "details", "enabled"}
    assert ali["messageKey"] == "status.resultCoins"


def test_game_result_summary_keeps_titles_but_removes_codes_accounts_and_urls():
    message, details = summarize_store_result(
        "prime",
        {
            "user": "private@example.invalid",
            "games": [{
                "title": "Example Game",
                "url": "https://secret.invalid/redeem/ABC-123",
                "status": "code: ABC-123 (GOG)",
            }],
        },
    )

    serialized = json.dumps(details)
    assert message == "1 claimed · 1 checked"
    assert details == {
        "kind": "games",
        "items": [{"title": "Example Game", "outcome": "claimed"}],
    }
    assert "private@example.invalid" not in serialized
    assert "ABC-123" not in serialized
    assert "secret.invalid" not in serialized


def test_aliexpress_summary_exposes_coin_balance_and_streak():
    message, details = summarize_store_result(
        "aliexpress",
        {
            "user": "private@example.invalid",
            "checkin": {
                "outcome": "collected",
                "claimedCoins": 15,
                "offeredCoins": 15,
                "balance": 480,
                "streakDays": 7,
                "tomorrowCoins": 18,
            },
        },
    )

    assert message == "15 coins collected"
    assert details == {
        "kind": "coins",
        "outcome": "collected",
        "claimedCoins": 15,
        "offeredCoins": 15,
        "balance": 480,
        "streakDays": 7,
        "tomorrowCoins": 18,
    }
    assert "private@example.invalid" not in json.dumps(details)


def test_dashboard_http_api_and_csrf():
    loop = asyncio.new_event_loop()
    loop_thread = Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    async def status():
        return {"running": False, "stores": [], "schedule": {}}

    async def config():
        return {"schema": [], "values": {}, "configured": {}}

    async def save(_values):
        return {"changed": [], "restartRequired": []}

    async def setup(_values):
        return {"changed": [], "restartRequired": [], "setup": {"required": True, "complete": True}}

    async def run(_stores):
        return True

    server = start_dashboard(
        loop=loop,
        port=0,
        status_callback=status,
        config_callback=config,
        save_callback=save,
        setup_callback=setup,
        run_callback=run,
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/api/status", timeout=3) as response:
            assert json.load(response)["running"] is False

        with urlopen(f"{base}/assets/icons/aliexpress.svg", timeout=3) as response:
            assert response.headers.get_content_type() == "image/svg+xml"
            assert response.read().startswith(b"<svg")

        with urlopen(f"{base}/assets/fonts/newsreader-latin.woff2", timeout=3) as response:
            assert response.headers.get_content_type() == "font/woff2"
            assert response.read(4) == b"wOF2"

        with urlopen(f"{base}/assets/locales/es.json", timeout=3) as response:
            assert json.load(response)["app"]["title"] == "Free Games Claimer"

        denied = Request(
            f"{base}/api/run",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(denied, timeout=3)
        assert error.value.code == 403

        allowed = Request(
            f"{base}/api/run",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-FGC-Token": server.csrf_token},
            method="POST",
        )
        with urlopen(allowed, timeout=3) as response:
            assert response.status == 202
            assert json.load(response)["accepted"] is True

        setup_request = Request(
            f"{base}/api/setup",
            data=b'{"values": {}}',
            headers={"Content-Type": "application/json", "X-FGC-Token": server.csrf_token},
            method="POST",
        )
        with urlopen(setup_request, timeout=3) as response:
            assert json.load(response)["setup"]["complete"] is True
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=3)
        loop.close()


def test_frontend_is_local_and_contains_store_controls():
    static = Path(__file__).resolve().parent.parent / "src" / "gui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    style = (static / "app.css").read_text(encoding="utf-8")

    assert "__FGC_TOKEN__" in html
    assert "Free Games Claimer" in html
    assert '<html lang="en">' in html
    assert "let currentLocale = 'en'" in script
    assert "data-i18n=\"dashboard.runNow\"" in html
    assert "data-i18n=\"settings.title\"" in html
    assert "data-i18n=\"security.title\"" in html
    assert "onboardingForm" in html
    assert "/api/run" in script
    assert "/api/config" in script
    assert "/api/setup" in script
    assert "'aliexpress'" in script
    assert "Unity" in (static / "locales" / "en.json").read_text(encoding="utf-8")
    assert "filter(store => store.enabled)" in script
    assert "fgc-dashboard-language" in script
    assert "credentialPurposeKey" in script
    assert "help-tooltip" in script
    assert "result-outcome" in script
    assert "storeManagerForm" in html
    assert "/assets/icons/aliexpress.svg" in style
    assert "@media (max-width: 900px)" in style
    assert "@media (max-width: 640px)" in style
    assert "Rafael Caires" not in html
    assert "claimer-control://" not in script


def test_locales_have_identical_translation_keys():
    locale_dir = Path(__file__).resolve().parent.parent / "src" / "gui" / "static" / "locales"

    def flatten(value, prefix=""):
        result = set()
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, dict):
                result.update(flatten(child, path))
            else:
                result.add(path)
        return result

    locales = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in locale_dir.glob("*.json")}
    assert set(locales) == {"en", "pt-BR", "es"}
    expected = flatten(locales["en"])
    for name, payload in locales.items():
        assert flatten(payload) == expected, f"translation keys differ for {name}"


def test_every_secret_credential_has_a_localized_purpose(monkeypatch):
    monkeypatch.setattr(settings.cfg, "gui_setup_required", False)
    schema = settings.get_settings(["epic"])["schema"]
    credential_keys = {
        item["credentialPurposeKey"]
        for item in schema
        if item["key"].endswith(("_EMAIL", "_USERNAME", "_PASSWORD", "_OTPKEY"))
    }
    assert "" not in credential_keys
    assert credential_keys
