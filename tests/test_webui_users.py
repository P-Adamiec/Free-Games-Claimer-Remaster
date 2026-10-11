"""[fork:webui] Web accounts: hashed passwords, roles, one bot per account."""

import json
import stat

import pytest

pytest.importorskip("argon2")

from webui.app.users import UserError, UserStore, fingerprint  # noqa: E402

PW = "correct horse battery"


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.json")


class TestPasswords:
    def test_right_password(self, store):
        store.create("alice", PW)
        assert store.verify("alice", PW)["role"] == "user"

    def test_wrong_password(self, store):
        store.create("alice", PW)
        assert store.verify("alice", "wrong password!") is None

    def test_unknown_user(self, store):
        assert store.verify("nobody", PW) is None

    def test_never_stored_in_plain_text(self, store):
        store.create("alice", PW)
        text = store.path.read_text()
        assert PW not in text and "$argon2" in text

    def test_file_is_private(self, store):
        store.create("alice", PW)
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    def test_too_short(self, store):
        with pytest.raises(UserError):
            store.create("alice", "short")

    def test_a_reset_logs_old_sessions_out(self, store):
        store.create("alice", PW)
        before = fingerprint(store.get("alice"))
        store.set_password("alice", "another long password")
        assert fingerprint(store.get("alice")) != before


class TestNamesAndBots:
    @pytest.mark.parametrize("name", ["", "A", "1abc", "a b", "../x", "x" * 40])
    def test_bad_names(self, store, name):
        with pytest.raises(UserError):
            store.create(name, PW)

    def test_names_are_case_insensitive(self, store):
        store.create("Alice", PW)
        assert store.get("alice")

    def test_duplicate(self, store):
        store.create("alice", PW)
        with pytest.raises(UserError):
            store.create("alice", PW)

    def test_one_bot_one_person(self, store):
        store.create("alice", PW, bot="alice")
        with pytest.raises(UserError):
            store.create("bob", PW, bot="alice")
        store.create("bob", PW)
        with pytest.raises(UserError):
            store.update("bob", bot="alice")


class TestAdmins:
    def test_the_last_admin_stays(self, store):
        store.create("root", PW, role="admin")
        with pytest.raises(UserError):
            store.delete("root")
        with pytest.raises(UserError):
            store.update("root", role="user")

    def test_bootstrap_only_without_accounts(self, store):
        assert store.bootstrap("admin", PW, ["admin"]) == "created"
        assert store.get("admin")["role"] == "admin" and store.get("admin")["bot"] == "admin"
        assert store.bootstrap("other", PW, []) == "exists"
        assert store.get("other") is None

    def test_bootstrap_needs_both_values(self, store):
        assert store.bootstrap("admin", "", []) == "missing"
        assert store.empty()

    def test_a_broken_file_reads_as_empty(self, store):
        store.path.write_text("{not json")
        assert store.empty()
        store.path.write_text(json.dumps({"users": []}))
        assert store.empty()
