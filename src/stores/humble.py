"""Humble Bundle: claims the games of a Humble Choice subscription.

"Claiming" a Choice game means revealing its key. The month itself is never
unlocked by the bot: early unlock is a payment, and a subscriber's month is
created by the automatic billing on the last Tuesday, so by the time a run
looks, a paid month either exists as an order or it does not.

What a run does, in order:

  1. Sign in (HB_EMAIL / HB_PASSWORD, an authenticator code if asked, else you
     over VNC). Humble's own JSON API, asked from inside the signed-in page,
     says whether the session is alive.
  2. Read your order list and pick the one month to work on. The list is
     only gamekeys — what an order is, and when it was made, lives in the
     order itself — so classifications are kept between runs (in the data
     folder, next to the database): an order whose product and date are
     already known is never opened just to be classified again, and a run
     opens only orders it has never seen, plus the one month it works on,
     in full. The pick is the most recent Choice month from the current
     plan, the single all-games plan sold since February 2022.
     Pick-limited months from before that switch are left alone: their
     unpicked games are a choice only you can make, and the bot must not
     spend picks. Older current-plan months are left alone too: unrevealed
     keys in them are a back catalogue to settle by hand, not a sweep for
     a monthly run, so they stay exactly as they are.
  3. Read the month's lineup from its membership page, because the order
     alone cannot see a game that was never selected on the site: it has
     no key entry there yet. Lineup games without an entry join the run as
     waiting games in their own right — the Steam check below covers them
     too, and one already in your library is never chosen, so no key entry
     is ever created for it. For every game whose key is still unrevealed
     and unexpired: ask Humble to choose the month's content (one call per
     month, by the short identifiers the site's own claim button sends),
     then reveal each key with one call per game. Choosing a game that
     had no entry is what makes Humble create it, so the order is read
     once more before the reveals. Both calls run inside the page, because
     Humble's anti-bot check turns away plain HTTP clients on those two
     endpoints while reads pass.
  4. Save each revealed key in the database, tagged with the store it
     belongs to. A Steam key is then activated by the Steam module in the
     same run when `steam` is in STORES too, and a GOG key is redeemed by
     the GOG module when `gog` is, exactly like the keys Prime Gaming and
     Fanatical hand out. Any other key waits in the database and on your
     Humble keys page, where a revealed key sits anyway.

Before step 3, a waiting Steam key is checked against your own Steam
library (HB_CHECK_STEAM, on unless switched off): a game already on Steam
keeps its key unrevealed, so the key stays giftable and the month stays
refundable. The check rides Steam's own browser profile and sign-in, and
reads Steam's own userdata and search suggestions. When your library
cannot be read at all, no Steam key is revealed that run either: a key
revealed for a game you already own is the one outcome no later run can
undo, so the keys wait for a run that can check. A dry run reveals
nothing either way, so it simply lists what it would do.

Revealing is one-way: a revealed key can no longer be gifted from Humble,
and a month with a revealed key in it is no longer refundable. That is why
this store is opt-in (name `humble` in STORES) and why a key that has
already been revealed, by you or by an earlier run, is never touched again.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from src.core.claimer import BaseClaimer, OTP_KEY_ATTEMPTS, account_key
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.redact import mask_code
from src.core.run_state import needs_you

logger = logging.getLogger("fgc.humble")

SITE_HOST = "www.humblebundle.com"
URL_LOGIN = "https://www.humblebundle.com/login"
URL_KEYS = "https://www.humblebundle.com/home/keys"
URL_MEMBERSHIP = "https://www.humblebundle.com/membership/home"
API_ORDERS = "/api/v1/user/order"
API_ORDER = "/api/v1/order/{gamekey}?all_tpkds=true"
API_CHOOSE = "/humbler/choosecontent"
API_REDEEM = "/humbler/redeemkey"

# Choice became a single plan with every game included on 2022-02-01. Months
# bought before that are pick-limited, so their orders are never claimed from.
LEGACY_CUTOFF = datetime(2022, 2, 1, tzinfo=timezone.utc)

# One fetch, run inside the signed-in page: the session cookie and the CSRF
# token only exist there, and /humbler/* turns plain HTTP clients away.
FETCH_JS = """
    (async () => {
        const method = %s, url = %s, form = %s;
        const init = {method, credentials: 'include', headers: {'Accept': 'application/json'}};
        if (form) {
            const token = (document.cookie.match(/(?:^|; )csrf_cookie=([^;]*)/) || [])[1] || '';
            const body = new URLSearchParams();
            for (const [name, value] of form) body.append(name, value);
            init.headers['Content-Type'] = 'application/x-www-form-urlencoded';
            init.headers['csrf-prevention-token'] = decodeURIComponent(token);
            init.body = body;
        }
        try {
            const r = await fetch(url, init);
            return JSON.stringify({status: r.status, body: await r.text()});
        } catch (e) {
            return JSON.stringify({status: 0, body: '', error: String(e).slice(0, 120)});
        }
    })()
