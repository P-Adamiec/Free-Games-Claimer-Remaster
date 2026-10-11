"""Web interface accounts: a JSON file with argon2 password hashes.

Stored on the web interface's own volume (never on a bot's volume, so a bot can
not read it). Roles: "admin" (sees everyone, manages accounts) and "user" (only
their own bot). Each account is linked to at most one bot.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from webui import protocol

ROLES = ("admin", "user")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
MIN_PASSWORD = 10

_hasher = PasswordHasher()
_lock = threading.Lock()
# Verifying against this keeps "unknown user" as slow as "wrong password".
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


class UserError(ValueError):
    pass


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise UserError("Een gebruikersnaam is 2-32 tekens: kleine letters, cijfers, - of _, "
                        "en begint met een letter.")
    return name


def check_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        raise UserError(f"Een wachtwoord moet minstens {MIN_PASSWORD} tekens hebben.")
    if len(password) > 256:
        raise UserError("Dat wachtwoord is te lang.")
    return password


def fingerprint(user: dict) -> str:
    """Changes whenever the password does, so a reset logs out every old session."""
    return hashlib.sha256(user.get("hash", "").encode()).hexdigest()[:16]


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        data = protocol.read_json(self.path) or {}
        users = data.get("users")
        return users if isinstance(users, dict) else {}

    def _save(self, users: dict) -> None:
        protocol.write_json(self.path, {"users": users})

    def all(self) -> dict:
        return self._load()

    def get(self, name: str) -> dict | None:
        return self._load().get((name or "").lower())

    def empty(self) -> bool:
        return not self._load()

    def verify(self, name: str, password: str) -> dict | None:
        user = self.get(name)
        try:
            if user is None:
                _hasher.verify(_DUMMY_HASH, password or "x")
                return None
            _hasher.verify(user["hash"], password or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        if _hasher.check_needs_rehash(user["hash"]):
            self.set_password(name, password, check=False)
            user = self.get(name)
        return user

    def create(self, name: str, password: str, role: str = "user", bot: str | None = None) -> dict:
        name = check_name(name)
        check_password(password)
        if role not in ROLES:
            raise UserError("Onbekende rol.")
        with _lock:
            users = self._load()
            if name in users:
                raise UserError("Die gebruikersnaam bestaat al.")
            if bot and any(u.get("bot") == bot for u in users.values()):
                raise UserError("Die bot is al gekoppeld aan een andere gebruiker.")
            users[name] = {"hash": _hasher.hash(password), "role": role, "bot": bot or None,
                           "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._save(users)
            return users[name]

    def set_password(self, name: str, password: str, check: bool = True) -> None:
        if check:
            check_password(password)
        with _lock:
            users = self._load()
            if name not in users:
                raise UserError("Onbekende gebruiker.")
            users[name]["hash"] = _hasher.hash(password)
            self._save(users)

    def update(self, name: str, *, role: str | None = None, bot: str | None = "") -> None:
        with _lock:
            users = self._load()
            if name not in users:
                raise UserError("Onbekende gebruiker.")
            if role is not None:
                if role not in ROLES:
                    raise UserError("Onbekende rol.")
                if role != "admin" and users[name]["role"] == "admin" and \
                        sum(1 for u in users.values() if u["role"] == "admin") == 1:
                    raise UserError("Er moet minstens één beheerder overblijven.")
                users[name]["role"] = role
            if bot != "":
                if bot and any(u.get("bot") == bot for n, u in users.items() if n != name):
                    raise UserError("Die bot is al gekoppeld aan een andere gebruiker.")
                users[name]["bot"] = bot or None
            self._save(users)

    def delete(self, name: str) -> None:
        with _lock:
            users = self._load()
            if name not in users:
                raise UserError("Onbekende gebruiker.")
            if users[name]["role"] == "admin" and sum(1 for u in users.values() if u["role"] == "admin") == 1:
                raise UserError("De laatste beheerder kan niet verwijderd worden.")
            del users[name]
            self._save(users)

    def bootstrap(self, name: str | None, password: str | None, bots: list[str]) -> str:
        """Create the first admin from the environment, only while there are no accounts at all."""
        if not self.empty():
            return "exists"
        if not name or not password:
            return "missing"
        name = check_name(name)
        self.create(name, password, "admin", name if name in bots else None)
        return "created"
