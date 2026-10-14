"""Keeps passwords, sign-in names, notification tokens and game codes out of the log, whichever library writes the line.

Notifications are yours and keep everything in full; only the log, which ends up in bug reports, is masked.
"""

from __future__ import annotations

import logging
import re
import traceback
from urllib.parse import parse_qsl, urlsplit

from rich.markup import escape

# Settings that work like a password, and settings that name your account.
SECRET_FIELDS = ("_password", "_otp_key", "_otp_codes", "_token", "_secret", "_api_key", "_cookie")
ACCOUNT_FIELDS = ("_email", "_username", "_login")

# Any e-mail address, also one a site quotes back that is not in .env.
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")

# Every value the log must never show, mapped to what it shows instead.
_hidden: dict[str, str] = {}
_pattern: re.Pattern | None = None


def mask_account(name) -> str:
    """Account label safe for a log someone pastes into a bug report: a.n.other@mail.com -> a***@mail.com."""
    text = str(name or "")
    local, at, domain = text.partition("@")
    if not at:
        return text          # a nickname is not personal data, leave it readable
    return f"{local[:1]}***@{domain}"


def mask_label(name) -> str:
    """How an account shows in the log: a***@mail.com for an e-mail, the first letter and *** for any other name."""
    text = str(name or "").strip()
    return mask_account(text) if "@" in text else f"{text[:1]}***"


def mask_code(code) -> str:
    """A game code cut for the log, like Steam keys: the whole code is only in your notification."""
    text = str(code or "").strip()
    return f"{text[:5]}…" if text else ""


def hide(value, shown: str = "***") -> None:
    """Never let this value reach the log again, from any library; `shown` stands in for it."""
    global _pattern
    text = str(value or "").strip()
    # Shorter values would cut into ordinary words and numbers; three letters still covers names like Ewa.
    if len(text) < 3 or _hidden.get(text) == shown:
        return
    _hidden[text] = shown
    words = "|".join(re.escape(v) for v in sorted(_hidden, key=len, reverse=True))
    _pattern = re.compile(rf"(?<![A-Za-z0-9])(?:{words})(?![A-Za-z0-9])")


def redact(text: str) -> str:
    """The text with every hidden value replaced as a whole word, and every e-mail address masked."""
    pattern = _pattern
    if pattern:
        text = pattern.sub(lambda m: _hidden[m.group(0)], text)
    return EMAIL.sub(r"\1***@\2", text) if "@" in text else text


def hide_account(name) -> None:
    """Mask an account name read from a page, such as your first name on Amazon, in every later log line."""
    if str(name or "").strip().lower() != "unknown":
        hide(name, mask_label(name))


def url_secrets(url: str) -> list:
    """The parts of a notification address that work like a password: tokens, keys, topics, user and password."""
    parts = urlsplit(str(url or "").strip())
    pieces = re.split(r"[@:]", parts.netloc) + parts.path.split("/") + [v for _, v in parse_qsl(parts.query)]
    # A host name has a dot and is public; short plain words like "webhooks" or "markdown" are part of every address.
    return [p for p in pieces if len(p) >= 6 and "." not in p and not (p.isalpha() and p.islower() and len(p) <= 8)]


def hide_settings(cfg) -> None:
    """Hide every password, two-factor secret, sign-in name and notification token set in .env."""
    for name in dir(cfg):
        if name.startswith("_"):
            continue
        value = getattr(cfg, name, None)
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str):
                continue
            if name.endswith(SECRET_FIELDS):
                hide(item)
            elif name.endswith(ACCOUNT_FIELDS):
                hide(item, mask_label(item))
    for url in [cfg.discord_webhook or ""] + str(cfg.notify_url or "").split(","):
        for piece in url_secrets(url):
            hide(piece)


class RedactFilter(logging.Filter):
    """Masks every hidden value in a log line before it is printed, tracebacks included."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            clean = redact(message)
            if record.exc_info and record.exc_info[1] is not None:
                trace = "".join(traceback.format_exception(*record.exc_info))
                if redact(trace) != trace:
                    # Rich would print the traceback with the value in it, so it goes out as masked text.
                    clean = f"{clean}\n{escape(redact(trace))}"
                    record.exc_info, record.exc_text = None, None
            if clean != message:
                record.msg, record.args = clean, None
        except Exception:
            pass  # a log line must never be lost to the mask itself
        return True