"""

# The sign-in form is drawn by script, so its fields are found by what they
# are, not by one build's ids: the e-mail box, the password box, the button.
LOGIN_FORM_JS = """
    JSON.stringify({
        href: location.href,
        email: !!document.querySelector('input[type="email"], input[name="username"], input#username'),
        password: !!document.querySelector('input[type="password"]'),
        code: !!document.querySelector('input[name*="code" i], input[autocomplete="one-time-code"]'),
        text: document.body ? document.body.innerText.slice(0, 400) : '',
    })
"""

FILL_LOGIN_JS = """
    (() => {
        const set = (el, value) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        };
        const email = document.querySelector('input[type="email"], input[name="username"], input#username');
        const password = document.querySelector('input[type="password"]');
        if (!email || !password) return false;
        set(email, %s);
        set(password, %s);
        const box = document.querySelector('input[name="remember"]');
        if (box && !box.checked) box.click();
        const button = document.querySelector('button[type="submit"], input[type="submit"]')
            || [...document.querySelectorAll('button')].find(b => /log\\s?in|sign\\s?in/i.test(b.textContent || ''));
        if (!button) return false;
        button.click();
        return true;
    })()
"""

FILL_CODE_JS = """
    (() => {
        const field = document.querySelector('input[name*="code" i], input[autocomplete="one-time-code"]');
        if (!field) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(field, %s);
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
        const button = document.querySelector('button[type="submit"], input[type="submit"]')
            || [...document.querySelectorAll('button')].find(b => /verify|continue|submit/i.test(b.textContent || ''));
        if (button) button.click();
        return true;
    })()
