"""Settings that quietly do nothing (issue #40).

A user set STEAM_ENABLE=false, which is not a setting, and the bot said nothing.
The same silence hides DRYRUN=maybe, which _bool() reads as false.
"""

import pytest

from src.core import config as C


class TestEnvFileParsing:
    """Only names and values that are actually set count, and the value is never split wrong."""

    def _settings(self, tmp_path, monkeypatch, text):
        env = tmp_path / ".env"
        env.write_text(text, encoding="utf-8")
        monkeypatch.setattr(C, "_env_root", env)
        monkeypatch.setattr(C, "_env_data", tmp_path / "not-there.env")
        return C.env_file_settings()

    def test_it_reads_a_plain_setting(self, tmp_path, monkeypatch):
        assert self._settings(tmp_path, monkeypatch, "STORES=steam,epic\n") == {"STORES": "steam,epic"}

    def test_spaces_around_the_equals_sign(self, tmp_path, monkeypatch):
        # This is how the reporter wrote it: STEAM_ENABLE = false
        got = self._settings(tmp_path, monkeypatch, "STEAM_ENABLE = false\n")
        assert got == {"STEAM_ENABLE": "false"}

    def test_an_export_prefix(self, tmp_path, monkeypatch):
        assert self._settings(tmp_path, monkeypatch, "export DEBUG=true\n") == {"DEBUG": "true"}

    def test_a_commented_line_sets_nothing(self, tmp_path, monkeypatch):
        assert self._settings(tmp_path, monkeypatch, "# STORES=steam\n#EMAIL=a@b.c\n") == {}

    def test_a_value_with_an_equals_sign_stays_one_setting(self, tmp_path, monkeypatch):
        got = self._settings(tmp_path, monkeypatch, "NOTIFY=tgram://a=b\n")
        assert got == {"NOTIFY": "tgram://a=b"}

    def test_a_byte_order_mark_does_not_glue_itself_to_the_first_name(self, tmp_path, monkeypatch):
        # An editor saving .env as UTF-8 with BOM; python-dotenv fixed the same trap in 1.2.3.
        assert self._settings(tmp_path, monkeypatch, '\ufeffSTORES=steam\n') == {"STORES": "steam"}

    def test_quotes_are_stripped(self, tmp_path, monkeypatch):
        assert self._settings(tmp_path, monkeypatch, 'STORES="steam"\n') == {"STORES": "steam"}


class TestKnownSettings:
    def test_the_scan_still_finds_settings(self):
        # Guards the regex: a rename in config.py must not empty this map.
        assert len(C.env_setting_kinds()) > 60

    def test_it_knows_what_kind_each_setting_is(self):
        kinds = C.env_setting_kinds()
        assert kinds["DRYRUN"] == "bool"
        assert kinds["WIDTH"] == "int"
        assert kinds["STORES"] == "str"

    def test_docker_only_settings_count_as_known(self):
        # FGC_TAG is read by Docker, VNC_PASSWORD by start-vnc.sh and, only whether it is set, by config.py.
        assert {"FGC_TAG", "VNC_PASSWORD", "STORES"} <= C.known_env_names()


class TestMasking:
    @pytest.mark.parametrize("name,value", [
        ("EG_PASSWORD", "hunter2"),
        ("EMAIL", "someone@example.com"),
        ("NOTIFY", "tgram://token/chat"),
        ("DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1/2"),
        ("EG_OTPKEY", "JBSWY3DPEHPK3PXP"),
        ("EG_PARENTALPIN", "1234"),
        ("STEAM_USERNAME", "pawel"),
        ("MY_OWN_TOKEN", "abcdef"),
    ])
    def test_a_secret_never_comes_back(self, name, value):
        assert C.mask_value(name, value) == "***"

    @pytest.mark.parametrize("name,value", [
        ("STORES", "steam,epic"),
        ("NOTIFY_SUMMARY", "maybe"),       # not NOTIFY, so the value still helps
        ("SCHEDULER_HOURS", "twelve"),
    ])
    def test_a_harmless_value_is_shown(self, name, value):
        assert C.mask_value(name, value) == value


