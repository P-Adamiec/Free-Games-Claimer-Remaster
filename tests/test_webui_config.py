"""[fork:webui] The web interface writes data/config.env: values must reach the bot unchanged, secrets never come back."""

import os
import stat

import pytest
from dotenv import dotenv_values, load_dotenv

from src.core.config import env_setting_kinds
from webui.app import config_store, schema

TRICKY = ["simple", "p'a\"s$w{o}rd", "back\\slash\\", "$HOME", "#not-a-comment", "  spaces  ", "a=b=c", "ünïcode"]


def _form(**fields):
    """A complete settings form, as a browser posts it: every checkbox that is ticked, every text field."""
    form = {"stores": ["steam", "epic"], "SCHEDULER_HOURS": "12", "SCHEDULER_TIMEZONE": "UTC"}
    form.update(fields)
    return form


class TestEveryFieldIsARealSetting:
    @pytest.mark.parametrize("key", sorted(schema.MANAGED_KEYS))
    def test_the_bot_reads_it(self, key):
        # The bot warns about unknown names in config.env, and a typo would silently do nothing.
        assert key in env_setting_kinds(), f"{key} is offered by the web interface but config.py never reads it"

    def test_docker_only_settings_are_not_offered(self):
        assert not {"VNC_PASSWORD", "PUID", "PGID", "SHOW", "BROWSER_DIR", "RUN_ONCE"} & schema.MANAGED_KEYS


class TestValuesSurviveTheRoundTrip:
    @pytest.mark.parametrize("value", TRICKY)
    def test_dotenv_reads_back_the_exact_value(self, tmp_path, value):
        path = tmp_path / "config.env"
        config_store.write_config(path, {"EG_PASSWORD": value})
        assert dotenv_values(path)["EG_PASSWORD"] == value

    @pytest.mark.parametrize("value", TRICKY)
    def test_the_bots_own_loader_gets_it_too(self, tmp_path, monkeypatch, value):
        # config.py uses load_dotenv(..., override=False) with interpolation on, the default.
        monkeypatch.delenv("EG_PASSWORD", raising=False)
        path = tmp_path / "config.env"
        config_store.write_config(path, {"EG_PASSWORD": value})
        load_dotenv(path, override=False)
        assert os.environ["EG_PASSWORD"] == value

    def test_a_line_break_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            config_store.write_config(tmp_path / "c.env", {"EG_PASSWORD": "a\nSTORES=all"})


