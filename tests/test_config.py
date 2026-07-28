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
                "DEBUG", "DEBUG_LIBS"):
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
