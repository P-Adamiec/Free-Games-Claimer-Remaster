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
from src.gui.state import DashboardState


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


@pytest.mark.parametrize(
    "values",
    [
        {"STORES": ["unknown"]},
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


def test_dashboard_state_does_not_include_accounts_or_results():
    state = DashboardState()
    state.begin_run(["aliexpress"])
    state.begin_store("aliexpress")
    state.finish_store("aliexpress", "Concluído · 1 resultado(s)")
    state.finish_run()

    payload = state.snapshot(["aliexpress"], {"nextRun": None})
    ali = next(store for store in payload["stores"] if store["key"] == "aliexpress")
    assert payload["running"] is False
    assert ali["state"] == "success"
    assert ali["enabled"] is True
    assert set(ali) == {"name", "badge", "color", "key", "state", "message", "lastRun", "enabled"}


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

    async def run(_stores):
        return True

    server = start_dashboard(
        loop=loop,
        port=0,
        status_callback=status,
        config_callback=config,
        save_callback=save,
        run_callback=run,
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/api/status", timeout=3) as response:
            assert json.load(response)["running"] is False

        with urlopen(f"{base}/assets/icons/aliexpress.svg", timeout=3) as response:
            assert response.headers.get_content_type() == "image/svg+xml"
            assert response.read().startswith(b"<svg")

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

    assert "__FGC_TOKEN__" in html
    assert "Executar agora" in html
    assert "Configurar" in html
    assert "http://" not in html and "https://" not in html
    assert "/api/run" in script
    assert "/api/config" in script
    assert "'aliexpress'" in script
    assert "/assets/icons/aliexpress.svg" in (
        static / "app.css"
    ).read_text(encoding="utf-8")
