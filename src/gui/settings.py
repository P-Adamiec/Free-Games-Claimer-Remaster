"""Validated, privacy-safe settings exposed by the local dashboard."""

from __future__ import annotations

import os
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
    label: str
    section: str
    kind: str = "text"
    secret: bool = False
    minimum: int | None = None
    maximum: int | None = None
    help: str = ""
    restart: bool = False

    def public(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "section": self.section,
            "kind": self.kind,
            "secret": self.secret,
            "min": self.minimum,
            "max": self.maximum,
            "help": self.help,
            "restart": self.restart,
        }


SPECS = (
    SettingSpec("STORES", "stores", "Lojas habilitadas", "Lojas", "stores"),
    SettingSpec("SCHEDULER_HOURS", "scheduler_hours", "Intervalo em horas", "Agendamento", "integer", minimum=0, maximum=720),
    SettingSpec("SCHEDULER_FIXED_TIMES", "scheduler_fixed_times", "Horários fixos", "Agendamento", help="Ex.: 07:30,12:00,21:00"),
    SettingSpec("SCHEDULER_TIMEZONE", "scheduler_timezone", "Fuso horário", "Agendamento", help="Ex.: America/Sao_Paulo"),
    SettingSpec("RUN_ON_STARTUP", "run_on_startup", "Executar ao iniciar", "Agendamento", "boolean"),
    SettingSpec("SHOW", "show", "Exibir navegador no VNC", "Navegador", "boolean"),
    SettingSpec("VNC_LOGIN_TIMEOUT", "vnc_login_timeout", "Espera para login manual (s)", "Navegador", "integer", minimum=30, maximum=3600),
    SettingSpec("NOTIFY_SUMMARY", "notify_summary", "Enviar resumo", "Notificações", "boolean"),
    SettingSpec("NOTIFY_ERRORS", "notify_errors", "Notificar erros", "Notificações", "boolean"),
    SettingSpec("NOTIFY_CLAIM_FAILS", "notify_claim_fails", "Notificar falhas", "Notificações", "boolean"),
    SettingSpec("NOTIFY_LOGIN_REQUEST", "notify_login_request", "Avisar quando precisar de login", "Notificações", "boolean"),
    SettingSpec("DISCORD_WEBHOOK", "discord_webhook", "Webhook do Discord", "Notificações", "password", True),
    SettingSpec("NOTIFY", "notify_url", "URL do Apprise", "Notificações", "password", True),
    SettingSpec("EG_EMAIL", "eg_email", "E-mail Epic Games", "Epic Games", "password", True),
    SettingSpec("EG_PASSWORD", "eg_password", "Senha Epic Games", "Epic Games", "password", True),
    SettingSpec("EG_OTPKEY", "eg_otpkey", "Chave TOTP Epic Games", "Epic Games", "password", True),
    SettingSpec("EG_MOBILE", "eg_mobile", "Coletar jogos mobile", "Epic Games", "boolean"),
    SettingSpec("PG_EMAIL", "pg_email", "E-mail Prime Gaming", "Prime Gaming", "password", True),
    SettingSpec("PG_PASSWORD", "pg_password", "Senha Prime Gaming", "Prime Gaming", "password", True),
    SettingSpec("PG_OTPKEY", "pg_otpkey", "Chave TOTP Prime Gaming", "Prime Gaming", "password", True),
    SettingSpec("PG_REDEEM", "pg_redeem", "Resgatar códigos externos", "Prime Gaming", "boolean"),
    SettingSpec("GOG_EMAIL", "gog_email", "E-mail GOG", "GOG", "password", True),
    SettingSpec("GOG_PASSWORD", "gog_password", "Senha GOG", "GOG", "password", True),
    SettingSpec("GOG_NEWSLETTER", "gog_newsletter", "Manter newsletter", "GOG", "boolean"),
    SettingSpec("STEAM_USERNAME", "steam_username", "Usuário Steam", "Steam", "password", True),
    SettingSpec("STEAM_PASSWORD", "steam_password", "Senha Steam", "Steam", "password", True),
    SettingSpec("UBI_EMAIL", "ubi_email", "E-mail Ubisoft", "Ubisoft", "password", True),
    SettingSpec("UBI_PASSWORD", "ubi_password", "Senha Ubisoft", "Ubisoft", "password", True),
    SettingSpec("UBI_OTPKEY", "ubi_otpkey", "Chave TOTP Ubisoft", "Ubisoft", "password", True),
    SettingSpec("UNITY_EMAIL", "unity_email", "E-mail Unity", "Unity", "password", True),
    SettingSpec("UNITY_PASSWORD", "unity_password", "Senha Unity", "Unity", "password", True),
    SettingSpec("UNITY_ACCEPT_TOS", "unity_accept_tos", "Aceitar termos automaticamente", "Unity", "boolean"),
    SettingSpec("AE_EMAIL", "ae_email", "E-mail AliExpress", "AliExpress", "password", True),
    SettingSpec("AE_PASSWORD", "ae_password", "Senha AliExpress", "AliExpress", "password", True),
    SettingSpec("AE_MIN_COINS", "ae_min_coins", "Mínimo de moedas", "AliExpress", "integer", minimum=1, maximum=10000),
    SettingSpec("AE_FLAG_RETRIES", "ae_flag_retries", "Tentativas após bloqueio", "AliExpress", "integer", minimum=0, maximum=10),
    SettingSpec("AE_FLAG_WAIT", "ae_flag_wait", "Espera após bloqueio (s)", "AliExpress", "integer", minimum=60, maximum=3600),
    SettingSpec("FAB_ACCEPT_EULA", "fab_accept_eula", "Aceitar licença automaticamente", "Fab", "boolean"),
    SettingSpec("FANATICAL_ENABLE", "fanatical_enable", "Ativar Fanatical", "GamerPower", "boolean"),
    SettingSpec("ALIENWARE_ENABLE", "alienware_enable", "Ativar Alienware", "GamerPower", "boolean"),
    SettingSpec("ITCHIO_ENABLE", "itchio_enable", "Ativar itch.io", "GamerPower", "boolean"),
    SettingSpec("INDIEGALA_ENABLE", "indiegala_enable", "Ativar IndieGala", "GamerPower", "boolean"),
)