class TestTheFile:
    def test_only_the_owner_can_read_it(self, tmp_path):
        path = tmp_path / "config.env"
        config_store.write_config(path, {"EG_EMAIL": "x@example.org"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        config_store.write_config(tmp_path / "config.env", {"EG_EMAIL": "x@example.org"})
        assert [p.name for p in tmp_path.iterdir()] == ["config.env"]

    def test_settings_added_by_hand_are_kept(self):
        new, errors = config_store.apply_form({"BROWSER_DIR": "data/browser"}, _form())
        assert not errors
        assert new["BROWSER_DIR"] == "data/browser"


class TestSecretsAreWriteOnly:
    CURRENT = {"STORES": "epic", "EG_EMAIL": "me@example.org", "EG_PASSWORD": "hunter2-secret",
               "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/abc"}

    def test_the_view_never_contains_a_secret(self):
        text = repr(config_store.view(self.CURRENT, set()))
        assert "hunter2-secret" not in text
        assert "webhooks/1/abc" not in text
        assert "me@example.org" in text  # an e-mail address is not a secret

    def test_the_view_says_it_is_set(self):
        epic = next(s for s in config_store.view(self.CURRENT, set())["stores"] if s["store"].key == "epic")
        pw = next(f for f in epic["fields"] if f["field"].key == "EG_PASSWORD")
        assert pw["is_set"] is True and pw["value"] == ""

    def test_a_blank_field_keeps_the_stored_secret(self):
        new, errors = config_store.apply_form(self.CURRENT, _form(EG_PASSWORD=""))
        assert not errors and new["EG_PASSWORD"] == "hunter2-secret"

    def test_a_new_value_replaces_it(self):
        new, _ = config_store.apply_form(self.CURRENT, _form(EG_PASSWORD="new one"))
        assert new["EG_PASSWORD"] == "new one"

    def test_clearing_removes_it(self):
        new, _ = config_store.apply_form(self.CURRENT, _form(EG_PASSWORD="", EG_PASSWORD__clear="on"))
        assert "EG_PASSWORD" not in new

    def test_a_password_keeps_its_spaces(self):
        new, _ = config_store.apply_form({}, _form(EG_PASSWORD=" padded "))
        assert new["EG_PASSWORD"] == " padded "


class TestValidation:
    @pytest.mark.parametrize("key,value", [
        ("SCHEDULER_TIMEZONE", "Mars/Base"),
        ("SCHEDULER_FIXED_TIMES", "25:00"),
        ("SCHEDULER_FIXED_TIMES", "9:00"),
        ("SCHEDULER_HOURS", "-1"),
        ("SCHEDULER_HOURS", "twelve"),
        ("DISCORD_WEBHOOK", "https://evil.example/api/webhooks/1/x"),
        ("DISCORD_WEBHOOK", "http://discord.com/api/webhooks/1/x"),
        ("EG_OTP_KEY", "not base32 1890"),
        ("EG_EMAIL", "no-at-sign"),
        ("NOTIFY", "just text"),
        ("EG_EMAIL", "a@b.c\nSTORES=x"),
        # dotenv would expand it, even in quotes, and the bot would get another password.
        ("EG_PASSWORD", "pa${HOME}ss"),
    ])
    def test_bad_values_are_refused(self, key, value):
        _, errors = config_store.apply_form({}, _form(**{key: value}))
        assert key in errors

    def test_an_authenticator_key_is_tidied(self):
        new, errors = config_store.apply_form({}, _form(EG_OTP_KEY="abcd efgh 2345"))
        assert not errors and new["EG_OTP_KEY"] == "ABCDEFGH2345"

    def test_at_least_one_store(self):
        _, errors = config_store.apply_form({}, _form(stores=[]))
        assert "STORES" in errors

    def test_unknown_stores_are_ignored(self):
        new, _ = config_store.apply_form({}, _form(stores=["steam", "evil"]))
        assert new["STORES"] == "steam"

    def test_nothing_scheduled_is_refused(self):
        # SCHEDULER_HOURS=0 without fixed times makes the bot run once and exit: a restart loop in Docker.
        _, errors = config_store.apply_form({}, _form(SCHEDULER_HOURS="0", SCHEDULER_FIXED_TIMES=""))
        assert "SCHEDULER_HOURS" in errors

    def test_fixed_times_alone_are_fine(self):
        new, errors = config_store.apply_form({}, _form(SCHEDULER_HOURS="0", SCHEDULER_FIXED_TIMES="06:00, 18:00"))
        assert not errors and new["SCHEDULER_FIXED_TIMES"] == "06:00,18:00"

    def test_an_unticked_checkbox_means_false(self):
        new, _ = config_store.apply_form({"EG_MOBILE": "true"}, _form())
        assert new["EG_MOBILE"] == "false"

    def test_no_mobile_platform_ticked_means_none(self):
        # Removing the key would bring the default (android,ios) back.
        new, _ = config_store.apply_form({}, _form())
        assert new["EG_MOBILE_PLATFORMS"] == ""

    def test_an_empty_text_field_falls_back_to_the_default(self):
        new, _ = config_store.apply_form({"EG_EMAIL": "old@example.org"}, _form(EG_EMAIL=""))
        assert "EG_EMAIL" not in new


class TestLockedByDocker:
    def test_a_key_docker_sets_is_never_changed(self, tmp_path):
        env = tmp_path / "bot.env"
        env.write_text("VNC_PASSWORD=x\nEG_EMAIL=fixed@example.org\n")
        locked = config_store.locked_keys(env)
        assert locked == {"EG_EMAIL"}
        new, _ = config_store.apply_form({}, _form(EG_EMAIL="other@example.org"), locked)
        assert "EG_EMAIL" not in new


class TestStoreSelection:
    def test_empty_means_the_bots_default_list(self):
        assert config_store.selected_stores({}) == list(schema.DEFAULT_STORES)

    def test_aliases_are_understood(self):
        assert config_store.selected_stores({"STORES": "ae, ms,steam"}) == ["steam", "microsoft", "aliexpress"]

    def test_default_list_matches_main(self):
        import main
        assert list(schema.DEFAULT_STORES) == main.DEFAULT_STORES
        assert set(schema.STORE_KEYS) == set(main.ALL_CLAIMERS) | set(main.SIDE_STORES) | set(main.REDEEM_STORES)
