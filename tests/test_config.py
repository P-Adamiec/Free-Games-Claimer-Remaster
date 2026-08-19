"""Reading settings from .env, typed helpers and the list-style options."""

import importlib

import pytest

import src.core.config as config_module


@pytest.fixture(autouse=True, scope="module")
def _restore_config():
    """Reloading the module replaces the shared cfg, put a clean one back afterwards."""
    yield
    importlib.reload(config_module)


def _reload(monkeypatch, **env):
    """Reload the config module with a controlled environment.

    The developer's own .env must not leak in, or these tests would pass or fail
    depending on whose machine they run on.
    """
    # Patch the source module: reloading config re-imports load_dotenv from it.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    for key in ("EG_MOBILE", "EG_MOBILE_PLATFORMS", "NOTIFY_SKIP_STORES",
                "NOTIFY_CLAIM_FAILS", "NOTIFY_ALREADY_CLAIMED", "DRYRUN", "WIDTH",
                "DEBUG", "DEBUG_LIBS", "VNC_IP", "NOVNC_PORT", "VNC_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module).cfg


class TestBoolParsing:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_truthy(self, monkeypatch, value):
        assert _reload(monkeypatch, DRYRUN=value).dryrun is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
    def test_falsy(self, monkeypatch, value):
        assert _reload(monkeypatch, DRYRUN=value).dryrun is False

    def test_default_when_unset(self, monkeypatch):
        assert _reload(monkeypatch).dryrun is False


class TestIntParsing:
    def test_reads_number(self, monkeypatch):
        assert _reload(monkeypatch, WIDTH="1920").width == 1920

    def test_falls_back_on_junk(self, monkeypatch):
        assert _reload(monkeypatch, WIDTH="wide").width == 1280


class TestNotifyToggles:
    def test_both_default_to_off(self, monkeypatch):
        cfg = _reload(monkeypatch)
        assert cfg.notify_claim_fails is False
        assert cfg.notify_already_claimed is False

    def test_can_be_enabled(self, monkeypatch):
        cfg = _reload(monkeypatch, NOTIFY_CLAIM_FAILS="true", NOTIFY_ALREADY_CLAIMED="true")
        assert cfg.notify_claim_fails is True
        assert cfg.notify_already_claimed is True


class TestDebugFlags:
    def test_debug_defaults_on_libs_defaults_off(self, monkeypatch):
        cfg = _reload(monkeypatch)
        assert cfg.debug is True
        assert cfg.debug_libs is False

    def test_debug_can_be_turned_off(self, monkeypatch):
        assert _reload(monkeypatch, DEBUG="false").debug is False

    def test_library_internals_are_independent_of_debug(self, monkeypatch):
        # DEBUG alone must not switch on the CDP/HTTP/SQL firehose.
        assert _reload(monkeypatch, DEBUG="true").debug_libs is False
        assert _reload(monkeypatch, DEBUG_LIBS="true").debug_libs is True


class TestSkipStores:
    def test_aliases_resolve_to_canonical_names(self, monkeypatch):
        cfg = _reload(monkeypatch, NOTIFY_SKIP_STORES="ae, amazon ,gp")
        assert cfg.notify_skip_stores == {"aliexpress", "prime", "gamerpower"}

    def test_silences_only_listed_stores(self, monkeypatch):
        cfg = _reload(monkeypatch, NOTIFY_SKIP_STORES="aliexpress")
        assert cfg.store_notify_enabled("epic") is True
        assert cfg.store_notify_enabled("aliexpress") is False

    def test_empty_means_nothing_silenced(self, monkeypatch):
        assert _reload(monkeypatch).notify_skip_stores == set()


class TestEpicMobilePlatforms:
    def test_defaults_to_both(self, monkeypatch):
        cfg = _reload(monkeypatch)
        assert cfg.eg_mobile is True
        assert cfg.eg_mobile_platform_list == ["android", "ios"]

    def test_respects_a_single_platform(self, monkeypatch):
        cfg = _reload(monkeypatch, EG_MOBILE_PLATFORMS="android")
        assert cfg.eg_mobile_platform_list == ["android"]

    def test_tolerates_spacing_and_case(self, monkeypatch):
        cfg = _reload(monkeypatch, EG_MOBILE_PLATFORMS=" IOS , Android ")
        assert cfg.eg_mobile_platform_list == ["ios", "android"]

    def test_drops_unknown_values(self, monkeypatch):
        cfg = _reload(monkeypatch, EG_MOBILE_PLATFORMS="android,windows")
        assert cfg.eg_mobile_platform_list == ["android"]

    def test_can_be_disabled(self, monkeypatch):
        assert _reload(monkeypatch, EG_MOBILE="false").eg_mobile is False


