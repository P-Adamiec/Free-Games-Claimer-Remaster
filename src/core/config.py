"""Application configuration – reads your settings from the .env file.

This file is responsible for loading all the settings you define in your .env file
(like email addresses, passwords, Discord webhooks, etc.) and making them available
to the rest of the application as simple Python variables.

If a variable is not set, sensible defaults are used (e.g. screen size 1280x720).
Store-specific credentials (like EG_EMAIL) take priority over default ones (EMAIL).
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# Load .env files (project root first, then data/config.env as fallback).
# The GUI writes only explicit user choices to data/gui.env; those choices win
# so they survive container recreation without exposing the host's .env file.
_root = Path(__file__).resolve().parent.parent.parent
_env_root = _root / ".env"
_env_data = _root / "data" / "config.env"
_env_gui = _root / "data" / "gui.env"

load_dotenv(_env_root, override=False)
load_dotenv(_env_data, override=False)
load_dotenv(_env_gui, override=True)


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


# Aliases so NOTIFY_SKIP_STORES accepts the same names as the CLI/STORES.
_STORE_ALIASES = {"ae": "aliexpress", "amazon": "prime", "gp": "gamerpower"}


def _skip_stores(key: str) -> set:
    """Read a comma-separated store denylist into a set of canonical store keys."""
    out = set()
    for s in os.getenv(key, "").split(","):
        s = s.strip().lower()
        if s:
            out.add(_STORE_ALIASES.get(s, s))
    return out


# ----- Settings guard: a setting nobody reads, or a value that cannot mean what it says (issue #40) -----

# The same scan tests/test_docs_env.py uses, with the helper captured so the expected type is known too.
_SETTING_RE = re.compile(r'(os\.getenv|_bool|_int|_skip_stores)\(\s*"([A-Z_0-9]+)"')
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", re.M)
_KIND_BY_HELPER = {"_bool": "bool", "_int": "int", "_skip_stores": "str", "os.getenv": "str"}
_TRUTHY = ("1", "true", "yes")
_FALSY = ("", "0", "false", "no")
# Anything that must never reach a log someone pastes into a bug report.
_SECRET_HINTS = ("PASSWORD", "SECRET", "TOKEN", "OTPKEY", "OTP_CODES", "PIN",
                 "COOKIE", "AUTH", "CREDENTIAL", "WEBHOOK", "EMAIL", "USERNAME")


def env_setting_kinds() -> dict:
    """Every setting this file reads, mapped to the kind of value it expects."""
    try:
        source = Path(__file__).read_text(encoding="utf-8")
    except OSError:
        return {}
    return {name: _KIND_BY_HELPER[helper] for helper, name in _SETTING_RE.findall(source)}


def known_env_names() -> set:
    """Settings the bot reads, plus the Docker-only ones, which live in .env.example."""
    names = set(env_setting_kinds())
    try:
        example = (_root / ".env.example").read_text(encoding="utf-8")
    except OSError:
        return names
    return names | set(re.findall(r"^#?\s*([A-Z_0-9]+)=", example, re.M))


def env_file_settings() -> dict:
    """Names and values actually set in your .env files. Commented-out lines set nothing."""
    found = {}
    for path in (_env_root, _env_data):
        try:
            # utf-8-sig: an editor-added BOM would otherwise glue itself to the first name.
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for name, value in _ENV_LINE_RE.findall(text):
            found[name] = value.strip().strip('"').strip("'")
    return found


def mask_value(name: str, value: str) -> str:
    """A value safe to print in a log someone will paste into a bug report."""
    upper = (name or "").upper()
    if upper == "NOTIFY" or any(hint in upper for hint in _SECRET_HINTS):
        return "***"
    # Catches a credential under a name we did not think of.
    if "@" in value or "://" in value:
        return "***"
    return value[:40]


def _looks_int(value: str) -> bool:
    """True when _int() would accept this value instead of falling back."""
    try:
        int(value)
    except ValueError:
        return False
    return True


def settings_warnings() -> list:
    """Settings that do nothing: unknown names, and values that cannot mean what they say."""
    kinds = env_setting_kinds()
    known = known_env_names()
    out = []
    for name, value in env_file_settings().items():
        if name not in known:
            out.append(f"{name} is not a setting this bot reads, so it does nothing.")
        elif kinds.get(name) == "bool" and value.lower() not in _TRUTHY + _FALSY:
            out.append(f"{name}={mask_value(name, value)} is not a yes/no value, so it reads as false.")
        elif kinds.get(name) == "int" and value and not _looks_int(value):
            out.append(f"{name}={mask_value(name, value)} is not a number, so the default is used.")
    return out


class Config:
    """All application settings in one place.
    
    Every setting here corresponds to an environment variable in your .env file.
    For example, 'eg_email' reads from the EG_EMAIL variable.
    """

    # --- General ---
    debug: bool = _bool("DEBUG", default=True)
    # Internals of third-party libraries (CDP frames, HTTP handshakes, SQL). Separate from DEBUG.
    debug_libs: bool = _bool("DEBUG_LIBS", default=False)
    dryrun: bool = _bool("DRYRUN")
    show: bool = _bool("SHOW", default=True)
    width: int = _int("WIDTH", 1280)
    height: int = _int("HEIGHT", 720)
    timeout: int = _int("TIMEOUT", 60) * 1000          # ms
    vnc_login_timeout: int = _int("VNC_LOGIN_TIMEOUT", 180) # seconds
    novnc_port: str = os.getenv("NOVNC_PORT", "7080")
    vnc_ip: str = os.getenv("VNC_IP", "localhost")
    # Full public noVNC address for reverse proxies; replaces VNC_IP and NOVNC_PORT in links.
    vnc_url_base: str | None = os.getenv("VNC_URL")

    @property
    def vnc_url(self) -> str:
        """One-click noVNC link for notifications (autoconnect opens the session)."""
        base = (self.vnc_url_base or "").strip().rstrip("/")
        if base:
            # A bare host means a reverse proxy, which is practically always https.
            if "://" not in base:
                base = f"https://{base}"
            return f"{base}/?autoconnect=true"
        return f"http://{self.vnc_ip}:{self.novnc_port}/?autoconnect=true"

    scheduler_hours: int = _int("SCHEDULER_HOURS", 12)
    scheduler_timezone: str = os.getenv("SCHEDULER_TIMEZONE", "UTC").strip() or "UTC"
    scheduler_fixed_times: str = os.getenv("SCHEDULER_FIXED_TIMES", "")
    run_on_startup: bool = _bool("RUN_ON_STARTUP", default=True)

    # --- Local web dashboard ---
    gui_enabled: bool = _bool("GUI_ENABLED", default=True)
    gui_port: int = _int("GUI_PORT", 8080)

    # --- DB Reset ---
    reset_db_games: bool = _bool("RESET_DB_GAMES", default=False)

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
    notify_claim_fails: bool = _bool("NOTIFY_CLAIM_FAILS", default=False)
    notify_already_claimed: bool = _bool("NOTIFY_ALREADY_CLAIMED", default=False)
    notify_updates: bool = _bool("NOTIFY_UPDATES", default=True)
    notify_login_request: bool = _bool("NOTIFY_LOGIN_REQUEST", default=True)
    notify_test: bool = _bool("NOTIFY_TEST", default=False)
    # Stores whose notifications are silenced (they still run and claim).
    notify_skip_stores: set = _skip_stores("NOTIFY_SKIP_STORES")

    def store_notify_enabled(self, store_name: str | None) -> bool:
        """False when the store's notifications are silenced via NOTIFY_SKIP_STORES."""
        return (store_name or "").lower() not in self.notify_skip_stores

    # --- Epic Games ---
    eg_email: str | None = os.getenv("EG_EMAIL") or os.getenv("EMAIL")
    eg_password: str | None = os.getenv("EG_PASSWORD") or os.getenv("PASSWORD")
    eg_otpkey: str | None = os.getenv("EG_OTPKEY")
    eg_parentalpin: str | None = os.getenv("EG_PARENTALPIN")
    # Epic's weekly mobile giveaways (claimed on the same store pages as the PC games).
    eg_mobile: bool = _bool("EG_MOBILE", default=True)
    eg_mobile_platforms: str = os.getenv("EG_MOBILE_PLATFORMS", "android,ios")

    @property
    def eg_mobile_platform_list(self) -> list[str]:
        """EG_MOBILE_PLATFORMS as a clean list, ignoring anything but android/ios."""
        wanted = [p.strip().lower() for p in self.eg_mobile_platforms.split(",")]
        return [p for p in wanted if p in ("android", "ios")]

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
    gog_otp_enable: bool = _bool("GOG_OTP_ENABLE")
    gog_otp_codes: list[str] = [c.strip() for c in os.getenv("GOG_OTP_CODES", "").split(",") if c.strip()]

    # --- Steam ---
    steam_username: str | None = os.getenv("STEAM_USERNAME")
    steam_password: str | None = os.getenv("STEAM_PASSWORD") or os.getenv("PASSWORD")

    # --- Unity Asset Store ---
    unity_email: str | None = os.getenv("UNITY_EMAIL") or os.getenv("EMAIL")
    unity_password: str | None = os.getenv("UNITY_PASSWORD") or os.getenv("PASSWORD")
    # Claiming needs Unity's Terms of Service accepted once at checkout.
    unity_accept_tos: bool = _bool("UNITY_ACCEPT_TOS", default=True)

    # --- Fab (Epic's asset marketplace, signs in with the Epic account) ---
    # Claiming requires accepting Fab's licence and the EU right-of-withdrawal waiver.
    fab_accept_eula: bool = _bool("FAB_ACCEPT_EULA", default=True)

    # --- Ubisoft ---
    ubi_email: str | None = os.getenv("UBI_EMAIL") or os.getenv("EMAIL")
    ubi_password: str | None = os.getenv("UBI_PASSWORD") or os.getenv("PASSWORD")
    ubi_otpkey: str | None = os.getenv("UBI_OTPKEY")

    # --- GamerPower ---
    # Most giveaways are in-game DLC needing a per-game account, so they are skipped by default.
    gp_claim_dlc: bool = _bool("GP_CLAIM_DLC", default=False)

    # --- GamerPower & Fanatical ---
    # Some GamerPower giveaways redirect to Fanatical.com,
    # which requires a Fanatical account + Steam account connection.
    # Set FANATICAL_ENABLE=true and provide credentials to enable.
    fanatical_enable: bool = _bool("FANATICAL_ENABLE", default=False)
    fanatical_email: str | None = os.getenv("FANATICAL_EMAIL") or os.getenv("EMAIL")
    fanatical_password: str | None = os.getenv("FANATICAL_PASSWORD") or os.getenv("PASSWORD")

    # --- Alienware Arena ---
    alienware_enable: bool = _bool("ALIENWARE_ENABLE", default=False)

    # --- Itch.io ---
    itchio_enable: bool = _bool("ITCHIO_ENABLE", default=False)
    itchio_email: str | None = os.getenv("ITCHIO_EMAIL") or os.getenv("EMAIL")
    itchio_password: str | None = os.getenv("ITCHIO_PASSWORD") or os.getenv("PASSWORD")
    # Recovery codes are static, so the bot can spend them; a TOTP secret would only move
    # your second factor onto this machine, so two-factor sign-in goes through VNC instead.
    # Spent one at a time and remembered in data/used_itchio_codes.txt.
    itchio_otp_enable: bool = _bool("ITCHIO_OTP_ENABLE")
    itchio_otp_codes: list[str] = [c.strip() for c in os.getenv("ITCHIO_OTP_CODES", "").split(",") if c.strip()]

    # --- IndieGala ---
    indiegala_enable: bool = _bool("INDIEGALA_ENABLE", default=False)
    indiegala_email: str | None = os.getenv("INDIEGALA_EMAIL") or os.getenv("EMAIL")
    indiegala_password: str | None = os.getenv("INDIEGALA_PASSWORD") or os.getenv("PASSWORD")

    # --- AliExpress ---
    ae_email: str | None = os.getenv("AE_EMAIL") or os.getenv("EMAIL")
    ae_password: str | None = os.getenv("AE_PASSWORD") or os.getenv("PASSWORD")
    # Bot-flag guard: skip collecting under AE_MIN_COINS, then wait AE_FLAG_WAIT (> ~7-min penalty) and retry AE_FLAG_RETRIES times.
    ae_min_coins: int = _int("AE_MIN_COINS", 2)
    ae_flag_retries: int = _int("AE_FLAG_RETRIES", 3)
    ae_flag_wait: int = _int("AE_FLAG_WAIT", 480)  # seconds (> ~7-min penalty)
    # AliExpress serves the coin page empty most of the time (measured: 1 usable page in 8 looks
    # over four minutes), so each extra approach is a real chance. 0 gives up on the first look.
    ae_page_retries: int = _int("AE_PAGE_RETRIES", 4)

    # --- Unknown/Other Indirect Stores ---
    unknown_stores_enable: bool = _bool("UNKNOWN_STORES_ENABLE", default=False)

    # --- Module selection ---
    # Comma-separated list of stores to run (e.g. "steam,prime").
    # Empty = main.py's DEFAULT_STORES, which is every store except Unity.
    stores: str = os.getenv("STORES", "")


cfg = Config()