"""


def order_created(order: dict) -> datetime | None:
    """When an order was bought, None when Humble's stamp cannot be read."""
    raw = str((order or {}).get("created") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def is_choice_order(order: dict) -> bool:
    """True for a Humble Choice month: a subscription product, or one named Choice."""
    product = (order or {}).get("product") or {}
    if str(product.get("category") or "").strip().lower() == "subscription":
        return True
    name = f"{product.get('machine_name') or ''} {product.get('human_name') or ''}".lower()
    return "choice" in name


def summary_facts(entry: dict) -> dict:
    """What one order-list entry says about its order, without opening it.

    Today's list is only gamekeys, so in practice every fact but the
    gamekey comes back empty and the caller classifies another way; the
    reading stays because an entry that does carry its product and date
    should be believed. "choice" comes back None when the entry says
    nothing about its product.
    """
    entry = entry or {}
    product = entry.get("product") or {}
    category = str(product.get("category") or "").strip().lower()
    name = f"{product.get('machine_name') or ''} {product.get('human_name') or ''}".lower()
    choice: bool | None = None
    if category or name.strip():
        choice = category == "subscription" or "choice" in name
    return {
        "gamekey": str(entry.get("gamekey") or ""),
        "choice": choice,
        "created": order_created(entry),
        "human_name": str(product.get("human_name") or ""),
    }


def detail_facts(order: dict, gamekey: str) -> dict:
    """summary_facts for an order opened in full, under the gamekey asked for."""
    product = (order or {}).get("product") or {}
    return {
        "gamekey": gamekey,
        "choice": is_choice_order(order),
        "created": order_created(order),
        "human_name": str(product.get("human_name") or ""),
    }


def pick_month(facts: list) -> tuple[dict | None, list, int]:
    """Split classified orders into (the month to work on, legacy months, older count).

    The month to work on is the most recent current-plan Choice month, the
    legacy months are the pick-limited ones from before February 2022, and
    the count is how many other current-plan months are left alone. A date
    that cannot be read counts as current-plan, not legacy: its games are
    visible either way, and a stamp Humble changes one day must not quietly
    retire every month there is. It still ranks oldest when the newest
    month is picked, so the same changed stamp cannot promote it either.
    """
    legacy, current = [], []
    for fact in facts or []:
        if not fact.get("choice"):
            continue
        created = fact.get("created")
        if created is not None and created < LEGACY_CUTOFF:
            legacy.append(fact)
        else:
            current.append(fact)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    latest = max(current, key=lambda fact: fact.get("created") or epoch) if current else None
    older = len(current) - 1 if latest is not None else 0
    return latest, legacy, older


def facts_to_cache(facts: list) -> dict:
    """The kept form of classified orders: gamekey -> the facts a pick needs.

    An order's product and date never change once it exists, so what one
    run learned about an order stays true for every later run.
    """
    kept = {}
    for fact in facts or []:
        gamekey = fact.get("gamekey") or ""
        if not gamekey or fact.get("choice") is None:
            continue
        created = fact.get("created")
        kept[gamekey] = {
            "choice": bool(fact["choice"]),
            "created": created.isoformat() if created else None,
            "human_name": fact.get("human_name") or "",
        }
    return kept


def facts_from_cache(cache: dict) -> dict:
    """Kept classifications back into pick_month's shape, keyed by gamekey.

    Malformed entries are dropped, never guessed at. A kept stamp that no
    longer parses loses its date and keeps its verdict: the order then
    counts as current-plan and ranks oldest, the same calls an unreadable
    live stamp gets.
    """
    facts = {}
    for gamekey, kept in (cache or {}).items():
        if not isinstance(kept, dict) or "choice" not in kept:
            continue
        raw = kept.get("created")
        facts[str(gamekey)] = {
            "gamekey": str(gamekey),
            "choice": bool(kept["choice"]),
            "created": order_created({"created": raw}) if raw else None,
            "human_name": str(kept.get("human_name") or ""),
        }
    return facts


def extract_tpks(order: dict) -> list[dict]:
    """An order's third-party keys (TPKs), one dict per key a game can hand out."""
    order = order or {}
    gamekey = str(order.get("gamekey") or "")
    found = []
    for tpk in ((order.get("tpkd_dict") or {}).get("all_tpks")) or []:
        try:
            key_index = int(tpk.get("key_index") or 0)
        except (TypeError, ValueError):
            key_index = 0
        found.append({
            "gamekey": gamekey,
            "title": str(tpk.get("human_name") or "(untitled)"),
            "machine_name": str(tpk.get("machine_name") or ""),
            "key_type": str(tpk.get("key_type") or "").strip().lower(),
            "key_index": key_index,
            "revealed_key": tpk.get("redeemed_key_val") or None,
            "expired": bool(tpk.get("is_expired")),
            "expiry_date": str(tpk.get("expiry_date") or ""),
        })
    return found


def tpk_state(tpk: dict) -> str:
    """Where one key stands before a run touches it: revealed, expired or waiting."""
    if (tpk or {}).get("revealed_key"):
        return "revealed"
    if (tpk or {}).get("expired"):
        return "expired"
    return "waiting"


def plan_reveals(tpks: list) -> list[dict]:
    """The waiting keys to reveal: unrevealed, unexpired, one per game and platform.

    A game offered as a Steam key or an Epic grant (a platform-choice title)
    keeps its Steam variant and drops the Epic one: a key can be activated,
    gifted or traded later, a keyless grant cannot be taken back. The same
    variant listed twice is revealed once.
    """
    steam_games = {(t["gamekey"], BaseClaimer._normalize_title(t["title"]))
                   for t in tpks or [] if "_choice_steam" in (t.get("machine_name") or "")}
    planned, seen = [], set()
    for tpk in tpks or []:
        if tpk_state(tpk) != "waiting":
            continue
        game = (tpk["gamekey"], BaseClaimer._normalize_title(tpk["title"]))
        machine = tpk.get("machine_name") or ""
        if "_choice_epic" in machine and game in steam_games:
            continue
        variant = game + (tpk.get("key_type") or "",)
        if ("_choice_steam" in machine or "_choice_epic" in machine) and variant in seen:
            continue
        seen.add(variant)
        planned.append(tpk)
    return planned


def owned_in_plan(planned: list, owned_titles) -> list:
    """The planned Steam keys whose game is already in your Steam library.

    `owned_titles` holds normalized titles (BaseClaimer._normalize_title
    form), as the Steam check hands them over. Only Steam keys can be in a
    Steam library; every other key in the plan is untouched.
    """
    owned = set(owned_titles or set())
    return [tpk for tpk in planned or []
            if (tpk.get("key_type") or "") == "steam"
            and BaseClaimer._normalize_title(tpk.get("title") or "") in owned]


# A Choice key entry's name ends in _<platform>, in front of the word
# "choice" (or Humble's real-world typo "hoice"): chantsofsennaar_choice_steam,
# deeprockgalactic_survivor_choice_steam. A single regional or functional
# word can sit between the game's own name and that ending — row, naeu,
# europe, playtest — and is not part of the identifier the choosing call
# expects: mightmushroom_playtest_choice_steam is chosen as "mightmushroom".
_CHOICE_SUFFIX_RE = re.compile(r"^(.+)_c?hoice_([a-z][a-z_]*)$")
_CHOICE_MODIFIERS = frozenset({"row", "naeu", "europe", "playtest"})


def short_identifier(machine_name: str) -> str | None:
    """The identifier the choosing call expects for one key entry, or None.

    None for a name that is not Choice content at all: such an entry is
    never chosen, and is not a game in a month's lineup either. The exact
    identifier normally comes from the month's page (month_lineup); this
    derivation is the fallback for when the page could not be read, and
    resolves the one ambiguity — a game's own name can contain underscores
    too — by stripping only the modifier words Humble is known to insert.
    """
    match = _CHOICE_SUFFIX_RE.match((machine_name or "").strip().lower())
    if not match:
        return None
    base = match.group(1)
    head, _, last = base.rpartition("_")
    if head and last in _CHOICE_MODIFIERS:
        return head
    return base


def lineup_identifiers(lineup: list) -> dict:
    """Key-entry name -> the page identifier of its game, for a whole lineup."""
    identifiers = {}
    for game in lineup or []:
        for tpkd in game.get("tpks") or []:
            identifiers[tpkd["machine_name"]] = game["identifier"]
    return identifiers


def choose_identifiers(planned: list) -> list[str]:
    """The identifiers to choose for a plan, one per game, in plan order.

    A game that has no key entry yet carries the identifier its month's
    page gave it; a game that has one gives up the short form of its key
    entry's name. Games with neither are not Choice content and are left
    out: there is nothing to choose for them.
    """
    identifiers = []
    for tpk in planned or []:
        identifier = tpk.get("identifier") or short_identifier(tpk.get("machine_name") or "")
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def membership_slug(order: dict) -> str | None:
    """The /membership/<slug> path for an order's month, or None.

    The product's own choice_url wins when Humble provides one; otherwise
    the slug is the product's machine name in kebab case with the Choice
    suffix stripped (october_2026_choice -> october-2026).
    """
    product = (order or {}).get("product") or {}
    url = str(product.get("choice_url") or "").strip()
    if url:
        return url.strip("/").rsplit("/", 1)[-1] or None
    machine = str(product.get("machine_name") or "").lower()
    for suffix in ("_choice_storefront", "_choice"):
        if machine.endswith(suffix):
            machine = machine[: -len(suffix)]
            break
    return machine.replace("_", "-") or None


def _embedded_choice_options(page: str) -> dict | None:
    """The contentChoiceOptions object a membership page carries in its data.

    The page embeds its month as JSON inside a script block; the object is
    lifted out by matching its braces, because the block around it is not
    JSON as a whole. None when the marker or a readable object is absent.
    """
    marker = page.find('"contentChoiceOptions"')
    if marker < 0:
        return None
    start = page.find("{", marker)
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(page)):
        char = page[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    blob = json.loads(page[start:index + 1])
                except ValueError:
                    return None
                return blob if isinstance(blob, dict) else None
    return None


def month_lineup(page: str | None, product_machine: str = "") -> list | None:
    """A Choice month's games, from its membership page's own data.

    One entry per game: its page identifier (what the choosing call
    expects), its title, and the key entries it would hand out, in the
    page's display order. Items that are not games (a month's coupon, a
    discount) never appear: their key entries' names are not Choice
    content, which is also what keeps them unclaimable here. None when
    the page's data cannot be read or belongs to another month, so the
    caller can say plainly that the lineup went unchecked.
    """
    if not page:
        return None
    blob = _embedded_choice_options(page)
    if blob is None:
        return None
    if product_machine and str(blob.get("productMachineName") or "") != product_machine:
        return None
    data = blob.get("contentChoiceData") or {}
    game_data = data.get("game_data") or {}
    if not isinstance(game_data, dict):
        return None
    lineup = []
    for identifier in data.get("display_order") or list(game_data):
        entry = game_data.get(identifier) or {}
        tpks = []
        for tpkd in entry.get("tpkds") or []:
            machine = str(tpkd.get("machine_name") or "")
            key_type = str(tpkd.get("key_type") or "").strip().lower()
            if short_identifier(machine) is None or key_type == "generic":
                continue
            if tpkd.get("visible") is False:
                continue
            tpks.append({
                "machine_name": machine,
                "key_type": key_type,
                "expired": bool(tpkd.get("is_expired")),
                "expiry_date": str(tpkd.get("expiration_date|datetime") or ""),
            })
        if not tpks:
            continue
        lineup.append({
            "identifier": str(identifier),
            "title": str(entry.get("title") or identifier),
            "tpks": tpks,
        })
    return lineup or None


def unchosen_tpks(order: dict, lineup: list) -> list[dict]:
    """Lineup games the order carries no key entry for yet, as waiting entries.

    Shaped like extract_tpks output so the rest of the run treats them the
    same, plus the page identifier the choosing call needs and an
    "unchosen" mark: choosing one is what makes Humble create its real
    key entry. A game lands here when it was never selected on the site —
    the order alone cannot see it at all.
    """
    present = {tpk["machine_name"] for tpk in extract_tpks(order)}
    gamekey = str((order or {}).get("gamekey") or "")
    missing = []
    for game in lineup or []:
        for tpkd in game.get("tpks") or []:
            if tpkd["machine_name"] in present:
                continue
            missing.append({
                "gamekey": gamekey,
                "title": game["title"],
                "machine_name": tpkd["machine_name"],
                "key_type": tpkd["key_type"],
                "key_index": 0,
                "revealed_key": None,
                "expired": tpkd["expired"],
                "expiry_date": tpkd["expiry_date"],
                "identifier": game["identifier"],
                "unchosen": True,
            })
    return missing


def reveal_outcome(http_status: int, payload) -> str:
    """What a redeemkey answer means: revealed, already, blocked or failed.

    "Blocked" is a 401/403: the session died or the anti-bot check answered,
    and no other key in this run would fare better, so the run stops there.
    """
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("success") is True:
        return "revealed"
    if http_status in (401, 403):
        return "blocked"
    said = json.dumps(payload).lower()
    if "already" in said:
        return "already"
    return "failed"


def choose_outcome(http_status: int, payload) -> bool:
    """True when the month's content counts as chosen: Humble said so, or it already was."""
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("success") is True:
        return True
    said = json.dumps(payload).lower()
    return "already" in said or "chosen" in said


class HumbleClaimer(BaseClaimer):
    store_name = "humble"

    # ------------------------------------------------------------------
    # Humble's API, asked from inside the page
    # ------------------------------------------------------------------

    async def _fetch_raw(self, url: str, form: list | None = None) -> tuple[int, str]:
        """One request to humblebundle.com from the open page; (HTTP status, raw body)."""
        js = FETCH_JS % (
            json.dumps("POST" if form else "GET"),
            json.dumps(url),
            json.dumps(form) if form else "null",
        )
        try:
            raw = await self.page.evaluate(js, await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("Humble request to %s failed: %s", url, e)
            return 0, ""
        if answer.get("error"):
            logger.debug("Humble request to %s failed in the page: %s", url, answer["error"])
        return int(answer.get("status") or 0), str(answer.get("body") or "")

    async def _fetch(self, url: str, form: list | None = None) -> tuple[int, object]:
        """One request to humblebundle.com from the open page; (HTTP status, JSON or None)."""
        status, body = await self._fetch_raw(url, form)
        try:
            payload = json.loads(body) if body else None
        except (TypeError, ValueError):
            payload = None
        return status, payload

    async def _fetch_text(self, url: str) -> tuple[int, str]:
        """One GET from the open page, body unparsed: for pages, not API answers."""
        return await self._fetch_raw(url)

    async def _signed_in(self) -> bool:
        """Ask the order list: it answers 200 signed in and 401 signed out."""
        status, payload = await self._fetch(API_ORDERS)
        if status == 200 and isinstance(payload, list):
            return True
        logger.debug("Order list answered %s, not a signed-in session.", status)
        return False

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login_form(self) -> dict:
        try:
            raw = await self.page.evaluate(LOGIN_FORM_JS)
            return json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("Could not read the sign-in page: %s", e)
            return {}

    async def _fill_code(self) -> bool:
        """One authenticator code from HB_OTP_KEY into the code box, when there is one."""
        if not cfg.hb_otp_key:
            return False
        self._last_totp = await self._fresh_totp(cfg.hb_otp_key, self._last_totp)
        try:
            filled = await self.page.evaluate(FILL_CODE_JS % json.dumps(self._last_totp))
        except Exception as e:
            logger.debug("Could not type the authenticator code: %s", e)
            return False
        if filled:
            await self.sleep(5)
        return bool(filled)

    async def _ensure_logged_in(self) -> bool:
        """Signed in to Humble: by itself with HB_EMAIL / HB_PASSWORD, else you over VNC."""
        await self.page.get(URL_KEYS)
        await self.sleep(4)
        if await self._human_challenge_present():
            if not await self._wait_out_challenge("Humble Bundle", store_key=self.store_name):
                return False
        if await self._signed_in():
            self.log_signed_in(account_key(cfg.hb_email, None))
            return True

        if cfg.hb_email and cfg.hb_password:
            logger.info("Signing in to Humble Bundle as %s…", account_key(cfg.hb_email, None))
            await self.page.get(URL_LOGIN)
            await self.sleep(4)
            try:
                sent = await self.page.evaluate(
                    FILL_LOGIN_JS % (json.dumps(cfg.hb_email), json.dumps(cfg.hb_password)))
            except Exception as e:
                logger.debug("Could not fill the sign-in form: %s", e)
                sent = False
            if sent:
                await self.sleep(5)
                codes = 0
                for _ in range(10):
                    if await self._signed_in():
                        self.log_signed_in(account_key(cfg.hb_email, None))
                        return True
                    form = await self._login_form()
                    # A code box means the password went through and a second step is left.
                    if form.get("code"):
                        if codes < OTP_KEY_ATTEMPTS and await self._fill_code():
                            codes += 1
                            continue
                        break
                    if not form.get("password"):
                        break
                    await self.sleep(3)
            else:
                logger.debug("The sign-in form was not on %s", await self._page_brief())

        msg = self._vnc_notice(
            "Humble Bundle: login needs you",
            "The automated sign-in did not finish. Open the browser and sign in to Humble Bundle; "
            "a code Humble e-mails you (its Guard check) is typed there too.",
        )
        if await self._wait_for_vnc_login(self._signed_in, custom_msg=msg, store_key=self.store_name):
            self.log_signed_in(account_key(cfg.hb_email, None))
            return True
        return False

    # ------------------------------------------------------------------
    # Choice months
    # ------------------------------------------------------------------

    async def _open_order(self, gamekey: str) -> dict | None:
        """One order in full, keys included; None when Humble will not hand it over."""
        status, order = await self._fetch(API_ORDER.format(gamekey=gamekey))
        if status != 200 or not isinstance(order, dict):
            logger.debug("Order %s answered %s, skipped.", gamekey, status)
            return None
        return order

    def _facts_cache_path(self) -> Path:
        """Where order classifications are kept: the data folder, by the database."""
        return cfg.browser_dir.parent / "humble-order-facts.json"

    def _read_facts_cache(self) -> dict:
        """Classifications kept from earlier runs; an unreadable file means none kept."""
        try:
            data = json.loads(self._facts_cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return facts_from_cache(data if isinstance(data, dict) else {})

    def _write_facts_cache(self, facts: list) -> None:
        """Keep this run's classifications for the next one.

        A failed write costs the next run its speed, never its pick, so it
        is noted at debug level and the run carries on.
        """
        try:
            path = self._facts_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(facts_to_cache(facts), indent=1, sort_keys=True),
                            encoding="utf-8")
        except OSError:
            logger.debug("The order classifications could not be kept for the next run.")

    async def _latest_choice_order(self) -> dict | None:
        """The one Choice month this run works on, opened in full, or None.

        The order list is only gamekeys, so classifying an order means
        opening it — once. Classifications are kept between runs
        (facts_to_cache), and a run opens in full only the orders it has
        never seen, plus the month it picks. Should the list itself ever
        carry an entry's product and date, those are used first and the
        kept answers stay a fallback.
        """
        status, payload = await self._fetch(API_ORDERS)
        if status != 200 or not isinstance(payload, list):
            logger.warning("Humble's order list answered %s, so no month can be read this run.", status)
            return None
        known = self._read_facts_cache()
        facts_by_key, opened = {}, {}
        unclassified = 0
        for entry in payload:
            summary = summary_facts(entry)
            gamekey = summary["gamekey"]
            if not gamekey:
                continue
            fact = summary if summary["choice"] is not None and summary["created"] is not None else None
            if fact is None:
                fact = known.get(gamekey)
            if fact is None:
                order = await self._open_order(gamekey)
                if order is None:
                    continue
                opened[gamekey] = order
                fact = detail_facts(order, gamekey)
                unclassified += 1
            facts_by_key[gamekey] = fact
        facts = list(facts_by_key.values())
        if unclassified:
            logger.info("Opened %d order(s) to classify them; the answers are kept, "
                        "so later runs open only orders they have never seen.", unclassified)
        self._write_facts_cache(facts)
        latest, legacy, older = pick_month(facts)
        for fact in legacy:
            product = fact["human_name"] or "Humble Choice (legacy month)"
            self.notify_games.append({"title": product, "url": URL_MEMBERSHIP,
                                      "status": "skipped:legacy-month"})
        if legacy:
            logger.info("%d pick-limited month(s) from before February 2022 left alone: "
                        "their unpicked games are yours to choose.", len(legacy))
        if latest is None:
            return None
        if older:
            logger.info("Only the most recent Choice month is checked: '%s'. "
                        "%d older month(s) left alone.",
                        latest["human_name"] or "the newest month", older)
        order = opened.get(latest["gamekey"])
        if order is None:
            order = await self._open_order(latest["gamekey"])
        if order is None:
            logger.warning("The most recent Choice month could not be opened, so nothing "
                           "is claimed this run.")
        return order

    async def _remember(self, tpk: dict, db_status: str, key: str | None = None,
                        platform: str = "", overwrite: bool = True) -> None:
        """Save one key's standing in the database, under the account that claimed it."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="humble", user=self.user or "unknown",
                game_id=f"{tpk['gamekey']}/{tpk['machine_name']}/{tpk['key_index']}",
                title=tpk["title"], url=URL_KEYS, status=db_status,
            )
            if created or overwrite:
                obj.status = db_status
            if key:
                obj.code = key
                obj.extra = json.dumps({"external_store": platform or tpk["key_type"]})
            await session.commit()

    async def _choose_month(self, order: dict, planned: list) -> bool:
        """Choose a plan's games in one call, by the identifiers the page itself uses.

        The identifiers are the short forms (choose_identifiers), the shape
        the site's own claim button sends. For a current-plan month the
        content is normally chosen already, and Humble saying so counts as
        chosen too: revealing the keys is the step that decides, this one
        only opens it — and for a game that had no key entry yet, it is
        what makes Humble create one.
        """
        identifiers = choose_identifiers(planned)
        if not identifiers:
            return True
        form = [["gamekey", str(order.get("gamekey") or "")], ["parent_identifier", "initial"]]
        form += [["chosen_identifiers[]", identifier] for identifier in identifiers]
        status, payload = await self._fetch(API_CHOOSE, form)
        chosen = choose_outcome(status, payload)
        logger.debug("Choosing the month answered %s (%s).", status, "chosen" if chosen else "not chosen")
        return chosen

    async def _month_lineup(self, order: dict) -> list | None:
        """The picked month's full lineup, from its membership page.

        The order alone cannot see a game that was never selected on the
        site — it has no key entry there — so the month's games are read
        from the page that offers them. None when the page or its data
        cannot be read, with the gap said out loud: a run that cannot see
        the lineup must not sound complete.
        """
        slug = membership_slug(order)
        if not slug:
            logger.warning("The month's membership page is not known, so games you have not "
                           "selected on the site yet, if any, are invisible this run.")
            return None
        status, page = await self._fetch_text(f"/membership/{slug}")
        product = (order.get("product") or {}).get("machine_name") or ""
        lineup = month_lineup(page, str(product)) if status == 200 else None
        if lineup is None:
            logger.warning("The month's lineup could not be read from its membership page, "
                           "so games you have not selected on the site yet, if any, are "
                           "invisible this run.")
        return lineup

    async def _materialize(self, order: dict, planned: list) -> list:
        """Swap planned entries that had no key entry for the real ones, after choosing.

        Choosing is what makes Humble create a game's key entry, so the
        order is read once more and the planned stand-ins are matched to
        real entries by name. A game Humble still hands no entry for is
        named and left for the next run; entries that were real all along
        pass through untouched.
        """
        fresh = await self._open_order(str(order.get("gamekey") or ""))
        if fresh is None:
            logger.warning("The order could not be re-read after choosing, so the games that "
                           "were just chosen wait for the next run.")
            return [tpk for tpk in planned if not tpk.get("unchosen")]
        real = {tpk["machine_name"]: tpk for tpk in extract_tpks(fresh)}
        settled = []
        for tpk in planned:
            if not tpk.get("unchosen"):
                settled.append(tpk)
                continue
            match = real.get(tpk["machine_name"])
            if match is None or tpk_state(match) != "waiting":
                logger.warning("'%s' was chosen, but Humble handed over no key entry for it; "
                               "it waits for the next run.", tpk["title"])
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                          "status": "skipped:choose-failed"})
                continue
            settled.append(match)
        return settled

    async def _reveal(self, tpk: dict) -> str:
        """Reveal one key and save it. Returns the run's standing: claimed, blocked or failed."""
        form = [["keytype", tpk["machine_name"]], ["key", tpk["gamekey"]],
                ["keyindex", str(tpk["key_index"])]]
        status, payload = await self._fetch(API_REDEEM, form)
        outcome = reveal_outcome(status, payload if isinstance(payload, dict) else {})
        if outcome == "blocked":
            logger.warning("Humble turned the reveal of '%s' away (HTTP %s): the session or its "
                           "anti-bot check, so the rest of this run waits.", tpk["title"], status)
            return "blocked"
        if outcome == "already":
            # Revealed between reading the order and asking, by you or another run.
            logger.info("'%s' was already revealed.", tpk["title"])
            await self._remember(tpk, "existed", overwrite=False)
            self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "existed"})
            return "claimed"
        if outcome != "revealed":
            logger.warning("Humble did not reveal the key for '%s' (HTTP %s).", tpk["title"], status)
            await self._remember(tpk, "failed")
            self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "failed:reveal-failed"})
            return "failed"

        key = str((payload or {}).get("key") or "")
        platform = tpk["key_type"] or "humble"
        if key:
            logger.info("✓ Revealed '%s' (%s key %s).", tpk["title"], platform, mask_code(key))
        else:
            # A keyless grant (an Epic variant linked to your account) has no key to hand over.
            logger.info("✓ Claimed '%s' (%s, no key: it goes to the linked account).", tpk["title"], platform)
        await self._remember(tpk, "claimed", key=key or None, platform=platform)
        self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "claimed"})
        return "claimed"

    async def _steam_owned_titles(self, candidates: list) -> set | None:
        """Normalized titles of waiting Steam keys your Steam library already holds.

        One check for the whole run, before the month is claimed from, on
        Steam's own profile and sign-in. The candidates are every waiting
        key entry the run can see, including games that have no entry in
        the order yet: one of those counts as owned exactly the same, and
        is then never chosen at all. An empty set when the check is off
        (HB_CHECK_STEAM=false) or no Steam key is waiting; None when the
        check is on but your library could not be read, which the caller
        treats as "reveal no Steam key this run".
        """
        if not cfg.hb_check_steam:
            return set()
        titles = sorted({tpk["title"] for tpk in plan_reveals(candidates)
                         if (tpk.get("key_type") or "") == "steam"})
        if not titles:
            return set()
        from src.stores.steam import steam_owned_verdicts

        verdicts = await steam_owned_verdicts(titles)
        if verdicts is None:
            logger.warning("Your Steam library could not be checked, so no Steam key will be "
                           "revealed this run; the keys wait, unrevealed, for the next one.")
            return None
        owned = {BaseClaimer._normalize_title(title)
                 for title, is_owned in verdicts.items() if is_owned}
        logger.info("Steam library check: %d of %d waiting Steam key(s) already in your "
                    "library, left unrevealed.", len(owned), len(titles))
        return owned

    async def _claim_month(self, order: dict, steam_owned: set | None,
                           lineup: list | None = None) -> None:
        """Settle one Choice month: record what is settled, reveal what is waiting.

        The month's games are the order's key entries plus the lineup games
        that have no entry yet (unchosen_tpks): a game never selected on
        the site is as much this month's game as any other, and the Steam
        check and the plan treat it the same. One already in your Steam
        library is never chosen, so no key entry is ever created for it.
        """
        tpks = extract_tpks(order)
        if lineup:
            # The page's identifiers are exact; stamp them onto the key
            # entries they belong to so choosing never has to derive one.
            identifiers = lineup_identifiers(lineup)
            for tpk in tpks:
                if tpk["machine_name"] in identifiers:
                    tpk["identifier"] = identifiers[tpk["machine_name"]]
            tpks += unchosen_tpks(order, lineup)
        for tpk in tpks:
            state = tpk_state(tpk)
            if state == "revealed":
                # Yours already, and a row the bot made for it keeps its key and queue place.
                await self._remember(tpk, "existed", overwrite=False)
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "existed"})
            elif state == "expired":
                logger.info("The key for '%s' expired on %s, before it was ever revealed.",
                            tpk["title"], tpk["expiry_date"] or "its publisher's date")
                await self._remember(tpk, "skipped:expired")
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "skipped:expired"})

        planned = plan_reveals(tpks)
        if planned and steam_owned is not None:
            # Already on Steam: the key stays unrevealed, so it can still be gifted.
            held = owned_in_plan(planned, steam_owned)
            for tpk in held:
                logger.info("'%s' is already in your Steam library, so its key stays unrevealed.",
                            tpk["title"])
                await self._remember(tpk, "skipped:owned-on-steam")
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                          "status": "skipped:owned-on-steam"})
            drop = {id(tpk) for tpk in held}
            planned = [tpk for tpk in planned if id(tpk) not in drop]
        elif planned and steam_owned is None and not cfg.dryrun:
            # The library could not be read: no Steam key is revealed on a guess.
            held = [tpk for tpk in planned if (tpk.get("key_type") or "") == "steam"]
            for tpk in held:
                logger.warning("'%s' waits for the next run: your Steam library could not be "
                               "checked this run.", tpk["title"])
                await self._remember(tpk, "skipped:steam-unchecked")
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                          "status": "skipped:steam-unchecked"})
            drop = {id(tpk) for tpk in held}
            planned = [tpk for tpk in planned if id(tpk) not in drop]

        if not planned:
            logger.debug("Nothing waiting to be revealed in this month.")
            return
        logger.info("🎮 %d Humble Choice game(s) to reveal.", len(planned))

        if cfg.dryrun:
            for tpk in planned:
                logger.info("DRYRUN – skipped revealing '%s'.", tpk["title"])
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "available (dry run)"})
            return

        await self._choose_month(order, planned)
        if any(tpk.get("unchosen") for tpk in planned):
            # Choosing created their key entries; re-read the order so the
            # reveals work on the real entries, not the stand-ins.
            planned = await self._materialize(order, planned)
            if not planned:
                return
        for index, tpk in enumerate(planned):
            if await self._reveal(tpk) == "blocked":
                left = len(planned) - index - 1
                if left:
                    logger.warning("%d more key(s) wait for the next run.", left)
                break

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point for the Humble Bundle claiming flow."""
        logger.debug("Starting Humble Bundle claiming flow")
        try:
            await self.start_browser()
            if not await self._ensure_logged_in():
                logger.error("Aborting Humble Bundle claim flow due to login failure.")
                needs_you(self.store_name, 0, "game")
                return

            order = await self._latest_choice_order()
            if order is None:
                logger.info("No Humble Choice month is waiting to be claimed.")
                return
            lineup = await self._month_lineup(order)
            candidates = extract_tpks(order)
            if lineup:
                candidates += unchosen_tpks(order, lineup)
            steam_owned = await self._steam_owned_titles(candidates)
            await self._claim_month(order, steam_owned, lineup)

        except Exception as exc:
            logger.exception("Fatal error")
            if cfg.notify_errors:
                await self.notify(f"humble failed: {exc}")
        finally:
            await self.close_browser()


async def claim_humble() -> dict:
    """Convenience entry point."""
    claimer = HumbleClaimer()
    await claimer.run()
    return {"store": "Humble Bundle", "user": claimer.user, "games": claimer.notify_games}
