"""[fork:webui] Login help: every store has it, the settings page and the Uitleg page show it."""

import re

import pytest

from webui.app import help as storehelp
from webui.app import schema

REQUIRED = ("mode", "summary", "bot", "you", "twofa", "key_steps", "notes")


class TestEveryStoreHasHelp:
    @pytest.mark.parametrize("key", schema.STORE_KEYS)
    def test_help_exists(self, key):
        h = storehelp.STORE_HELP[key]
        assert all(k in h for k in REQUIRED), key
        assert h["mode"] in storehelp.MODES and h["summary"] and h["you"]

    def test_no_help_for_stores_that_do_not_exist(self):
        assert set(storehelp.STORE_HELP) == set(schema.STORE_KEYS)

    @pytest.mark.parametrize("store", [s for s in schema.STORES if any(f.key.endswith("_OTP_KEY") for f in s.fields)],
                             ids=lambda s: s.key)
    def test_stores_with_an_authenticator_key_explain_how_to_get_it(self, store):
        assert storehelp.STORE_HELP[store.key]["key_steps"]

    def test_plain_gender_neutral_text(self):
        text = repr(storehelp.STORE_HELP) + repr(storehelp.GENERAL)
        assert not re.search(r"\b(hij|zij|hem|haar)\b", text, re.I)


fastapi = pytest.importorskip("fastapi")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from webui.app import main as webapp  # noqa: E402


@pytest.fixture
def client(tmp_path):
    (tmp_path / "bots" / "alice").mkdir(parents=True)
    app = webapp.create_app({"WEBUI_DATA_DIR": str(tmp_path / "w"), "FGC_BOTS": "alice",
                             "FGC_BOTS_DIR": str(tmp_path / "bots"), "FGC_BOTS_ENV_DIR": str(tmp_path / "env"),
                             "WEBUI_ADMIN_USER": "alice", "WEBUI_ADMIN_PASSWORD": "admin password 1",
                             "WEBUI_COOKIE_SECURE": "false", "FGC_QUEUE_DIR": str(tmp_path / "q")})
    c = TestClient(app)
    token = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "alice", "password": "admin password 1", "csrf": token})
    return c


def test_uitleg_lists_every_store(client):
    page = client.get("/uitleg").text
    for store in schema.STORES:
        assert f'id="{store.key}"' in page and store.name in page
    assert "Nu uitvoeren" in page


@pytest.mark.parametrize("store", schema.STORES, ids=lambda s: s.key)
def test_settings_page_shows_help_per_store(client, store):
    page = client.get("/instellingen").text
    assert page.count("Hoe inloggen werkt") == len(schema.STORES)
    from markupsafe import escape
    assert str(escape(storehelp.STORE_HELP[store.key]["summary"])) in page


def test_uitleg_needs_a_login(tmp_path, client):
    anon = TestClient(client.app)
    assert anon.get("/uitleg", follow_redirects=False).status_code == 303
