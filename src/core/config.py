"""Application configuration – reads your settings from the .env file.

This file is responsible for loading all the settings you define in your .env file
(like email addresses, passwords, Discord webhooks, etc.) and making them available
to the rest of the application as simple Python variables.

If a variable is not set, sensible defaults are used (e.g. screen size 1280x720).
Store-specific credentials (like EG_EMAIL) take priority over default ones (EMAIL).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env files (project root first, then data/config.env as fallback)
# Docker passes env vars directly, so override=False means real env vars win.
_root = Path(__file__).resolve().parent.parent
_env_root = _root / ".env"
_env_data = _root / "data" / "config.env"

load_dotenv(_env_root, override=False)
load_dotenv(_env_data, override=False)


def _bool(key: str, default: bool = False) -> bool:
    """Read an env var as a boolean (truthy: '1', 'true', 'yes')."""
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


def _int(key: str, default: int = 0) -> int:
    """Read an env var as an integer."""
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


class Config:
    """All application settings in one place.
    
    Every setting here corresponds to an environment variable in your .env file.
    For example, 'eg_email' reads from the EG_EMAIL variable.
    """

    # --- General ---
    debug: bool = _bool("DEBUG")
    dryrun: bool = _bool("DRYRUN")
    show: bool = _bool("SHOW", default=True)
    width: int = _int("WIDTH", 1280)
    height: int = _int("HEIGHT", 720)
    timeout: int = _int("TIMEOUT", 60) * 1000          # ms
    login_timeout: int = _int("LOGIN_TIMEOUT", 180) * 1000  # ms
    vnc_login_timeout: int = _int("VNC_LOGIN_TIMEOUT", 180) # seconds
    novnc_port: str | None = os.getenv("NOVNC_PORT")
    scheduler_hours: int = _int("SCHEDULER_HOURS", 12)

    # --- Directories ---
    # _data_dir must resolve to /fgc/data (the Docker volume mount),
    # NOT /fgc/src/data.  config.py lives at /fgc/src/core/config.py,
    # so project root is .parent.parent.parent → /fgc.
    _data_dir: Path = Path(__file__).resolve().parent.parent.parent / "data"
    browser_dir: Path = Path(os.getenv("BROWSER_DIR") or "") if os.getenv("BROWSER_DIR") else _data_dir / "browser"
    screenshots_dir: Path = Path(os.getenv("SCREENSHOTS_DIR") or "") if os.getenv("SCREENSHOTS_DIR") else _data_dir / "screenshots"

    # --- Database ---
    database_url: str = f"sqlite+aiosqlite:///{_data_dir}/fgc.db"

    # --- Notifications ---
    discord_webhook: str | None = os.getenv("DISCORD_WEBHOOK")
    notify_url: str | None = os.getenv("NOTIFY")  # apprise URL fallback
    notify_summary: bool = _bool("NOTIFY_SUMMARY", default=True)
    notify_errors: bool = _bool("NOTIFY_ERRORS", default=True)
    notify_claim_fails: bool = _bool("NOTIFY_CLAIM_FAILS", default=True)
    notify_login_request: bool = _bool("NOTIFY_LOGIN_REQUEST", default=True)

    # --- Epic Games ---
    eg_email: str | None = os.getenv("EG_EMAIL") or os.getenv("EMAIL")
    eg_password: str | None = os.getenv("EG_PASSWORD") or os.getenv("PASSWORD")
    eg_otpkey: str | None = os.getenv("EG_OTPKEY")
    eg_parentalpin: str | None = os.getenv("EG_PARENTALPIN")

    # --- Prime Gaming ---
    pg_email: str | None = os.getenv("PG_EMAIL") or os.getenv("EMAIL")
    pg_password: str | None = os.getenv("PG_PASSWORD") or os.getenv("PASSWORD")
    pg_otpkey: str | None = os.getenv("PG_OTPKEY")
    pg_force_check_collected: bool = _bool("PG_FORCE_CHECK_COLLECTED")
    pg_redeem: bool = _bool("PG_REDEEM")
    pg_claimdlc: bool = _bool("PG_CLAIMDLC")

    # --- GOG ---
    gog_email: str | None = os.getenv("GOG_EMAIL") or os.getenv("EMAIL")
    gog_password: str | None = os.getenv("GOG_PASSWORD") or os.getenv("PASSWORD")
    gog_newsletter: bool = _bool("GOG_NEWSLETTER")
    gog_force_redeem: bool = _bool("GOG_FORCE_REDEEM")

    # --- Steam ---
    steam_username: str | None = os.getenv("STEAM_USERNAME")
    steam_password: str | None = os.getenv("STEAM_PASSWORD") or os.getenv("PASSWORD")
    steam_use_gamerpower: bool = _bool("STEAM_USE_GAMERPOWER", default=True)

    # --- Module selection ---
    # Comma-separated list of stores to run (e.g. "steam,prime").
    # Empty = all stores enabled (default).
    stores: str = os.getenv("STORES", "")


cfg = Config()