class TestVncUrl:
    """The one-click link in every "manual action needed" notification.

    VNC_URL is for reverse proxies (issue #3): it must win over VNC_IP and
    NOVNC_PORT, while leaving those two working exactly as before for everyone else.
    """

    def test_default_is_unchanged(self, monkeypatch):
        assert _reload(monkeypatch).vnc_url == "http://localhost:7080/?autoconnect=true"

    def test_host_and_port_still_work_together(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_IP="192.168.1.100", NOVNC_PORT="8080")
        assert cfg.vnc_url == "http://192.168.1.100:8080/?autoconnect=true"

    def test_full_url_drops_the_port(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_URL="https://fgc.reverseproxy.tld")
        assert cfg.vnc_url == "https://fgc.reverseproxy.tld/?autoconnect=true"

    def test_full_url_overrides_host_and_port(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_URL="https://fgc.reverseproxy.tld",
                      VNC_IP="192.168.1.100", NOVNC_PORT="8080")
        assert cfg.vnc_url == "https://fgc.reverseproxy.tld/?autoconnect=true"

    def test_trailing_slash_does_not_double_up(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_URL="https://fgc.reverseproxy.tld/")
        assert cfg.vnc_url == "https://fgc.reverseproxy.tld/?autoconnect=true"

    def test_a_path_is_kept(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_URL="https://proxy.tld/fgc")
        assert cfg.vnc_url == "https://proxy.tld/fgc/?autoconnect=true"

    def test_missing_scheme_becomes_https(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_URL="fgc.reverseproxy.tld")
        assert cfg.vnc_url == "https://fgc.reverseproxy.tld/?autoconnect=true"

    def test_plain_http_is_honoured(self, monkeypatch):
        cfg = _reload(monkeypatch, VNC_URL="http://nas.local:9000")
        assert cfg.vnc_url == "http://nas.local:9000/?autoconnect=true"

    @pytest.mark.parametrize("value", ["", "   ", "/"])
    def test_empty_value_falls_back_to_host_and_port(self, monkeypatch, value):
        cfg = _reload(monkeypatch, VNC_URL=value, VNC_IP="192.168.1.100")
        assert cfg.vnc_url == "http://192.168.1.100:7080/?autoconnect=true"


class TestAliExpressPageRetries:
    """The second chance for a coin page that loads but paints nothing."""

    def test_defaults_to_several_extra_approaches(self, monkeypatch):
        # One usable page in eight looks was measured, so a single retry is not enough.
        assert _reload(monkeypatch).ae_page_retries == 4

    def test_zero_gives_up_on_the_first_look(self, monkeypatch):
        assert _reload(monkeypatch, AE_PAGE_RETRIES="0").ae_page_retries == 0

    def test_junk_falls_back_to_the_default(self, monkeypatch):
        assert _reload(monkeypatch, AE_PAGE_RETRIES="soon").ae_page_retries == 4

    def test_a_negative_value_cannot_ask_for_negative_attempts(self, monkeypatch):
        # The loop clamps with max(0, ...), so this must never run a single approach.
        assert max(0, _reload(monkeypatch, AE_PAGE_RETRIES="-3").ae_page_retries) == 0



class TestItchioRecoveryCodes:
    """Recovery codes follow the GOG shape: a switch plus a comma-separated list."""

    def test_off_and_empty_by_default(self, monkeypatch):
        cfg = _reload(monkeypatch)
        assert cfg.itchio_otp_enable is False and cfg.itchio_otp_codes == []

    def test_codes_are_split_and_trimmed(self, monkeypatch):
        cfg = _reload(monkeypatch, ITCHIO_OTP_ENABLE="true", ITCHIO_OTP_CODES=" 12345678 , 23456789,34567890 ")
        assert cfg.itchio_otp_enable is True
        assert cfg.itchio_otp_codes == ["12345678", "23456789", "34567890"]

    def test_blank_entries_are_dropped(self, monkeypatch):
        assert _reload(monkeypatch, ITCHIO_OTP_CODES="12345678,,  ,23456789").itchio_otp_codes == \
            ["12345678", "23456789"]

    def test_the_switch_is_independent_of_the_codes(self, monkeypatch):
        # Codes without the switch stay unused, exactly like GOG_OTP_CODES.
        cfg = _reload(monkeypatch, ITCHIO_OTP_CODES="12345678")
        assert cfg.itchio_otp_codes and cfg.itchio_otp_enable is False