_BY_KEY = {spec.key: spec for spec in SPECS}


class SettingsError(ValueError):
    """Raised when the dashboard receives an invalid setting."""


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise SettingsError("valor booleano inválido")


def _normalise(spec: SettingSpec, value):
    if spec.kind == "boolean":
        return _as_bool(value)
    if spec.kind == "integer":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{spec.label}: informe um número inteiro") from exc
        if spec.minimum is not None and number < spec.minimum:
            raise SettingsError(f"{spec.label}: mínimo {spec.minimum}")
        if spec.maximum is not None and number > spec.maximum:
            raise SettingsError(f"{spec.label}: máximo {spec.maximum}")
        return number
    if spec.kind == "stores":
        raw = value if isinstance(value, list) else str(value or "").split(",")
        stores = [str(item).strip().lower() for item in raw if str(item).strip()]
        if not stores:
            raise SettingsError("Selecione pelo menos uma loja")
        invalid = sorted(set(stores) - set(STORE_KEYS))
        if invalid:
            raise SettingsError(f"Lojas desconhecidas: {', '.join(invalid)}")
        return list(dict.fromkeys(stores))

    text = str(value or "").strip()
    if any(char in text for char in "\r\n\x00"):
        raise SettingsError(f"{spec.label}: caracteres inválidos")
    if len(text) > 2048:
        raise SettingsError(f"{spec.label}: valor muito longo")
    if spec.key == "SCHEDULER_TIMEZONE":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError("Fuso horário inválido") from exc
    if spec.key == "SCHEDULER_FIXED_TIMES" and text:
        for item in text.split(","):
            parts = item.strip().split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise SettingsError("Use horários no formato HH:MM separados por vírgula")
            hour, minute = map(int, parts)
            if hour > 23 or minute > 59:
                raise SettingsError("Horário fora do intervalo válido")
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
    }


def save_settings(values: dict, path: Path | None = None) -> dict:
    """Validate, persist and apply an allow-listed set of settings."""
    if not isinstance(values, dict):
        raise SettingsError("Configuração inválida")
    target = path or (cfg._data_dir / "gui.env")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)

    changed = []
    restart_required = []
    for key, raw_value in values.items():
        spec = _BY_KEY.get(str(key))
        if spec is None:
            raise SettingsError(f"Configuração não permitida: {key}")
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
