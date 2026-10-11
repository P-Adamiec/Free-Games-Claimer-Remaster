"""Reading and writing a bot's data/config.env, with write-only secrets.

The bot loads data/config.env itself (src/core/config.py), after the variables
Docker passes in. A variable Docker sets therefore always wins; those keys are
shown as "vastgezet" and never changed here.

Secrets never leave this module in readable form: the view only says whether
one is set, and a blank secret field in a form keeps the stored value.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from webui import protocol
from webui.app import schema

HEADER = (
    "# Beheerd door de webinterface (webui/). Wijzigingen hier worden overschreven als je\n"
    "# in de webinterface op Opslaan drukt; instellingen die je hier zelf toevoegt blijven staan.\n"
)


def read_config(path: Path) -> dict[str, str]:
    """Every KEY=value in the file, exactly as the bot will see it."""
    if not path.exists():
        return {}
    values = dotenv_values(path, interpolate=False, encoding="utf-8")
    return {k: v for k, v in values.items() if v is not None}


def locked_keys(env_file: Path | None) -> set[str]:
    """Managed settings that Docker already passes to the bot, so config.env cannot change them."""
    if not env_file or not env_file.exists():
        return set()
    try:
        names = set(dotenv_values(env_file, interpolate=False))
    except OSError:
        return set()
    return names & schema.MANAGED_KEYS


def quote(value: str) -> str:
    """Single quotes with only \\\\ and \\' escaped. ${ is refused earlier: dotenv expands it even here."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def serialize(values: dict[str, str]) -> str:
    order = ["STORES", *[f.key for f in schema.all_fields()]]
    keys = [k for k in order if k in values] + sorted(k for k in values if k not in order)
    lines = [HEADER]
    for key in keys:
        value = values[key]
        if any(ch in value for ch in "\r\n\x00") or "${" in value:
            raise ValueError(f"{key} contains a line break or ${{, which the bot cannot read back")
        lines.append(f"{key}={quote(value)}\n")
    return "".join(lines)


def write_config(path: Path, values: dict[str, str]) -> None:
    protocol.atomic_write(path, serialize(values), mode=0o600)


def selected_stores(values: dict[str, str]) -> list[str]:
    raw = values.get("STORES", "").strip()
    if not raw:
        return list(schema.DEFAULT_STORES)
    aliases = {"ae": "aliexpress", "amazon": "prime", "ms": "microsoft", "xbox": "microsoft",
               "itch": "itchio", "itch.io": "itchio", "awa": "alienware", "ubi": "ubisoft"}
    picked = {aliases.get(s.strip().lower(), s.strip().lower()) for s in raw.split(",")}
    return [k for k in schema.STORE_KEYS if k in picked]


def field_view(f: schema.Field, values: dict[str, str], locked: set[str]) -> dict:
    """What a template may know about one field. Never contains a secret value."""
    stored = values.get(f.key)
    view = {"field": f, "locked": f.key in locked, "is_set": bool(stored)}
    if f.secret:
        view["value"] = ""
    elif f.kind == schema.MULTI:
        view["value"] = [p for p in (stored if stored is not None else f.default).split(",") if p]
    elif f.kind == schema.BOOL:
        raw = (stored if stored is not None else f.default).strip().lower()
        view["value"] = raw in ("1", "true", "yes")
    else:
        view["value"] = stored if stored is not None else ""
    return view


def view(values: dict[str, str], locked: set[str]) -> dict:
    stores = selected_stores(values)
    return {
        "stores": [{"store": s, "enabled": s.key in stores,
                    "fields": [field_view(f, values, locked) for f in s.fields]} for s in schema.STORES],
        "sections": [{"id": sid, "title": title, "fields": [field_view(f, values, locked) for f in fields]}
                     for sid, title, fields in schema.SECTIONS],
        "stores_locked": "STORES" in locked,
    }


def _form_get(form, key):
    if hasattr(form, "getlist"):
        return form.getlist(key)
    value = form.get(key)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def apply_form(current: dict[str, str], form, locked: set[str] = frozenset()) -> tuple[dict[str, str], dict[str, str]]:
    """New config values from a submitted form, plus a Dutch error message per field key.

    `form` is a starlette FormData or a plain dict (lists for multi-values).
    """
    new = dict(current)
    errors: dict[str, str] = {}

    if "STORES" not in locked:
        picked = [s for s in _form_get(form, "stores") if s in schema.STORE_KEYS]
        if not picked:
            errors["STORES"] = "Kies minstens één winkel."
        else:
            new["STORES"] = ",".join(k for k in schema.STORE_KEYS if k in picked)

    for f in schema.all_fields():
        if f.key in locked:
            continue
        given = _form_get(form, f.key)
        if f.secret:
            if _form_get(form, f"{f.key}__clear"):
                new.pop(f.key, None)
                continue
            raw = given[0] if given else ""
            if raw.strip() == "":
                continue  # blank = keep what is stored
        elif f.kind == schema.BOOL:
            raw = "on" if given and given[-1] in ("on", "true", "1") else ""
        elif f.kind == schema.MULTI:
            raw = given
        else:
            raw = given[0] if given else ""
        try:
            value = schema.clean(f, raw)
        except schema.Invalid as exc:
            errors[f.key] = f"{f.label} {exc}."
            continue
        # Empty means "use the bot's default", except a list whose default is not empty
        # (EG_MOBILE_PLATFORMS: nothing ticked really means none).
        if value == "" and not (f.kind == schema.MULTI and f.default):
            new.pop(f.key, None)
        else:
            new[f.key] = value

    hours = new.get("SCHEDULER_HOURS", "12")
    if hours.strip() == "0" and not new.get("SCHEDULER_FIXED_TIMES", "").strip() \
            and "SCHEDULER_HOURS" not in errors:
        errors["SCHEDULER_HOURS"] = ("Zonder interval én zonder vaste tijden zou de bot na één run stoppen. "
                                     "Vul een aantal uren of vaste tijden in.")
    return new, errors
