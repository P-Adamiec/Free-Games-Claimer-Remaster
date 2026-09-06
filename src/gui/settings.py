"""Validated, privacy-safe settings exposed by the local dashboard."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import set_key

from src.core.config import cfg


STORE_KEYS = ("steam", "epic", "fab", "prime", "gog", "ubisoft", "unity", "gamerpower", "aliexpress")


@dataclass(frozen=True)
class SettingSpec:
    key: str
    attr: str
    label_key: str
    section_key: str
    kind: str = "text"
    secret: bool = False
    minimum: int | None = None
    maximum: int | None = None
    help_key: str = ""
    credential_purpose_key: str = ""
    restart: bool = False

    def public(self) -> dict:
        return {
            "key": self.key,
            "labelKey": self.label_key,
            "sectionKey": self.section_key,
            "kind": self.kind,
            "secret": self.secret,
            "min": self.minimum,
            "max": self.maximum,
            "helpKey": self.help_key,
            "credentialPurposeKey": self.credential_purpose_key,
            "restart": self.restart,
        }


def _setting(
    key: str,
    attr: str,
    section: str,
    kind: str = "text",
    *,
    secret: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
    help_key: str = "",
    credential: bool = False,
) -> SettingSpec:
    """Create a schema entry whose user-facing copy lives in locale files."""
    return SettingSpec(
        key,
        attr,
        f"settings.{key}.label",
        f"section.{section}",
        kind,
        secret,
        minimum,
        maximum,
        help_key,
        f"credentials.{section}.purpose" if credential else "",
    )


SPECS = (
    _setting("STORES", "stores", "stores", "stores"),
    _setting("SCHEDULER_HOURS", "scheduler_hours", "schedule", "integer", minimum=0, maximum=720),
    _setting("SCHEDULER_FIXED_TIMES", "scheduler_fixed_times", "schedule", help_key="help.fixedTimes"),
    _setting("SCHEDULER_TIMEZONE", "scheduler_timezone", "schedule", help_key="help.timezone"),
    _setting("RUN_ON_STARTUP", "run_on_startup", "schedule", "boolean"),
    _setting("SHOW", "show", "browser", "boolean"),
    _setting("VNC_LOGIN_TIMEOUT", "vnc_login_timeout", "browser", "integer", minimum=30, maximum=3600),
    _setting("NOTIFY_SUMMARY", "notify_summary", "notifications", "boolean"),
    _setting("NOTIFY_ERRORS", "notify_errors", "notifications", "boolean"),
    _setting("NOTIFY_CLAIM_FAILS", "notify_claim_fails", "notifications", "boolean"),
    _setting("NOTIFY_LOGIN_REQUEST", "notify_login_request", "notifications", "boolean"),
    _setting("DISCORD_WEBHOOK", "discord_webhook", "notifications", "password", secret=True),
    _setting("NOTIFY", "notify_url", "notifications", "password", secret=True),
    _setting("EG_EMAIL", "eg_email", "epic", "password", secret=True, credential=True),
    _setting("EG_PASSWORD", "eg_password", "epic", "password", secret=True, credential=True),
    _setting("EG_OTPKEY", "eg_otpkey", "epic", "password", secret=True, credential=True),
    _setting("EG_MOBILE", "eg_mobile", "epic", "boolean"),
    _setting("PG_EMAIL", "pg_email", "prime", "password", secret=True, credential=True),
    _setting("PG_PASSWORD", "pg_password", "prime", "password", secret=True, credential=True),
    _setting("PG_OTPKEY", "pg_otpkey", "prime", "password", secret=True, credential=True),
    _setting("PG_REDEEM", "pg_redeem", "prime", "boolean"),
    _setting("GOG_EMAIL", "gog_email", "gog", "password", secret=True, credential=True),
    _setting("GOG_PASSWORD", "gog_password", "gog", "password", secret=True, credential=True),
    _setting("GOG_NEWSLETTER", "gog_newsletter", "gog", "boolean"),
    _setting("STEAM_USERNAME", "steam_username", "steam", "password", secret=True, credential=True),
    _setting("STEAM_PASSWORD", "steam_password", "steam", "password", secret=True, credential=True),
    _setting("UBI_EMAIL", "ubi_email", "ubisoft", "password", secret=True, credential=True),
    _setting("UBI_PASSWORD", "ubi_password", "ubisoft", "password", secret=True, credential=True),
    _setting("UBI_OTPKEY", "ubi_otpkey", "ubisoft", "password", secret=True, credential=True),
    _setting("UNITY_EMAIL", "unity_email", "unity", "password", secret=True, credential=True),
    _setting("UNITY_PASSWORD", "unity_password", "unity", "password", secret=True, credential=True),
    _setting("UNITY_ACCEPT_TOS", "unity_accept_tos", "unity", "boolean"),
    _setting("AE_EMAIL", "ae_email", "aliexpress", "password", secret=True, credential=True),
    _setting("AE_PASSWORD", "ae_password", "aliexpress", "password", secret=True, credential=True),
    _setting("AE_MIN_COINS", "ae_min_coins", "aliexpress", "integer", minimum=1, maximum=10000),
    _setting("AE_FLAG_RETRIES", "ae_flag_retries", "aliexpress", "integer", minimum=0, maximum=10),
    _setting("AE_FLAG_WAIT", "ae_flag_wait", "aliexpress", "integer", minimum=60, maximum=3600),
    _setting("FAB_ACCEPT_EULA", "fab_accept_eula", "fab", "boolean"),
    _setting("FANATICAL_ENABLE", "fanatical_enable", "gamerpower", "boolean"),
    _setting("FANATICAL_EMAIL", "fanatical_email", "fanatical", "password", secret=True, credential=True),
    _setting("FANATICAL_PASSWORD", "fanatical_password", "fanatical", "password", secret=True, credential=True),
    _setting("ALIENWARE_ENABLE", "alienware_enable", "gamerpower", "boolean"),
    _setting("ITCHIO_ENABLE", "itchio_enable", "gamerpower", "boolean"),
    _setting("ITCHIO_EMAIL", "itchio_email", "itchio", "password", secret=True, credential=True),
    _setting("ITCHIO_PASSWORD", "itchio_password", "itchio", "password", secret=True, credential=True),
    _setting("INDIEGALA_ENABLE", "indiegala_enable", "gamerpower", "boolean"),
    _setting("INDIEGALA_EMAIL", "indiegala_email", "indiegala", "password", secret=True, credential=True),
    _setting("INDIEGALA_PASSWORD", "indiegala_password", "indiegala", "password", secret=True, credential=True),
)

_BY_KEY = {spec.key: spec for spec in SPECS}


class SettingsError(ValueError):
    """Raised when the dashboard receives an invalid setting."""

    def __init__(self, message: str, code: str = "error.invalidSettings") -> None:
        super().__init__(message)
        self.code = code


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise SettingsError("Invalid boolean value")


def _normalise(spec: SettingSpec, value):
    if spec.kind == "boolean":
        return _as_bool(value)
    if spec.kind == "integer":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{spec.key}: expected an integer", "error.integer") from exc
        if spec.minimum is not None and number < spec.minimum:
            raise SettingsError(f"{spec.key}: minimum {spec.minimum}", "error.minimum")
        if spec.maximum is not None and number > spec.maximum:
            raise SettingsError(f"{spec.key}: maximum {spec.maximum}", "error.maximum")
        return number
    if spec.kind == "stores":
        raw = value if isinstance(value, list) else str(value or "").split(",")
        stores = [str(item).strip().lower() for item in raw if str(item).strip()]
        if not stores:
            raise SettingsError("Select at least one store", "error.selectStore")
        invalid = sorted(set(stores) - set(STORE_KEYS))
        if invalid:
            raise SettingsError(f"Unknown stores: {', '.join(invalid)}", "error.unknownStore")
        return list(dict.fromkeys(stores))

    text = str(value or "").strip()
    if any(char in text for char in "\r\n\x00"):
        raise SettingsError(f"{spec.key}: invalid characters", "error.invalidCharacters")
    if len(text) > 2048:
        raise SettingsError(f"{spec.key}: value is too long", "error.tooLong")
    if spec.key == "SCHEDULER_TIMEZONE":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError("Invalid timezone", "error.timezone") from exc
    if spec.key == "SCHEDULER_FIXED_TIMES" and text:
        for item in text.split(","):
            parts = item.strip().split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise SettingsError("Use comma-separated HH:MM times", "error.fixedTimes")
            hour, minute = map(int, parts)
            if hour > 23 or minute > 59:
                raise SettingsError("Time is outside the valid range", "error.fixedTimesRange")
        text = ",".join(item.strip() for item in text.split(","))
    return text


def _serialise(spec: SettingSpec, value) -> str:
    if spec.kind == "boolean":
        return "true" if value else "false"
    if spec.kind == "stores":
        return ",".join(value)
    return str(value)


def _runtime_value(spec: SettingSpec, value):
    if spec.key == "NOTIFY_SKIP_STORES":
        return set(value)
    if spec.kind == "stores":
        return ",".join(value)
    return value


def get_settings(default_stores: list[str]) -> dict:
    """Return dashboard settings without ever returning secret values."""
    values = {}
    configured = {}
    for spec in SPECS:
        current = getattr(cfg, spec.attr, None)
        if spec.secret:
            configured[spec.key] = bool(current)
            continue
        if spec.kind == "stores":
            values[spec.key] = [part.strip() for part in (current or "").split(",") if part.strip()] or default_stores
        else:
            values[spec.key] = current
    return {
        "schema": [spec.public() for spec in SPECS],
        "values": values,
        "configured": configured,
        "setup": get_setup_state(),
    }


def save_settings(values: dict, path: Path | None = None) -> dict:
    """Validate, persist and apply an allow-listed set of settings."""
    if not isinstance(values, dict):
        raise SettingsError("Invalid settings payload")
    target = path or (cfg._data_dir / "gui.env")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)

    changed = []
    restart_required = []
    for key, raw_value in values.items():
        spec = _BY_KEY.get(str(key))
        if spec is None:
            raise SettingsError(f"Setting not allowed: {key}", "error.settingNotAllowed")
        # Blank secret fields mean "keep the existing value".
        if spec.secret and (raw_value is None or str(raw_value).strip() == ""):
            continue
        value = _normalise(spec, raw_value)
        serialised = _serialise(spec, value)
        set_key(str(target), spec.key, serialised, quote_mode="always")
        try:
            target.chmod(0o600)
        except OSError:
            pass
        os.environ[spec.key] = serialised
        setattr(cfg, spec.attr, _runtime_value(spec, value))
        changed.append(spec.key)
        if spec.restart:
            restart_required.append(spec.key)

    return {"changed": changed, "restartRequired": restart_required}


def _setup_path(path: Path | None = None) -> Path:
    return path or (cfg._data_dir / "setup.json")


def get_setup_state(path: Path | None = None) -> dict:
    """Return setup state without exposing anything from the saved configuration."""
    required = bool(cfg.gui_setup_required)
    complete = not required or _setup_path(path).is_file()
    return {"required": required, "complete": complete}


def complete_setup(values: dict, *, settings_path: Path | None = None, marker_path: Path | None = None) -> dict:
    """Save allow-listed settings and atomically mark local onboarding complete."""
    result = save_settings(values, settings_path)
    marker = _setup_path(marker_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"complete": True}), encoding="utf-8")
    temporary.replace(marker)
    try:
        marker.chmod(0o600)
    except OSError:
        pass
    return {**result, "setup": {"required": bool(cfg.gui_setup_required), "complete": True}}
