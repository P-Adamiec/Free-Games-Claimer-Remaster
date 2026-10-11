"""Which bot settings the web interface offers, in Dutch, and how each one is checked.

Every key here is a setting src/core/config.py reads, so it is safe to put in
data/config.env (the bot warns about names it does not know). Settings that only
Docker reads (VNC_PASSWORD, PUID, SHOW, ...) are deliberately left out: they
cannot be changed from data/config.env.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

TEXT, EMAIL, SECRET, OTPKEY, CODES, BOOL, INT, MULTI, TIMES, TZ, WEBHOOK, APPRISE = (
    "text", "email", "secret", "otpkey", "codes", "bool", "int", "multi", "times", "tz", "webhook", "apprise")
SECRET_KINDS = {SECRET, OTPKEY, CODES, WEBHOOK, APPRISE}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = TEXT
    default: str = ""
    help: str = ""
    min: int | None = None
    max: int | None = None
    choices: tuple = ()
    advanced: bool = False

    @property
    def secret(self) -> bool:
        return self.kind in SECRET_KINDS


@dataclass(frozen=True)
class Store:
    key: str
    name: str
    about: str
    fields: tuple = ()
    default_on: bool = False
    needs_vnc: str = ""


def _login(prefix: str, user_label: str = "E-mailadres", kind: str = EMAIL, user_key: str = "") -> tuple:
    return (
        Field(user_key or f"{prefix}_EMAIL", user_label, kind),
        Field(f"{prefix}_PASSWORD", "Wachtwoord", SECRET),
    )


OTP_HELP = ("De geheime sleutel uit de authenticator-instellingen (\"handmatige sleutel\"), "
            "dan vult de bot de inlogcode zelf in. Leeg laten mag: dan vraagt de bot via het scherm.")
CODES_HELP = "Herstelcodes, gescheiden door komma's. Gebruikte codes onthoudt de bot zelf."

STORES: tuple[Store, ...] = (
    Store("steam", "Steam", "Gratis games die je mag houden.",
          _login("STEAM", "Gebruikersnaam", TEXT, "STEAM_USERNAME"), default_on=True),
    Store("epic", "Epic Games", "De wekelijkse gratis games.", (
        *_login("EG"),
        Field("EG_OTP_KEY", "Authenticator-sleutel", OTPKEY, help=OTP_HELP),
        Field("EG_OTP_CODES", "Herstelcodes", CODES, help=CODES_HELP, advanced=True),
        Field("EG_PARENTALPIN", "Ouderlijk-toezicht-pincode", SECRET, advanced=True),
        Field("EG_MOBILE", "Ook de gratis mobiele game", BOOL, "true"),
        Field("EG_MOBILE_PLATFORMS", "Mobiele versies", MULTI, "android,ios", choices=(
            ("android", "Android"), ("ios", "iOS"))),
    ), default_on=True),
    Store("fab", "Fab", "Gratis assets op je Epic-account (zelfde login als Epic).", (
        Field("FAB_ACCEPT_EULA", "Licentie en afstandsverklaring automatisch accepteren", BOOL, "true"),
    )),
    Store("prime", "Prime Gaming (Amazon)", "De maandelijkse gratis games.", (
        *_login("PG"),
        Field("PG_OTP_KEY", "Authenticator-sleutel", OTPKEY, help=OTP_HELP),
        Field("PG_FORCE_CHECK_COLLECTED", "Al geclaimde games opnieuw controleren", BOOL, "false", advanced=True),
    ), default_on=True),
    Store("gog", "GOG", "Giveaways en de GOG-codes van Prime Gaming.", (
        *_login("GOG"),
        Field("GOG_OTP_KEY", "Authenticator-sleutel", OTPKEY, help=OTP_HELP),
        Field("GOG_OTP_CODES", "Herstelcodes", CODES, help=CODES_HELP, advanced=True),
        Field("GOG_NEWSLETTER", "Nieuwsbrief houden na een claim", BOOL, "false"),
        Field("GOG_FORCE_REDEEM", "Oude codes opnieuw inwisselen", BOOL, "false", advanced=True),
    ), default_on=True),
    Store("microsoft", "Microsoft / Xbox", "Codes van Prime Gaming en tijdelijk gratis games.", (
        *_login("MS"),
        Field("MS_OTP_KEY", "Authenticator-sleutel", OTPKEY, help=OTP_HELP),
        Field("MS_FORCE_REDEEM", "Codes van de laatste 60 dagen opnieuw proberen", BOOL, "false", advanced=True),
    ), default_on=True),
    Store("ubisoft", "Ubisoft", "Gratis game-giveaways op je Ubisoft Connect-account.", (
        *_login("UBI"),
        Field("UBI_OTP_KEY", "Authenticator-sleutel", OTPKEY, help=OTP_HELP),
    ), default_on=True),
    Store("unity", "Unity Asset Store", "Het wekelijkse gratis asset.", (
        *_login("UNITY"),
        Field("UNITY_ACCEPT_TOS", "Voorwaarden automatisch accepteren", BOOL, "true"),
    )),
    Store("aliexpress", "AliExpress", "Dagelijks munten ophalen.", (
        *_login("AE"),
        Field("AE_MIN_COINS", "Minimaal aantal munten", INT, "2", min=0, max=1000, advanced=True),
        Field("AE_FLAG_RETRIES", "Pogingen na een bot-blokkade", INT, "3", min=0, max=20, advanced=True),
        Field("AE_FLAG_WAIT", "Wachttijd na een blokkade (seconden)", INT, "480", min=0, max=7200, advanced=True),
        Field("AE_PAGE_RETRIES", "Extra pogingen bij een lege pagina", INT, "4", min=0, max=20, advanced=True),
    ), default_on=True),
    Store("itchio", "Itch.io", "Giveaways die GamerPower vindt.", (
        *_login("ITCHIO"),
        Field("ITCHIO_OTP_KEY", "Authenticator-sleutel", OTPKEY, help=OTP_HELP),
        Field("ITCHIO_OTP_CODES", "Herstelcodes", CODES, help=CODES_HELP, advanced=True),
    )),
    Store("fanatical", "Fanatical", "Steam-sleutels uit giveaways.", _login("FANATICAL")),
    Store("indiegala", "IndieGala", "Giveaways.", _login("INDIEGALA"),
          needs_vnc="Bij het inloggen moet je zelf een captcha aanvinken via het scherm; daarna blijf je 14 dagen ingelogd."),
    Store("alienware", "Alienware Arena", "Alleen een melding, de sleutel claim je zelf.", ()),
)
STORE_KEYS = tuple(s.key for s in STORES)
DEFAULT_STORES = tuple(s.key for s in STORES if s.default_on)

SCHEDULE: tuple[Field, ...] = (
    Field("SCHEDULER_HOURS", "Elke zoveel uur een run", INT, "12", min=0, max=168,
          help="0 = geen vaste interval, gebruik dan vaste tijden."),
    Field("SCHEDULER_FIXED_TIMES", "Vaste tijden per dag", TIMES,
          help="Bijvoorbeeld 09:00,21:30 (24-uurs notatie, gescheiden door komma's)."),
    Field("SCHEDULER_TIMEZONE", "Tijdzone", TZ, "UTC", help="Bijvoorbeeld Europe/Amsterdam."),
    Field("RUN_ON_STARTUP", "Een run starten zodra de bot opstart", BOOL, "true"),
)

NOTIFY: tuple[Field, ...] = (
    Field("DISCORD_WEBHOOK", "Discord-webhook", WEBHOOK,
          help="Discord: Serverinstellingen > Integraties > Webhooks > Nieuwe webhook > URL kopiëren."),
    Field("NOTIFY", "Apprise-adressen", APPRISE,
          help="Voor Telegram, ntfy, e-mail enz. Bijvoorbeeld tgram://bottoken/chatid. Meerdere met komma's."),
    Field("NOTIFY_SUMMARY", "Samenvatting na elke run", BOOL, "true"),
    Field("NOTIFY_ERRORS", "Fouten en crashes", BOOL, "true"),
    Field("NOTIFY_CLAIM_FAILS", "Games die niet geclaimd konden worden", BOOL, "true"),
    Field("NOTIFY_LOGIN_REQUEST", "Melding als je moet inloggen via het scherm", BOOL, "true"),
    Field("NOTIFY_ALREADY_CLAIMED", "Ook games die je al had", BOOL, "false", advanced=True),
    Field("NOTIFY_MISSING_BASE", "DLC waarvan je het basisspel mist", BOOL, "true", advanced=True),
    Field("NOTIFY_DOWNLOAD_ONLY", "Itch.io-giveaways die alleen een download zijn", BOOL, "true", advanced=True),
    Field("NOTIFY_UPDATES", "Nieuwe versie van de bot", BOOL, "true", advanced=True),
    Field("NOTIFY_TEST", "Testbericht bij elke start", BOOL, "false",
          help="Handig om te controleren of meldingen aankomen; zet het daarna weer uit."),
    Field("NOTIFY_SKIP_STORES", "Geen meldingen voor deze winkels", MULTI,
          choices=tuple((s.key, s.name) for s in STORES), advanced=True),
)

OTHER: tuple[Field, ...] = (
    Field("VNC_LOGIN_TIMEOUT", "Wachttijd voor handmatig inloggen (seconden)", INT, "180", min=30, max=3600,
          help="Zo lang wacht de bot op jou bij een inlogstap, captcha of code."),
    Field("GP_CLAIM_DLC", "Ook in-game DLC-giveaways (GamerPower)", BOOL, "false", advanced=True),
    Field("DRYRUN", "Proefmodus: alleen zoeken, niets claimen", BOOL, "false", advanced=True),
    Field("DEBUG", "Uitgebreid logboek", BOOL, "true", advanced=True),
    Field("EMAIL", "Standaard e-mailadres", EMAIL, advanced=True,
          help="Gebruikt voor elke winkel waar je hierboven niets invult."),
    Field("PASSWORD", "Standaard wachtwoord", SECRET, advanced=True),
)

SECTIONS = (("planning", "Planning", SCHEDULE), ("meldingen", "Meldingen", NOTIFY), ("overig", "Overig", OTHER))


def all_fields() -> list[Field]:
    out = [f for s in STORES for f in s.fields]
    for _, _, fields in SECTIONS:
        out.extend(fields)
    return out


FIELDS: dict[str, Field] = {f.key: f for f in all_fields()}
MANAGED_KEYS = frozenset(FIELDS) | {"STORES"}


# ----------------------------------------------------------------- validation

class Invalid(ValueError):
    pass


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_OTP_RE = re.compile(r"^[A-Z2-7]+=*$")
_CODE_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _plain(value: str, limit: int = 512) -> str:
    if any(ch in value for ch in "\r\n\x00"):
        raise Invalid("mag geen regeleinden bevatten")
    if "${" in value:
        # python-dotenv expands ${...} even inside quotes, so the bot would read something else.
        raise Invalid("mag de tekens ${ niet bevatten")
    if len(value) > limit:
        raise Invalid(f"is te lang (maximaal {limit} tekens)")
    return value


def clean(f: Field, raw) -> str:
    """The value as it goes into config.env, or Invalid with a Dutch reason."""
    if f.kind == BOOL:
        return "true" if raw in (True, "on", "true", "1", "yes") else "false"
    if f.kind == MULTI:
        picked = raw if isinstance(raw, (list, tuple)) else [p for p in str(raw or "").split(",")]
        allowed = [c for c, _ in f.choices]
        bad = [p for p in picked if p and p not in allowed]
        if bad:
            raise Invalid("bevat een onbekende keuze")
        return ",".join(c for c in allowed if c in picked)
    value = str(raw or "")
    if f.kind != SECRET:
        value = value.strip()
    value = _plain(value)
    if value == "":
        return ""
    if f.kind == EMAIL and ("@" not in value or " " in value):
        raise Invalid("is geen geldig e-mailadres")
    if f.kind == INT:
        try:
            number = int(value)
        except ValueError:
            raise Invalid("moet een heel getal zijn") from None
        if (f.min is not None and number < f.min) or (f.max is not None and number > f.max):
            raise Invalid(f"moet tussen {f.min} en {f.max} liggen")
        return str(number)
    if f.kind == TIMES:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        bad = [p for p in parts if not _TIME_RE.match(p)]
        if bad:
            raise Invalid(f"bevat een ongeldige tijd: {', '.join(bad)} (gebruik UU:MM)")
        return ",".join(dict.fromkeys(parts))
    if f.kind == TZ:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise Invalid("is geen bekende tijdzone (bijvoorbeeld Europe/Amsterdam)") from None
        return value
    if f.kind == OTPKEY:
        compact = value.replace(" ", "").upper()
        if not _OTP_RE.match(compact):
            raise Invalid("is geen geldige authenticator-sleutel (letters A-Z en cijfers 2-7)")
        return compact
    if f.kind == CODES:
        codes = [c.strip() for c in value.split(",") if c.strip()]
        if any(not _CODE_RE.match(c) for c in codes):
            raise Invalid("mag alleen letters, cijfers en streepjes bevatten, gescheiden door komma's")
        return ",".join(codes)
    if f.kind == WEBHOOK:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or not (host in ("discord.com", "discordapp.com")
                                            or host.endswith((".discord.com", ".discordapp.com")))
                or not parts.path.startswith("/api/webhooks/")):
            raise Invalid("moet een Discord-webhook zijn (https://discord.com/api/webhooks/...)")
        return value
    if f.kind == APPRISE:
        urls = [u.strip() for u in value.split(",") if u.strip()]
        if any("://" not in u or " " in u for u in urls):
            raise Invalid("moet een of meer Apprise-adressen zijn (iets://...), gescheiden door komma's")
        return ",".join(urls)
    return value


def timezones() -> list[str]:
    try:
        return sorted(available_timezones())
    except Exception:
        return ["UTC", "Europe/Amsterdam", "Europe/Brussels"]
