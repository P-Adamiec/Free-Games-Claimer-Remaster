"""[fork:webui] The web app: login, throttling, CSRF, and every person only sees their own bot."""

import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from webui.app import main as webapp  # noqa: E402
from webui.app import security  # noqa: E402

ADMIN_PW = "admin password 1"
USER_PW = "user password 22"


@pytest.fixture
def env(tmp_path):
    for name in ("alice", "bob"):
        (tmp_path / "bots" / name).mkdir(parents=True)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "alice.env").write_text("VNC_PASSWORD=alicevnc\n")
    (tmp_path / "env" / "bob.env").write_text("VNC_PASSWORD=bobvnc12\n")
    return {"WEBUI_DATA_DIR": str(tmp_path / "w"), "FGC_BOTS": "alice,bob", "FGC_BOTS_DIR": str(tmp_path / "bots"),
            "FGC_BOTS_ENV_DIR": str(tmp_path / "env"), "WEBUI_ADMIN_USER": "alice",
            "WEBUI_ADMIN_PASSWORD": ADMIN_PW, "WEBUI_COOKIE_SECURE": "false"}


@pytest.fixture
def app(env):
    application = webapp.create_app(env)
    application.state.users.create("bob", USER_PW, "user", "bob")
    return application


def csrf(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(app, name, password):
    client = TestClient(app)
    token = csrf(client)
    r = client.post("/login", data={"username": name, "password": password, "csrf": token})
    return client, r


class TestStartup:
    def test_refuses_to_start_without_any_account(self, env):
        env = {**env, "WEBUI_ADMIN_PASSWORD": ""}
        with pytest.raises(SystemExit, match="WEBUI_ADMIN_PASSWORD"):
            webapp.create_app(env)

    def test_a_new_bot_gets_its_own_schedule(self, app, env):
        from pathlib import Path
        alice = (Path(env["FGC_BOTS_DIR"]) / "alice" / "config.env").read_text()
        bob = (Path(env["FGC_BOTS_DIR"]) / "bob" / "config.env").read_text()
        assert "06:00,18:00" in alice and "09:00,21:00" in bob
        assert "RUN_ON_STARTUP='false'" in alice


class TestLogin:
    def test_pages_need_a_login(self, app):
        r = TestClient(app).get("/instellingen", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")

    def test_good_login(self, app):
        _, r = login(app, "bob", USER_PW)
        assert r.status_code == 200 and "Overzicht" in r.text

    def test_login_needs_the_csrf_token(self, app):
        r = TestClient(app).post("/login", data={"username": "bob", "password": USER_PW})
        assert r.status_code == 400

    def test_next_cannot_leave_the_site(self):
        assert webapp._safe_next("//evil.example/") == "/"
        assert webapp._safe_next("https://evil.example/") == "/"
        assert webapp._safe_next("/instellingen") == "/instellingen"

    def test_too_many_failures_lock_the_account_out(self, app, monkeypatch):
        monkeypatch.setattr(security.Throttle, "delay", lambda self, *k: 0.0)
        client = TestClient(app)
        token = csrf(client)
        for _ in range(security.LOCK_AFTER):
            client.post("/login", data={"username": "bob", "password": "nope nope", "csrf": token})
        r = client.post("/login", data={"username": "bob", "password": USER_PW, "csrf": token})
        assert r.status_code == 429


class TestThrottle:
    def test_delay_grows_then_blocks(self):
        now = [0.0]
        t = security.Throttle(clock=lambda: now[0])
        assert t.delay("ip:x") == 0
        for _ in range(security.FREE_TRIES + 1):
            t.fail("ip:x")
        assert t.delay("ip:x") > 0
        for _ in range(security.LOCK_AFTER):
            t.fail("ip:x")
        assert t.blocked("ip:x")
        now[0] += security.WINDOW + 1
        assert not t.blocked("ip:x")


class TestIsolation:
    def test_settings_post_needs_csrf(self, app):
        client, _ = login(app, "bob", USER_PW)
        r = client.post("/instellingen", data={"stores": ["steam"]})
        assert r.status_code == 403

    def test_each_person_edits_only_their_own_bot(self, app, env):
        from pathlib import Path
        client, _ = login(app, "bob", USER_PW)
        token = csrf(client, "/instellingen")
        r = client.post("/instellingen", data={"csrf": token, "stores": ["steam"], "STEAM_USERNAME": "bobsteam",
                                               "STEAM_PASSWORD": "bob-secret-pw", "SCHEDULER_HOURS": "12"})
        assert r.status_code == 200
        assert "bob-secret-pw" in (Path(env["FGC_BOTS_DIR"]) / "bob" / "config.env").read_text()
        assert "bobsteam" not in (Path(env["FGC_BOTS_DIR"]) / "alice" / "config.env").read_text()
        # Saving asks the bot to restart, so it reads the new file.
        assert (Path(env["FGC_BOTS_DIR"]) / "bob" / "webui" / "request-restart.json").exists()

    def test_secrets_never_reach_the_page(self, app):
        client, _ = login(app, "bob", USER_PW)
        token = csrf(client, "/instellingen")
        client.post("/instellingen", data={"csrf": token, "stores": ["steam"], "STEAM_PASSWORD": "bob-secret-pw",
                                           "SCHEDULER_HOURS": "12"})
        page = client.get("/instellingen").text
        assert "bob-secret-pw" not in page and "ingesteld (leeg laten" in page

    def test_only_your_own_vnc_password(self, app):
        client, _ = login(app, "bob", USER_PW)
        page = client.get("/").text
        assert "bobvnc12" in page and "alicevnc" not in page

    def test_users_cannot_open_admin(self, app):
        client, _ = login(app, "bob", USER_PW)
        assert client.get("/beheer").status_code == 403

    def test_admin_sees_everyone(self, app):
        client, _ = login(app, "alice", ADMIN_PW)
        page = client.get("/beheer").text
        assert "bob" in page and "alice" in page

    def test_vnc_socket_needs_a_login(self, app):
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with TestClient(app).websocket_connect("/vnc/websockify") as ws:
                ws.receive_bytes()

    def test_vnc_socket_refuses_other_sites(self, app):
        from starlette.websockets import WebSocketDisconnect
        client, _ = login(app, "bob", USER_PW)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/vnc/websockify", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_bytes()


class TestHeaders:
    def test_security_headers(self, app):
        r = TestClient(app).get("/login")
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"

    def test_session_cookie_flags(self, env):
        application = webapp.create_app({**env, "WEBUI_COOKIE_SECURE": "true"})
        r = TestClient(application, base_url="https://testserver").get("/login")
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