class TestSettingsWarnings:
    @pytest.fixture(autouse=True)
    def _no_retired_switch_from_a_developers_env(self, monkeypatch):
        # A local .env is loaded into the environment on import and would add a warning to every test.
        for name in C._RETIRED_SWITCHES:
            monkeypatch.delenv(name, raising=False)

    def _warn(self, monkeypatch, settings):
        monkeypatch.setattr(C, "env_file_settings", lambda: settings)
        return C.settings_warnings()

    def test_an_invented_setting_is_named(self, monkeypatch):
        warnings = self._warn(monkeypatch, {"STEAM_ENABLE": "false"})
        assert len(warnings) == 1 and warnings[0].startswith("STEAM_ENABLE ")

    @pytest.mark.parametrize("name", ["FANATICAL_ENABLE", "ITCHIO_ENABLE", "INDIEGALA_ENABLE", "ALIENWARE_ENABLE"])
    def test_the_retired_side_store_switches_are_named(self, monkeypatch, name):
        # Kept for one release after 1.9 and gone in 1.11: whoever still has one hears it does nothing.
        warnings = self._warn(monkeypatch, {name: "true"})
        assert len(warnings) == 1 and "is not a setting this bot reads" in warnings[0]

    def test_timeout_is_gone_and_named(self, monkeypatch):
        # Only a helper no store called read it, so it never changed a thing (removed in 1.11).
        assert not hasattr(C.cfg, "timeout")
        warnings = self._warn(monkeypatch, {"TIMEOUT": "60"})
        assert warnings == ["TIMEOUT is not a setting this bot reads, so it does nothing."]

    def test_a_retired_switch_passed_as_a_variable_is_named_too(self, monkeypatch):
        # docker compose hands .env over as variables, so no file inside the container ever lists it.
        monkeypatch.setenv("FANATICAL_ENABLE", "true")
        warnings = self._warn(monkeypatch, {})
        assert warnings == ["FANATICAL_ENABLE is not a setting this bot reads, so it does nothing."]

    def test_it_is_named_once_when_both_places_have_it(self, monkeypatch):
        monkeypatch.setenv("FANATICAL_ENABLE", "true")
        assert len(self._warn(monkeypatch, {"FANATICAL_ENABLE": "true"})) == 1

    @pytest.mark.parametrize("settings", [
        {"STORES": "steam,epic"},
        {"DEBUG": "true"},
        {"DEBUG": "0"},
        {"DRYRUN": ""},
        {"SCHEDULER_HOURS": "12"},
    ])
    def test_a_setting_that_works_is_quiet(self, monkeypatch, settings):
        assert self._warn(monkeypatch, settings) == []

    def test_a_yes_no_setting_holding_something_else(self, monkeypatch):
        warnings = self._warn(monkeypatch, {"DRYRUN": "maybe"})
        assert warnings and "DRYRUN=maybe" in warnings[0] and "false" in warnings[0]

    def test_a_number_setting_holding_words(self, monkeypatch):
        warnings = self._warn(monkeypatch, {"SCHEDULER_HOURS": "twelve"})
        assert warnings and "SCHEDULER_HOURS=twelve" in warnings[0]

    def test_no_secret_value_reaches_the_warning(self, monkeypatch):
        blob = " ".join(self._warn(monkeypatch, {
            "EG_PASSWORD": "hunter2",
            "EMAIL": "someone@example.com",
            "NOTIFY": "tgram://token/chat",
            "MY_OWN_PASSWORD": "hunter3",
            "DRYRUN": "maybe",
        }))
        for secret in ("hunter2", "hunter3", "someone@example.com", "tgram://token/chat"):
            assert secret not in blob
        assert "DRYRUN=maybe" in blob
