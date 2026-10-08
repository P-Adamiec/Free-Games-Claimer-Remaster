"""GamerPower store module.

Fetches giveaways from GamerPower API, skips any games we've already
claimed in other stores (Steam, Epic, GOG), and processes indirect
redemption sites (Fanatical, Alienware Arena, Itch.io, IndieGala)
according to user configuration.
"""
import json
import re
import httpx
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import nodriver as uc

from sqlalchemy import select

from src.core.claimer import BaseClaimer, OTP_KEY_ATTEMPTS, mask_account
from src.core.config import cfg
from src.core.database import async_session, ClaimedGame, get_or_create
from src.core.run_state import needs_you, waits_for_nobody
from src.core.selection import is_store_active
from src.core.url_security import url_has_allowed_host
import logging
from src.core.claimer import filenamify

logger = logging.getLogger("fgc.gamerpower")

GAMERPOWER_API_URL = "https://www.gamerpower.com/api/giveaways"

# Full games and Early Access are worth claiming; DLC needs a per-game account (GP_CLAIM_DLC).
CLAIMABLE_TYPES = ("game", "early access")

# Host to store key. Order matters only for readability, hosts never overlap.
_STORE_HOSTS = (
    ("steam", "store.steampowered.com"),
    ("epic", "epicgames.com"),
    ("gog", "gog.com"),
    ("microsoft", "xbox.com"),
    ("microsoft", "microsoft.com"),
    ("fanatical", "fanatical.com"),
    ("alienware", "alienwarearena.com"),
    ("itchio", "itch.io"),
    ("indiegala", "indiegala.com"),
    ("ubisoft", "ubisoft.com"),
)

# Stores with a module of their own, which finds these giveaways without GamerPower's help.
COVERED_ELSEWHERE = {"ubisoft": "ubisoft"}

# Only consulted when the resolved address gives nothing away.
_INSTRUCTION_HINTS = (
    ("indiegala", "indiegala"),
    ("alienware", "alienware"),
    ("fanatical", "fanatical"),
    ("itchio", "itch.io"),
)


def wanted_types(claim_dlc: bool) -> set[str]:
    """Giveaway types worth processing, lowercased for comparison."""
    types = set(CLAIMABLE_TYPES)
    if claim_dlc:
        types.add("dlc")
    return types


def is_wanted(entry: dict, claim_dlc: bool) -> bool:
    """True when this giveaway is a type we try to claim."""
    return str((entry or {}).get("type") or "").strip().lower() in wanted_types(claim_dlc)


def classify_target(final_url: str, instructions: str = "") -> str:
    """Which store a giveaway ends at, by host first and instructions only as a fallback."""
    for store, host in _STORE_HOSTS:
        if url_has_allowed_host(final_url, host, allow_subdomains=True):
            return store

    text = (instructions or "").lower()
    for store, hint in _INSTRUCTION_HINTS:
        if hint in text:
            return store
    return "unknown"

# Two-factor screens differ per site, so the code box is found by what it is, not by its name.
OTP_FIELD = "[data-otp-field]"

OTP_STATE_JS = r"""
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const hint = /code|otp|totp|2fa|two.?factor|verif/i;
        const fields = [...document.querySelectorAll('input')].filter(vis)
            .filter(i => ['text', 'tel', 'number', ''].includes((i.type || '').toLowerCase()));
        const labelled = fields.filter(i => hint.test(
            [i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.autocomplete].join(' ')));
        const body = document.body ? (document.body.innerText || '') : '';
        const target = labelled[0] || (fields.length === 1 ? fields[0] : null);
        if (target) target.setAttribute('data-otp-field', '1');
        return JSON.stringify({
            labelled: labelled.length,
            visibleTextFields: fields.length,
            hasPassword: !!document.querySelector('input[type="password"]'),
            talksAboutIt: /two.?factor|verification code|authenticator/i.test(body),
        });
    })()
"""


# Fanatical signs in through a modal opened from the header; /en/login is a 404, and the
# first visible text box on any page is the search field, not the email one.
FAN_SIGNED_OUT_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        let token = '';
        try { token = JSON.parse(localStorage.getItem('bsauth') || '{}').token || ''; } catch (e) {}
        // A header that has not rendered yet shows no Sign in button either, so the site's token must be there too.
        return !token || [...document.querySelectorAll('button, a')].filter(vis)
            .some(b => /^sign in$/i.test((b.textContent || '').trim()));
    })()
"""

FAN_OPEN_LOGIN_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const b = [...document.querySelectorAll('button, a')].filter(vis)
            .find(x => /^sign in$/i.test((x.textContent || '').trim()));
        if (!b) return false;
        b.click();
        return true;
    })()
"""

FAN_MARK_FIELDS_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const mail = document.querySelector('#emailInput');
        const pass = [...document.querySelectorAll('#passwordInput, input[type="password"]')].find(vis);
        if (mail) mail.setAttribute('data-fgc-mail', '1');
        if (pass) pass.setAttribute('data-fgc-pass', '1');
        return !!mail && !!pass;
    })()
"""

# The header carries a Sign in button too, so the submit is looked up inside the modal.
FAN_SUBMIT_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const pass = document.querySelector('[data-fgc-pass]');
        if (!pass) return false;
        let scope = pass;
        for (let i = 0; i < 6 && scope.parentElement; i++) {
            scope = scope.parentElement;
            if (scope.tagName === 'FORM' || /modal|dialog/i.test(String(scope.className))) break;
        }
        const b = [...scope.querySelectorAll('button')].filter(vis)
            .find(x => /^sign in$/i.test((x.textContent || '').trim()));
        if (!b) return false;
        b.click();
        return true;
    })()
"""


# IndieGala's notification prompt and cookie banner sit over its login form until answered.
IG_DISMISS_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        for (const b of [...document.querySelectorAll('button')].filter(vis)) {
            if (/^(don't allow|i agree)$/i.test((b.textContent || '').trim())) b.click();
        }
    })()
"""

# IndieGala's login fields carry no name or id, so the visible pair is marked and then typed into.
IG_MARK_FIELDS_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const pass = [...document.querySelectorAll('input[type="password"]')].find(vis);
        if (!pass) return false;
        let scope = pass.parentElement, mail = null;
        for (let i = 0; i < 6 && scope && !mail; i++, scope = scope.parentElement) {
            mail = [...scope.querySelectorAll('input[type="text"], input[type="email"]')].find(vis) || null;
        }
        if (!mail) return false;
        mail.setAttribute('data-fgc-mail', '1');
        pass.setAttribute('data-fgc-pass', '1');
        return true;
    })()
"""

# Owned means no ADD TO LIBRARY and "just go to your Library"; every page says "Search in your library".
IG_OWNED_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const offered = [...document.querySelectorAll('a, button')].filter(vis)
            .some(b => /^add to library$/i.test((b.innerText || '').trim()));
        return !offered && /just go to your library/i.test(document.body ? document.body.innerText : '');
    })()
"""

# Marked so the claim is a real click on the one visible ADD TO LIBRARY, not a scripted one.
IG_MARK_CLAIM_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const b = [...document.querySelectorAll('a, button')].filter(vis)
            .find(x => /^add to library$/i.test((x.innerText || '').trim()));
        if (!b) return false;
        b.setAttribute('data-fgc-claim', '1');
        return true;
    })()
"""

# The login page always carries a reCAPTCHA checkbox; unsolved until its answer box holds a token.
IG_CAPTCHA_UNSOLVED_JS = """
    (() => {
        const frame = [...document.querySelectorAll('iframe')].find(f => {
            const r = f.getBoundingClientRect();
            return /recaptcha\\/(api2|enterprise)\\/anchor/.test(f.src || '') && !/[?&]size=invisible/.test(f.src || '')
                && r.width >= 60 && r.height >= 40;
        });
        if (!frame) return false;
        let box = frame.parentElement, answer = null;
        for (let i = 0; i < 4 && box && !answer; i++, box = box.parentElement) {
            answer = box.querySelector('textarea.g-recaptcha-response, textarea[name="g-recaptcha-response"]');
        }
        if (answer && answer.value) return false;
        frame.scrollIntoView({block: 'center'});
        return true;
    })()
"""

# The page carries other submit buttons (the prompt's ×), so LOGIN is looked up beside the password.
IG_SUBMIT_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        let scope = document.querySelector('[data-fgc-pass]');
        for (let i = 0; i < 6 && scope; i++, scope = scope.parentElement) {
            const b = [...scope.querySelectorAll('button, input[type="submit"]')].filter(vis)
                .find(x => /^log ?in$/i.test((x.textContent || x.value || '').trim()));
            if (b) { b.click(); return true; }
        }
        return false;
    })()
"""


def indiegala_game_id(url: str) -> str:
    """IndieGala's own identifier for a freebie: the slug in `freebies.indiegala.com/<slug>`."""
    parsed = urlparse(str(url or ""))
    slug = parsed.path.strip("/").split("/")[0].lower()
    if not url_has_allowed_host(str(url or ""), "indiegala.com", allow_subdomains=True) or not slug:
        return ""
    return slug


def fanatical_game_id(url: str) -> str:
    """Fanatical's own identifier for a giveaway: the slug behind /game/ or /giveaway/."""
    match = re.search(r"/(?:game|giveaway|bundle)/([a-z0-9-]+)", str(url or "").lower())
    return match.group(1) if match else ""


# The site's token ("bsauth") plus the headers its own api client sends on every call (api/index.js); no token, no call.
_FAN_HEADERS = """
        const auth = JSON.parse(localStorage.getItem('bsauth') || '{}');
        if (!auth.token) return JSON.stringify({status: 0});
        const h = {authorization: auth.token, accept: 'application/json'};
        try {
            const a = JSON.parse(localStorage.getItem('bsanonymous') || '{}');
            if (a.id) h.anonid = String(a.id);
        } catch (e) {}
        if (window.fingerprint) h['x-fan-fp'] = String(window.fingerprint);
        if (window.version) h['X-Fan-Client-Version'] = String(window.version);
"""

FAN_ORDERS_JS = """
    (async () => {
        try {""" + _FAN_HEADERS + """
            const r = await fetch('/api/user/orders', {headers: h});
            return JSON.stringify({status: r.status, orders: r.ok ? await r.json() : null});
        } catch (e) {
            return JSON.stringify({status: -1, error: String(e).slice(0, 120)});
        }
    })()
"""

# The order list carries only names; the ids a reveal needs and the platform come from the order itself.
FAN_ORDER_JS = """
    (async () => {
        try {""" + _FAN_HEADERS + """
            const r = await fetch('/api/user/orders/' + encodeURIComponent(__OID__), {headers: h});
            return JSON.stringify({status: r.status, order: r.ok ? await r.json() : null});
        } catch (e) {
            return JSON.stringify({status: -1, error: String(e).slice(0, 120)});
        }
    })()
"""

# The call Fanatical's own "Reveal key" button makes (key-reveal-service.js), for one order item only.
FAN_REVEAL_JS = """
    (async () => {
        try {""" + _FAN_HEADERS + """
            if (auth.email_confirmed === false) return JSON.stringify({status: 0, reason: 'email-unconfirmed'});
            h['content-type'] = 'application/json';
            // The site's store holds the raw "bsatok" string and sends it whole, not its inner value.
            const atok = localStorage.getItem('bsatok') || '';
            const r = await fetch('/api/user/orders/redeem', {method: 'POST', headers: h,
                body: JSON.stringify({...__PAYLOAD__, atok})});
            const text = await r.text();
            let data = null;
            try { data = JSON.parse(text); } catch (e) {}
            return JSON.stringify({status: r.status, data, error: r.ok ? '' : text.slice(0, 200)});
        } catch (e) {
            return JSON.stringify({status: -1, error: String(e).slice(0, 120)});
        }
    })()
"""

# Your newsletter state as the site reads it on every page load (redux/ducks/initial-load.js refreshAuth).
FAN_NEWSLETTER_JS = """
    (async () => {
        try {""" + _FAN_HEADERS + """
            const r = await fetch('/api/user/refresh-auth', {headers: h});
            if (!r.ok) return JSON.stringify({status: r.status});
            const u = await r.json();
            return JSON.stringify({status: r.status, subscribed: !!(u.email_newsletter || u.email_newsletter_pending)});
        } catch (e) {
            return JSON.stringify({status: -1, error: String(e).slice(0, 120)});
        }
    })()
"""

# What the account page's "unsubscribe from all marketing emails" button sends (redux/ducks/email-subscribe.js).
FAN_UNSUBSCRIBE_JS = """
    (async () => {
        try {""" + _FAN_HEADERS + """
            h['content-type'] = 'application/json';
            const r = await fetch('/api/crm/frontunsubscribe', {method: 'POST', headers: h, body: '{}'});
            return JSON.stringify({status: r.status});
        } catch (e) {
            return JSON.stringify({status: -1, error: String(e).slice(0, 120)});
        }
    })()
"""

# The giveaway's steps by their markup (iconContainer--<type> and a .done tick), not their English wording.
FAN_GIVEAWAY_JS = """
    (() => {
        const steps = [...document.querySelectorAll('.GiveawaySteps__step')].map(s => {
            const icon = s.querySelector('[class*="GiveawaySteps__step__iconContainer--"]');
            const type = icon ? (icon.className.match(/iconContainer--(\\S+)/) || [])[1] || '' : '';
            return {type, done: !!s.querySelector('.GiveawaySteps__step__status .done'),
                    text: (s.innerText || '').replace(/\\s+/g, ' ').trim()};
        });
        const soldOut = !!document.querySelector('.product-giveaway-newsletter-required.sold-out');
        return JSON.stringify({steps, soldOut});
    })()
"""

# The cart's total as shown and which checkout control it offers: the upsell link or the pay button itself.
FAN_CHECKOUT_STATE_JS = """
    (() => {
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const total = [...document.querySelectorAll('.checkout-total .summary-number')].find(vis);
        const pay = [...document.querySelectorAll('button#api-button')].find(vis);
        const proceed = [...document.querySelectorAll('a')].filter(vis)
            .some(x => /^proceed to checkout$/i.test((x.innerText || '').trim()));
        return JSON.stringify({
            path: location.href,  // the full address, so fanatical_page() can check the host
            total: total ? (total.innerText || '').trim() : null,
            pay: pay ? (pay.innerText || '').replace(/\\s+/g, ' ').trim() : '',
            payDisabled: !!(pay && pay.disabled),
            proceed,
        });
    })()
"""

# Each marks the one visible control for nodriver's click: the giveaway button, the cart's upsell link, the pay button.
_FAN_MARK = """
    (() => {
        document.querySelectorAll('[data-fgc-fan]').forEach(e => e.removeAttribute('data-fgc-fan'));
        const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const el = __FIND__;
        if (!el || el.disabled) return false;
        el.setAttribute('data-fgc-fan', '1');
        return true;
    })()
"""
# The same button class sits on every add-to-cart, so only the one in the giveaway's steps box counts.
FAN_MARK_MAIN_JS = _FAN_MARK.replace(
    "__FIND__", "[...document.querySelectorAll('.GiveawaySteps__steps__action button')].find(vis)")
FAN_MARK_PROCEED_JS = _FAN_MARK.replace(
    "__FIND__", "[...document.querySelectorAll('a')].filter(vis)"
    ".find(x => /^proceed to checkout$/i.test((x.innerText || '').trim()))")
# "Proceed To Checkout" without an upsell, "Skip and Proceed To Checkout" after it; never the upsell's own ADD.
FAN_MARK_PAY_JS = _FAN_MARK.replace(
    "__FIND__", "[...document.querySelectorAll('button#api-button')].find(vis)")

# Where you connect Steam to Fanatical once; the bot never does it for you.
FAN_LINKED_ACCOUNTS_URL = "https://www.fanatical.com/en/account/linked-accounts"

STEAM_KEY_RE = re.compile(r"^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$")
OTHER_DRM = ("epic", "gog", "uplay", "ubisoft", "origin", "ea app", "battle.net", "microsoft", "xbox", "rockstar")


def find_fanatical_item(orders, slug: str, title: str, sole_item: bool = False) -> dict | None:
    """This giveaway's order item as {"oid", "bid", "status", "item"}, a COMPLETE order first, never by position.

    `sole_item` takes a one-item order whatever its name: only for the order the bot's own checkout just made.
    """
    want = BaseClaimer._normalize_title(title)
    first = None
    for order in orders if isinstance(orders, list) else []:
        if not isinstance(order, dict):
            continue
        items = [item for item in order.get("items") or [] if isinstance(item, dict)]
        for item in items:
            text = json.dumps(item).lower()
            named = want and BaseClaimer._normalize_title(item.get("name") or "") == want
            if (slug and f'"{slug.lower()}"' in text) or named or (sole_item and len(items) == 1):
                # Top-level items reveal with bid null; bundles are left to your library.
                found = {"oid": order.get("_id"), "bid": None, "status": str(order.get("status") or ""), "item": item}
                if found["status"] == "COMPLETE":
                    return found
                first = first or found
    return first


def fanatical_next_step(steps: list) -> tuple[str, int]:
    """What the giveaway's steps need next: ready, steam, newsletter, human or wait, with that step's position."""
    if not steps:
        return "wait", -1
    # An unlinked Steam account blocks the claim wherever it sits, so nothing else is ticked or asked first.
    for i, step in enumerate(steps):
        if step.get("type") == "steamConnect" and not step.get("done"):
            return "steam", i
    for i, step in enumerate(steps):
        if not step.get("done"):
            return ("newsletter" if step.get("type") == "newsletter" else "human"), i
    return "ready", -1


def fanatical_should_unsubscribe(before: bool | None, after: bool | None) -> bool:
    """Unsubscribe only when you were not subscribed before the claim and are now; unknown means no."""
    return before is False and after is True


def fanatical_receipt_order(path: str) -> str:
    """The order id Fanatical puts on its receipt page (merchantReference), empty when there is none."""
    query = parse_qs(urlparse(str(path or "")).query)
    ref = (query.get("merchantReference") or [""])[0]
    return ref if re.fullmatch(r"[0-9a-f]{24}", ref) else ""


def fanatical_receipt_authorised(path: str) -> bool:
    """True when the receipt says the order went through; a refused or cancelled one gets a receipt page too."""
    result = (parse_qs(urlparse(str(path or "")).query).get("authResult") or [""])[0]
    return result.upper() in ("AUTHORISED", "AUTHORIZED")


def fanatical_page(url: str, page: str) -> bool:
    """True when `url` is Fanatical's `page` itself ("cart", "receipt"), not a product whose slug starts with it."""
    url = str(url or "")
    # A full address must be Fanatical's own: a receipt elsewhere proves nothing.
    if "://" in url and not url_has_allowed_host(url, "fanatical.com", allow_subdomains=True):
        return False
    return bool(re.search(rf"/{page}/?$", urlparse(url).path))


def fanatical_price(text) -> float | None:
    """A price as Fanatical prints it ("€0.00", "0,00 zł", "$1,299.99"), None when there is no number."""
    match = re.search(r"\d[\d.,\s]*", str(text or ""))
    if not match:
        return None
    number = re.sub(r"\s", "", match.group(0)).rstrip(".,")
    cents = re.match(r"^(.*?)[.,](\d{2})$", number)
    if cents:
        return float(f"{re.sub(r'[.,]', '', cents.group(1)) or 0}.{cents.group(2)}")
    return float(re.sub(r"[.,]", "", number))


def steam_key_in(value) -> str:
    """The first Steam-shaped key anywhere in a Fanatical answer, empty when there is none."""
    if isinstance(value, str):
        return value.strip().upper() if STEAM_KEY_RE.match(value.strip().upper()) else ""
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    for child in children:
        found = steam_key_in(child)
        if found:
            return found
    return ""


def fanatical_item_is_steam(item: dict) -> bool:
    """A key is sent to Steam only when the item says Steam, or names no platform at all."""
    drm = (item or {}).get("drm")
    # Order items list every platform with true or false, so "steam": false must not count.
    if isinstance(drm, dict):
        return bool(drm.get("steam"))
    if isinstance(drm, list):
        return "steam" in [str(d).lower() for d in drm]
    text = json.dumps(item or {}).lower()
    return "steam" in text or not any(other in text for other in OTHER_DRM)


# An owned itch.io game carries a purchase banner; a page you do not own carries none.
ITCH_OWNED_JS = """
    (() => !!document.querySelector('.purchase_banner, .ownership_reason'))()
"""


def download_only_status(first_time: bool) -> str:
    """What the summary says about a giveaway itch.io only hands out as a file."""
    # Said plainly once, then parked under a "skipped" status the summary filter hides.
    return "download only, nothing to claim 📥" if first_time else "skipped:download-only"


def itch_game_id(url: str) -> str:
    """Itch.io's own identifier for a game: creator host plus slug, e.g. `dev.itch.io/game`."""
    parsed = urlparse(str(url or ""))
    slug = parsed.path.strip("/").split("/")[0]
    if not parsed.netloc or not slug:
        return ""
    return f"{parsed.netloc.lower()}/{slug.lower()}"


def needs_otp(state: dict) -> bool:
    """True when the page is asking for an authenticator code rather than a password."""
    state = state or {}
    if state.get("labelled"):
        return True
    # No named field: only trust a page that says so and offers exactly one box to type in.
    return bool(state.get("talksAboutIt")
                and state.get("visibleTextFields") == 1
                and not state.get("hasPassword"))


def login_help_message(label: str, code_screen: bool, tried_backup: bool = False) -> str:
    """What to tell the user when a side store will not sign in on its own."""
    if not code_screen:
        return f"{label} did not accept the automated sign-in. Open the browser and finish it."

    lines = [f"{label} is asking for your authenticator code. Open the browser and type it."]
    if tried_backup:
        lines.append("A recovery code was spent on this attempt and did not get through either.")
    return " ".join(lines)


# One claimer serves four sites, so what the person reads is not what the run state counts by.
SIDE_STORE_KEYS = {"Itch.io": "itchio", "Fanatical": "fanatical",
                   "IndieGala": "indiegala", "Alienware Arena": "alienware"}


def side_store_key(label: str) -> str:
    """The store key behind a label shown to the user."""
    return SIDE_STORE_KEYS.get(label, (label or "").lower())


# Some giveaway hosts answer a bare client with a different redirect than a browser gets.
_ROUTE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/120.0.0.0 Safari/537.36")

# A giveaway that lands on a storefront banner instead of a product page cannot be claimed.
_PRODUCT_MARKERS = {"steam": ("/app/", "/sub/"), "epic": ("/p/", "/bundles/")}


def is_product_page(store: str, final_url: str) -> bool:
    """False when a giveaway lands on a storefront banner instead of a page you can claim."""
    markers = _PRODUCT_MARKERS.get(store)
    return True if not markers else any(marker in (final_url or "") for marker in markers)


def _clean_title(title: str) -> str:
    """Drop the "(Steam) Key Giveaway" tails GamerPower puts on its titles."""
    for pattern in (r'(?i)\s*\(\s*steam\s*\)\s*(?:key\s*)?giveaway\s*$',
                    r'(?i)\s*(?:steam\s*)?key\s*giveaway\s*$',
                    r'(?i)\s*giveaway\s*$',
                    r'(?i)\s*\(\s*steam\s*\)\s*key\s*$',
                    r'(?i)\s*steam\s*key\s*$'):
        title = re.sub(pattern, '', title)
    return title.strip()


def counts_as_done(store: str, status: str) -> bool:
    """True for a row whose game needs nothing more: claimed or existed, also once Steam took a Fanatical key."""
    status = str(status or "")
    # Steam writes its key outcome over a Fanatical row ("claimed and activated", "failed:key-region").
    return status == "existed" or status.startswith("claimed") or str(store or "") == "fanatical"


async def _claimed_titles() -> set:
    """Every title already claimed anywhere, so the same game is not chased twice."""
    titles = set()
    async with async_session() as session:
        result = await session.execute(select(ClaimedGame))
        for db_game in result.scalars().all():
            if counts_as_done(db_game.store, db_game.status):
                titles.add(BaseClaimer._normalize_title(db_game.title))
    return titles


async def _resolve_target(game: dict) -> tuple[str, str]:
    """Follow a giveaway's redirect and work out which store it ends at. No browser."""
    giveaway_url = game.get("giveaway_url", "")
    final_url = giveaway_url.lower()
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": _ROUTE_UA},
                                     timeout=20) as client:
            res = await client.get(giveaway_url)
            final_url = str(res.url).lower()
    except Exception as e:
        logger.debug("Failed to pre-resolve URL %s: %s", giveaway_url, e)

    instructions = (game.get("instructions", "") or "").lower()
    target_store = classify_target(final_url, instructions)
    if target_store != "unknown" and classify_target(final_url) == "unknown":
        logger.debug("[GamerPower] '%s' routed to %s by its instructions, the URL gave nothing.",
                     game.get("title", "Unknown"), target_store)
    return target_store, final_url


async def discover_giveaways() -> dict:
    """Find GamerPower's giveaways and sort them by the store they end at. Never raises.

    This is the only part that talks to GamerPower: the stores themselves do the claiming.
    """
    try:
        logger.debug("Fetching giveaways from GamerPower API")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(GAMERPOWER_API_URL)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list):
            logger.debug("GamerPower returned non-list data")
            return {}

        # Filter before resolving redirects: every entry kept costs one extra HTTP request.
        kept = [item for item in data if is_wanted(item, cfg.gp_claim_dlc)]
        if len(data) - len(kept):
            logger.debug("Skipped %d giveaway(s) of an unwanted type (GP_CLAIM_DLC=%s).",
                         len(data) - len(kept), cfg.gp_claim_dlc)

        games = [{
            "title": _clean_title(item.get("title", "Unknown")),
            "url": item.get("open_giveaway_url", ""),
            "giveaway_url": item.get("open_giveaway_url", ""),
            # Carried through so routing can fall back on them, see classify_target().
            "instructions": item.get("instructions", "") or "",
            "type": item.get("type", "") or "",
            "platforms": item.get("platforms", "") or "",
        } for item in kept]

        logger.debug("GamerPower API returned %d giveaway(s)", len(games))
        db_titles = await _claimed_titles()
        unique_gp = []
        for gp in games:
            norm = BaseClaimer._normalize_title(gp["title"])
            if any(s in norm or norm in s for s in db_titles):
                logger.debug("GamerPower duplicate (already processed globally): %s", gp["title"])
            else:
                unique_gp.append(gp)

        if not unique_gp:
            logger.info("No unique GamerPower giveaways found.")
            return {}

        links = [f"  • [bold cyan]{g['title']}[/bold cyan] 🔗 {g.get('giveaway_url', '')}" for g in unique_gp]
        logger.info("🎮 [bold magenta]GamerPower: %d extra game(s):[/bold magenta]\n%s",
                    len(unique_gp), "\n".join(links))

        routed: dict = {}
        for game in unique_gp:
            store, final_url = await _resolve_target(game)
            game["final_url"] = final_url
            if not is_product_page(store, final_url):
                logger.info("⏭️ [GamerPower] '%s' → %s URL is not a game page (%s), skipping",
                            game["title"], store.title(), final_url)
                continue
            if store in COVERED_ELSEWHERE:
                logger.info("⏭️ [GamerPower] '%s' → %s, already covered by the '%s' store, skipping",
                            game["title"], store.title(), COVERED_ELSEWHERE[store])
                continue
            routed.setdefault(store, []).append(game)
        logger.debug("GamerPower routing: %s",
                     {store: len(items) for store, items in sorted(routed.items())})
        return routed
    except Exception:
        logger.exception("Could not read the GamerPower giveaways")
        return {}


class GamerPowerClaimer(BaseClaimer):
    store_name = "gamerpower"
    # The browser profile keeps its original folder name so existing side-store logins survive.
    profile_name = "base"

    def __init__(self) -> None:
        super().__init__()
        self.user = "GamerPower"
        self._fanatical_games = []
        # Itch.io signs in once per run, not once per giveaway.
        self._itch_session_ok = False
        self._ig_session_noted = False
        self._fan_session_noted = False

    async def run(self, routed: dict | None = None) -> None:
        """Claim the giveaways that land on sites with no store module of their own."""
        routed = routed or {}
        try:
            work = []
            for store in ("fanatical", "alienware", "itchio", "indiegala", "unknown"):
                label, _ = self._side_store(store)
                for game in routed.get(store, []):
                    if self._side_store_selected(store):
                        work.append((store, game))
                    else:
                        logger.info("⏭️ [GamerPower] '%s' → %s, not part of this run, skipping",
                                    game.get("title", "Unknown"), label)
            if not work:
                return

            # One browser for all of them: these sites are claimed page by page, not in batches.
            await self.start_browser(force_headful=True)
            for store, game in work:
                # A site whose prompt nobody answered is left alone: opening its sign-in page
                # once per giveaway would cost hours and lower the session's standing there.
                if waits_for_nobody(store):
                    logger.info("⏭️ [GamerPower] '%s' → %s is waiting for you, skipping",
                                game.get("title", "Unknown"), self._side_store(store)[0])
                    needs_you(store)
                    continue
                await self._process_side_store(store, game)
        except Exception as exc:
            logger.exception("Fatal error in GamerPower")
            if cfg.notify_errors:
                await self.notify(f"gamerpower failed: {exc}")
        finally:
            # Summary notifications deferred to main.py
            await self.close_browser()

    @staticmethod
    def _side_store_selected(target_store: str) -> bool:
        """Side stores need an account, so each one runs only when STORES names it."""
        if target_store == "unknown":
            # Opening a site nobody mapped is not supported yet, whatever GP_UNKNOWN_STORES says.
            if cfg.gp_unknown_stores:
                logger.warning("Opening unknown sites is not supported yet, GP_UNKNOWN_STORES stays off.")
            return False
        return is_store_active(target_store)

    def _side_store(self, target_store: str) -> tuple[str, object]:
        """Label and handler for a site with no store module of its own."""
        table = {
            "fanatical": ("Fanatical giveaway", self._claim_fanatical_game),
            "alienware": ("Alienware Arena", self._claim_alienware_game),
            "itchio": ("Itch.io giveaway", self._claim_itchio_game),
            "indiegala": ("IndieGala giveaway", self._claim_indiegala_game),
        }
        # No handler for an unknown site, all we can do is open it for a human.
        return table.get(target_store, ("Unknown site", None))

    async def _process_side_store(self, target_store: str, game: dict) -> None:
        """Claim one giveaway on a side site, in the browser this claimer already opened."""
        title = game.get("title", "Unknown")
        label, handler = self._side_store(target_store)
        reported = len(self.notify_games)

        try:
            if handler:
                logger.info("🎮 [GamerPower] '%s' → %s", title, label)
                await handler(game)
            else:
                domain = urlparse(game.get("final_url", "")).netloc.replace("www.", "")
                logger.info("❓ [GamerPower] '%s' → Unknown site (%s). Opening for manual review via VNC.",
                            title, domain)
                await self.page.get(game.get("giveaway_url", ""))
                await self.sleep(10)
        except Exception:
            logger.exception("[GamerPower] Error processing '%s'", title)

        # The summary lists these sites under GamerPower, so NOTIFY_SKIP_STORES is applied per site here.
        if not cfg.store_notify_enabled(target_store):
            logger.debug("Notifications silenced for '%s', leaving '%s' out of the summary.", target_store, title)
            del self.notify_games[reported:]

    async def _itch_logged_in(self) -> bool:
        """Signed in when itch.io offers a logout link and no login link. Verified live."""
        try:
            return bool(await self.page.evaluate("""
                (() => {
                    const hrefs = [...document.querySelectorAll('a[href]')]
                        .map(a => a.getAttribute('href') || '');
                    return hrefs.some(h => h.includes('/logout')) && !hrefs.some(h => h.includes('/login'));
                })()
            """))
        except Exception as e:
            logger.debug("[Itch.io] Sign-in check failed: %s", e)
            return False

    async def _itch_logged_in_after_load(self, seconds: int = 12) -> bool:
        """Signed in, asked again until itch.io has finished loading; a page still loading has no logout link yet."""
        waited = 0
        while True:
            if await self._itch_logged_in():
                if waited:
                    logger.debug("[Itch.io] Signed in once the page finished loading (%ss).", waited)
                return True
            if waited >= seconds:
                logger.debug("[Itch.io] Still no sign-in after %ss of loading.", waited)
                return False
            await self.sleep(2)
            waited += 2

    async def _ig_logged_in(self) -> bool:
        """Signed in when IndieGala offers a way out and none in. Verified live signed out."""
        try:
            raw = await self.page.evaluate("""
                JSON.stringify((() => {
                    const hrefs = [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href') || '');
                    const labels = [...document.querySelectorAll('a, button')]
                        .map(b => (b.textContent || '').trim().toLowerCase());
                    return {
                        logout: hrefs.some(h => h.includes('logout')),
                        login: hrefs.some(h => h.includes('/login'))
                            || labels.some(t => t === 'login' || t === 'log in' || t === 'sign in')
                    };
                })())
            """)
            state = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[IndieGala] Sign-in check failed: %s", e)
            return False
        # Signed out always offers a way in, on indiegala.com and on freebies.indiegala.com alike.
        return bool(state.get("logout")) or not state.get("login")

    def _log_side_signed_in(self, label: str, account: str | None) -> None:
        """The line every store prints. `log_signed_in()` would also overwrite `self.user`,
        which this claimer keeps as the database key for all its side stores."""
        self.logger.info("🔓 [bold green]Signed in as:[/bold green] %s (%s)",
                         mask_account(account) or "unknown", label)

    async def _resubmit_login(self, label: str, email: str, password: str) -> bool:
        """Send the sign-in form again. A human check interrupts the first attempt, it does not undo it."""
        js_email = json.dumps(email)
        js_password = json.dumps(password)
        try:
            sent = await self.page.evaluate(f'''
                (() => {{
                    const vis = el => {{ const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }};
                    const inputs = [...document.querySelectorAll('input')].filter(vis);
                    const user = inputs.find(i => /email|user|login/i.test(i.name + ' ' + i.id + ' ' + i.type));
                    const pass = inputs.find(i => (i.type || '').toLowerCase() === 'password');
                    if (!user || !pass) return false;
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                    setter.call(user, {js_email});
                    user.dispatchEvent(new Event("input", {{bubbles: true}}));
                    setter.call(pass, {js_password});
                    pass.dispatchEvent(new Event("input", {{bubbles: true}}));
                    const submit = document.querySelector('button[type="submit"], input[type="submit"]')
                        || [...document.querySelectorAll('button')].filter(vis)
                            .find(b => /log ?in|sign ?in/i.test(b.textContent || ''));
                    if (!submit) return false;
                    submit.click();
                    return true;
                }})()
            ''')
        except Exception as e:
            logger.debug("[%s] Could not send the sign-in form again: %s", label, e)
            return False
        if sent:
            logger.info("[%s] The check is done, sending your sign-in again.", label)
            await self.sleep(6)
        return bool(sent)

    async def _resubmit_when_form_returns(self, label: str, email: str, password: str) -> bool:
        """Wait for the sign-in form to come back after a human check, then send it.

        The check disappears a moment before the page reloads, and one look at that moment
        finds no form at all.
        """
        for _ in range(6):
            if await self._resubmit_login(label, email, password):
                return True
            await self.sleep(3)
        logger.debug("[%s] No sign-in form came back after the check.", label)
        return False

    async def _confirm_side_login(self, label: str, check_fn, backup_codes: list | None = None,
                                 backup_file: str = "", otp_key: str | None = None,
                                 credentials: tuple | None = None) -> bool:
        """Finish a side-store login: answer a code screen if one shows, else hand over via VNC."""
        if await check_fn():
            return True

        # A captcha is not a login failure the bot can retype its way out of: Fanatical answers
        # a correct password with "we just need to check that you're a real person".
        if await self._human_challenge_present():
            logger.warning("[%s] The site is showing a human check, handing over.", label)
            if await self._wait_out_challenge(label, store_key=side_store_key(label)):
                if await check_fn():
                    return True
                # The check interrupted a sign-in that was already sent, so send it again
                # rather than asking you to type a password the bot already has.
                if credentials and await self._resubmit_when_form_returns(label, *credentials)                         and await check_fn():
                    return True

        state = {}
        tried_backup = False
        try:
            raw = await self.page.evaluate(OTP_STATE_JS)
            state = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[%s] Could not read the login screen: %s", label, e)

        if needs_otp(state):
            logger.debug("[%s] Two-factor screen detected: %s", label, state)
            for attempt in range(OTP_KEY_ATTEMPTS if otp_key else 0):
                if attempt:
                    logger.warning("[%s] The code was not accepted, trying once more with a fresh one.", label)
                if await self._fill_totp(label, otp_key) and await check_fn():
                    return True
                if not await self._code_screen_still_up(label):
                    break
            if backup_codes:
                tried_backup = await self._fill_backup_code(label, backup_codes, backup_file)
                if tried_backup and await check_fn():
                    return True
            elif not otp_key:
                logger.info("[%s] The site is asking for a two-factor code, handing over to you.", label)

        code_screen = needs_otp(state)
        msg = self._vnc_notice(
            f"{label}: 2FA code needed" if code_screen else f"{label}: login needs you",
            login_help_message(label, code_screen, tried_backup))
        if await self._wait_for_vnc_login(check_fn, custom_msg=msg, store_key=side_store_key(label)):
            return True
        logger.warning("[%s] Still not signed in, skipping this giveaway.", label)
        needs_you(side_store_key(label))
        return False

    async def _type_otp(self, label: str, code: str) -> bool:
        """Type a code into whatever box OTP_STATE_JS marked, then press the site's submit button."""
        try:
            field = await self.page.select(OTP_FIELD, timeout=8)
            if not field:
                return False
            await field.click()
            await self.sleep(0.4)
            # A refused code can stay in the box, and the next one would be typed after it.
            await field.clear_input()
            await field.send_keys(code)
            await self.sleep(0.6)
            pressed = await self.page.evaluate("""
                (() => {
                    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                    const words = /^(log ?in|verify|continue|submit|sign ?in|authenticate)$/i;
                    const named = x => words.test(((x.innerText || x.value || '')).trim());
                    const buttons = root => [...root.querySelectorAll('button, input[type="submit"]')].filter(vis);
                    // The code box's own form first: Fanatical's header has a "Sign in" button that closes the code screen.
                    const form = (document.querySelector('[data-otp-field]') || document.body).closest('form');
                    const own = form ? buttons(form) : [];
                    const b = own.find(named) || own.find(x => x.type === 'submit') || buttons(document).find(named);
                    if (b) b.click();
                    return b ? ((b.innerText || b.value || '').trim().slice(0, 30)) : '';
                })()
            """)
            # nodriver hands back a RemoteObject, not '', when nothing was found.
            if isinstance(pressed, str) and pressed:
                logger.debug("[%s] Code sent with the '%s' button.", label, pressed)
            else:
                logger.debug("[%s] Code typed, but no submit button was found.", label)
            await self.sleep(6)
            return True
        except Exception as e:
            logger.debug("[%s] Could not enter the two-factor code: %s", label, e)
            return False

    async def _code_screen_still_up(self, label: str) -> bool:
        """True when the code box is still there, which means the code was turned down."""
        try:
            raw = await self.page.evaluate(OTP_STATE_JS)
            return needs_otp(json.loads(raw) if isinstance(raw, str) else {})
        except Exception as e:
            logger.debug("[%s] Could not re-read the code screen: %s", label, e)
            return False

    async def _fill_totp(self, label: str, otp_key: str) -> bool:
        """Auto-enter the authenticator (TOTP) code from the store's own secret."""
        logger.debug("[%s] Entering the two-factor code from your authenticator secret.", label)
        self._last_totp = await self._fresh_totp(otp_key, self._last_totp)
        return await self._type_otp(label, self._last_totp)

    async def _fill_backup_code(self, label: str, codes: list, used_name: str) -> bool:
        """Spend one recovery code, the way epic.py and gog.py do: first unused, then remember it."""
        code = self._next_unused_code(codes, used_name)
        if not code:
            logger.warning("[%s] Every recovery code has been used already.", label)
            return False

        # Some sites keep recovery behind its own link next to the authenticator field.
        await self.page.evaluate("""
            (() => {
                const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const link = [...document.querySelectorAll('a, button')].filter(vis)
                    .find(x => /recovery|backup/i.test((x.textContent || '').trim()));
                if (link) link.click();
            })()
        """)
        await self.sleep(3)
        try:
            raw = await self.page.evaluate(OTP_STATE_JS)
            if not needs_otp(json.loads(raw) if isinstance(raw, str) else {}):
                logger.debug("[%s] No code box after opening the recovery screen.", label)
                return False
        except Exception as e:
            logger.debug("[%s] Could not read the recovery screen: %s", label, e)
            return False
        if not await self._type_otp(label, code.replace("-", "").replace(" ", "")):
            return False
        self._mark_code_used(code, used_name, codes)
        return True

    def _no_credentials_notice(self, label: str, prefix: str) -> str:
        """The VNC prompt for a side store with no password set, named after the site, not this module."""
        return self._vnc_notice(
            f"{label}: login needs you",
            f"No {prefix}_EMAIL / {prefix}_PASSWORD set. Open the browser and sign in to {label}.",
        )

    async def _clear_challenge(self, label: str) -> bool:
        """Let a captcha that shows up mid-claim be solved, instead of failing quietly."""
        if not await self._human_challenge_present():
            return True
        logger.warning("[%s] A human check appeared during the claim.", label)
        return await self._wait_out_challenge(label, store_key=side_store_key(label))

    async def _fanatical_signed_in(self) -> bool:
        """Signed in when the site's own token is there and no Sign in control is left on the page."""
        try:
            return not bool(await self.page.evaluate(FAN_SIGNED_OUT_JS))
        except Exception as e:
            logger.debug("[Fanatical] Sign-in check failed: %s", e)
            return False

    async def _fanatical_login(self, email: str, password: str) -> None:
        """Open the header modal and fill it. Fanatical has no login page, /en/login is a 404."""
        if not await self.page.evaluate(FAN_OPEN_LOGIN_JS):
            logger.debug("[Fanatical] No Sign in button to open the login modal.")
            return
        await self.sleep(5)
        if not await self.page.evaluate(FAN_MARK_FIELDS_JS):
            logger.debug("[Fanatical] The login modal did not offer both fields.")
            return
        for selector, value in (("[data-fgc-mail]", email), ("[data-fgc-pass]", password)):
            field = await self.page.select(selector, timeout=8)
            if not field:
                return
            await field.click()
            await self.sleep(0.4)
            await field.send_keys(value)
            await self.sleep(0.6)
        await self.page.evaluate(FAN_SUBMIT_JS)
        await self.sleep(8)

    async def _fanatical_orders(self) -> list | None:
        """Your Fanatical orders from the account API, None when the API cannot be read."""
        try:
            raw = await self.page.evaluate(FAN_ORDERS_JS, await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[Fanatical] Could not read the orders: %s", e)
            return None
        if answer.get("status") != 200 or not isinstance(answer.get("orders"), list):
            logger.debug("[Fanatical] Orders API answered %s %s", answer.get("status"), answer.get("error", ""))
            return None
        logger.debug("[Fanatical] %d order(s) on the account.", len(answer["orders"]))
        return answer["orders"]

    async def _fanatical_order_item(self, oid: str, game_id: str, title: str, ours: bool = False) -> dict | None:
        """The item read from its own order, with the ids and platform the order list leaves out."""
        try:
            raw = await self.page.evaluate(FAN_ORDER_JS.replace("__OID__", json.dumps(str(oid or ""))),
                                           await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[Fanatical] Could not read the order for '%s': %s", title, e)
            return None
        detail = find_fanatical_item([answer.get("order")], game_id, title, sole_item=ours)
        logger.debug("[Fanatical] Order for '%s' answered %s, item found: %s %s", title, answer.get("status"),
                     bool(detail), detail["status"] if detail else "")
        return detail

    async def _fanatical_reveal_key(self, found: dict, title: str) -> str:
        """This one item's key, revealed the way Fanatical's own button does; empty when it cannot."""
        key = steam_key_in(found["item"])
        if key:
            return key
        item = found["item"]
        payload = {"oid": found.get("oid"), "bid": found.get("bid"), "pid": item.get("_id"),
                   "serialId": item.get("serialId"), "iid": item.get("iid")}
        try:
            raw = await self.page.evaluate(FAN_REVEAL_JS.replace("__PAYLOAD__", json.dumps(payload)),
                                           await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[Fanatical] Could not reveal the key for '%s': %s", title, e)
            return ""
        data = answer.get("data")
        key = steam_key_in(data)
        # Fanatical's error text may quote the account, so its e-mail is masked like every other one in the log.
        detail = answer.get("reason") or answer.get("error") or ""
        detail = re.sub(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@", r"\1***@", detail)
        logger.debug("[Fanatical] Key reveal for '%s' answered %s %s, Steam key found: %s",
                     title, answer.get("status"), detail[:120], bool(key))
        if answer.get("reason") == "email-unconfirmed":
            logger.info("[Fanatical] Confirm your e-mail address on Fanatical to reveal the key for '%s'.", title)
        elif isinstance(data, dict) and data.get("key") == "email":
            logger.info("[Fanatical] Fanatical e-mailed you a code to reveal the key for '%s'.", title)
        return key

    async def _fanatical_path(self) -> str:
        """The open page's full address; checks on it must keep the host check."""
        try:
            return str(await self.page.evaluate("location.href") or "")
        except Exception as e:
            logger.debug("[Fanatical] Could not read the page address: %s", e)
            return ""

    async def _fanatical_on_receipt(self) -> bool:
        """True once the browser shows Fanatical's receipt, the end of every checkout."""
        return fanatical_page(await self._fanatical_path(), "receipt")

    async def _fanatical_giveaway(self) -> dict:
        """The giveaway's steps as [{"type", "done", "text"}] and whether its keys ran out."""
        try:
            state = json.loads(await self.page.evaluate(FAN_GIVEAWAY_JS) or "{}")
        except Exception as e:
            logger.debug("[Fanatical] Could not read the giveaway steps: %s", e)
            state = {}
        return {"steps": state.get("steps") or [], "soldOut": bool(state.get("soldOut"))}

    async def _fanatical_steps_done(self) -> bool:
        """True once every giveaway step carries its tick."""
        steps = (await self._fanatical_giveaway())["steps"]
        return bool(steps) and all(step.get("done") for step in steps)

    async def _fanatical_click(self, mark_js: str, what: str) -> bool:
        """nodriver's click on the one control `mark_js` tags, False when the page offers none."""
        try:
            if not await self.page.evaluate(mark_js):
                logger.debug("[Fanatical] No %s on the page.", what)
                return False
            button = await self.page.select("[data-fgc-fan]", timeout=8)
            await button.scroll_into_view()
            await self.sleep(0.8)
            await button.click()
            logger.debug("[Fanatical] Clicked %s.", what)
            return True
        except Exception as e:
            logger.debug("[Fanatical] Could not click %s: %s", what, e)
            return False

    async def _fanatical_finish_steps(self, title: str) -> str:
        """Tick the giveaway's steps, the newsletter here, the rest by you: ready, sold-out, steam, no-steps, steps."""
        ticked = seen = False
        for _ in range(10):
            state = await self._fanatical_giveaway()
            if state["soldOut"]:
                return "sold-out"
            steps = state["steps"]
            seen = seen or bool(steps)
            logger.debug("[Fanatical] Steps for '%s': %s", title, [(s.get("type"), s.get("done")) for s in steps])
            action, at = fanatical_next_step(steps)
            if action == "ready":
                return "ready"
            if action == "wait":
                # The steps render a moment after the page.
                await self.sleep(2)
                continue
            if action == "steam":
                # Connecting Steam is a one-time account setting done before, not something to wait for mid-claim.
                logger.warning("[Fanatical] '%s' needs your Steam account connected on Fanatical, at %s "
                               "(a limited Steam account does not count). Once it is linked, the next run claims it.",
                               title, FAN_LINKED_ACCOUNTS_URL)
                return "steam"
            if action == "newsletter":
                # The site's button toggles the consent, so a second click would take it back.
                if not ticked:
                    logger.info("[Fanatical] '%s' asks for the e-mail newsletter, subscribing.", title)
                    ticked = await self._fanatical_click(FAN_MARK_MAIN_JS, "newsletter step")
                await self.sleep(2)
                continue
            # A Steam wishlist or a partner link: only you can do those.
            step = steps[at].get("text") or steps[at].get("type")
            logger.warning("[Fanatical] '%s' needs a step from you: %s.", title, step)
            notice = self._vnc_notice("Fanatical: a giveaway step needs you",
                                      f"'{title}' asks you to: {step}. "
                                      "Do it in the browser, the bot claims the game after.")

            async def asked_step_done(at=at, kind=steps[at].get("type")) -> bool:
                now = (await self._fanatical_giveaway())["steps"]
                # The list can re-render, so the asked step is found by its type first and its position only after.
                same = [s for s in now if s.get("type") == kind] or now[at:at + 1]
                return bool(same) and all(s.get("done") for s in same)

            if not await self._wait_for_vnc_login(asked_step_done, custom_msg=notice, store_key="fanatical"):
                return "steps"
        if not seen:
            logger.debug("[Fanatical] '%s' shows no giveaway steps at %s.", title, await self._fanatical_path())
            return "no-steps"
        return "ready" if await self._fanatical_steps_done() else "steps"

    async def _fanatical_checkout(self, title: str) -> tuple[str, str]:
        """Check the free cart out as its page does: ("receipt", its address), else "not-free", "no-cart" or "stuck"."""
        cart_seen = free_seen = moved = paid = False
        for attempt in range(25):
            await self.sleep(3)
            try:
                state = json.loads(await self.page.evaluate(FAN_CHECKOUT_STATE_JS) or "{}")
            except Exception as e:
                logger.debug("[Fanatical] Could not read the checkout: %s", e)
                continue
            path = str(state.get("path") or "")
            if fanatical_page(path, "receipt"):
                return "receipt", path
            if not fanatical_page(path, "cart"):
                if moved:
                    # /billing or /payment wants details the bot never types.
                    logger.debug("[Fanatical] Checkout went to %s.", path)
                    return "stuck", ""
                if not cart_seen and attempt >= 5:
                    logger.debug("[Fanatical] Still on %s after the claim click.", path)
                    return "no-cart", ""
                continue
            cart_seen = True
            if state.get("total") is not None:
                total = fanatical_price(state["total"])
                logger.debug("[Fanatical] Cart total %s (%s).", state["total"], total)
                if total is None:
                    logger.warning("[Fanatical] Could not read your cart's total, so '%s' was left in it.", title)
                    return "not-free", ""
                if total != 0:
                    logger.warning("[Fanatical] Your cart costs %s, so '%s' was left in it.", state["total"], title)
                    return "not-free", ""
                free_seen = True
            if not free_seen or paid:
                continue
            if state.get("pay") and not state.get("payDisabled"):
                paid = moved = await self._fanatical_click(FAN_MARK_PAY_JS, state["pay"])
            elif state.get("proceed"):
                moved = await self._fanatical_click(FAN_MARK_PROCEED_JS, "Proceed to checkout") or moved
        return ("stuck" if cart_seen else "no-cart"), ""

    async def _fanatical_complete_order(self, game_id: str, title: str, oid: str = "",
                                        seconds: int = 50) -> dict | None:
        """This giveaway's item once its order is COMPLETE, read from the order itself; None when it is not by then."""
        waited, found, listed_complete, misses = 0, None, None, 0
        for pause in (0, 3, 5, 8, 13, 21):
            if waited + pause > seconds:
                break
            await self.sleep(pause)
            waited += pause
            # The receipt names the order; when it does not, or that order cannot be read, the list finds it by name.
            found = await self._fanatical_order_item(oid, game_id, title, ours=True) if oid else None
            if not found:
                found = find_fanatical_item(await self._fanatical_orders() or [], game_id, title)
                if found and found["status"] == "COMPLETE":
                    listed_complete = found
                    found = await self._fanatical_order_item(found["oid"], game_id, title)
                    misses += not found
            if found and found["status"] == "COMPLETE":
                return found
            if misses >= 2:
                break
        # The list said COMPLETE but the order itself would not load: claimed, the key stays in your library.
        if listed_complete:
            return listed_complete
        logger.debug("[Fanatical] Order for '%s' is %s.", title, found["status"] if found else "not on the account")
        return None

    async def _fanatical_newsletter(self, tries: int = 1) -> bool | None:
        """True when your account gets Fanatical's newsletter (or confirmation is pending), None when unreadable."""
        subscribed = None
        for attempt in range(tries):
            if attempt:
                await self.sleep(3)
            try:
                answer = json.loads(await self.page.evaluate(FAN_NEWSLETTER_JS, await_promise=True) or "{}")
            except Exception as e:
                logger.debug("[Fanatical] Could not read the newsletter state: %s", e)
                continue
            if answer.get("status") != 200:
                logger.debug("[Fanatical] Account read answered %s %s", answer.get("status"), answer.get("error", ""))
                continue
            subscribed = bool(answer.get("subscribed"))
            # The receipt page signs you up a moment after it opens, so a "no" is asked again.
            if subscribed:
                break
        return subscribed

    async def _fanatical_unsubscribe(self, title: str) -> None:
        """Take back the newsletter this claim signed you up for, the way the account page's own link does."""
        try:
            raw = await self.page.evaluate(FAN_UNSUBSCRIBE_JS, await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("[Fanatical] Newsletter unsubscribe failed: %s", e)
            answer = {}
        logger.debug("[Fanatical] Newsletter unsubscribe answered %s %s", answer.get("status"), answer.get("error", ""))
        # The receipt page can still sign you up just after this, so the answer is read a moment later.
        await self.sleep(3)
        if await self._fanatical_newsletter() is False:
            logger.info("[Fanatical] Unsubscribed from the newsletter again after '%s'.", title)
        else:
            logger.warning("[Fanatical] Could not unsubscribe from the newsletter, do it in your Fanatical account.")

    async def _remember_fanatical(self, game_id: str, title: str, url: str, status: str, steam_key: str = "") -> None:
        """Store a Fanatical outcome; "existed" never overwrites a row, so Steam's outcome for its key stays."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="fanatical", user=self.user,
                game_id=game_id, title=title, url=url, status=status,
            )
            if created or status != "existed":
                obj.status = status
            if steam_key:
                obj.code = steam_key
                obj.extra = json.dumps({"external_store": "steam"})
            await session.commit()

    async def _itch_owns_this(self) -> bool:
        """True when the open game page shows itch.io's own-this banner. Language independent."""
        try:
            return bool(await self.page.evaluate(ITCH_OWNED_JS))
        except Exception as e:
            logger.debug("[Itch.io] Ownership check failed: %s", e)
            return False

    async def _remember_itchio(self, game_id: str, title: str, url: str, status: str) -> bool:
        """Store an itch.io outcome. True when this run is the first to see that status."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="itchio", user=self.user,
                game_id=game_id, title=title, url=url, status=status,
            )
            first_time = created or obj.status != status
            obj.status = status
            await session.commit()
        return first_time

    async def _itch_run_claim(self, title: str) -> str:
        """Walk itch.io's claim chain. Returns "clicked", "download-only", "not-free" or "blocked"."""
        purchase = await self.page.evaluate("""
            (() => {
                const a = document.querySelector('a.buy_btn[href], a.button.buy_btn[href]');
                return a ? a.href : '';
            })()
        """)
        if not purchase:
            logger.warning("[Itch.io] '%s' has no claim button on its page.", title)
            return "blocked"

        await self.page.get(str(purchase))
        await self.sleep(5)
        if not await self._clear_challenge("Itch.io"):
            return "blocked"
        # Only a free or pay-what-you-want game offers the direct download link. Anything
        # else wants real money, and the bot has no business there.
        went_free = await self.page.evaluate("""
            (() => {
                const b = document.querySelector('a.direct_download_btn');
                if (!b) return false;
                b.click();
                return true;
            })()
        """)
        if not went_free:
            return "not-free"
        await self.sleep(6)

        # The download page carries the one control that puts the game in your library.
        clicked = await self.page.evaluate("""
            (() => {
                const clean = s => (s || '').replace(/[\\s]+/g, ' ').trim();
                const el = [...document.querySelectorAll('a, button')]
                    .filter(x => x.querySelectorAll('a, button').length === 0)
                    .find(x => /^claim( game)?$/i.test(clean(x.textContent)));
                if (!el) return false;
                el.click();
                return true;
            })()
        """)
        if not clicked:
            logger.debug("[Itch.io] '%s' offers a download but no claim control.", title)
            return "download-only"
        await self.sleep(7)
        return "clicked"

    async def _claim_fanatical_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("final_url") or game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)
        game_id = fanatical_game_id(url) or giveaway_url

        notify_game = {"title": f"{title} (Fanatical)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # The host alone is not enough: staying on the previous game's page would judge
            # this one by that page.
            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            # The cookie wall covers the header, so it goes first either way.
            await self.page.evaluate("""
                (() => {
                    const b = [...document.querySelectorAll('button, a')].find(x =>
                        (x.textContent || '').includes('Reject All Non-Essential') ||
                        (x.textContent || '').includes('Reject All'));
                    if (b) b.click();
                })()
            """)
            await self.sleep(2)

            # Every giveaway lists "Create or Sign in" as its first step, done or not, so only the header decides.
            if not await self._fanatical_signed_in():
                email = cfg.fanatical_email
                password = cfg.fanatical_password
                if email and password:
                    logger.info("[Fanatical] Logging in as %s…", mask_account(email))
                    await self._fanatical_login(email, password)
                    # Fanatical issues no recovery codes, so only the authenticator secret gets the bot past its code screen.
                    if not await self._confirm_side_login("Fanatical", self._fanatical_signed_in,
                                                          otp_key=cfg.fanatical_otp_key,
                                                          credentials=(email, password)):
                        return
                    self._log_side_signed_in("Fanatical", email)
                else:
                    logger.warning("[Fanatical] No credentials set (FANATICAL_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(
                            self._fanatical_signed_in, custom_msg=self._no_credentials_notice("Fanatical", "FANATICAL"),
                            store_key="fanatical"):
                        return
                self._fan_session_noted = True
            elif not self._fan_session_noted:
                self._log_side_signed_in("Fanatical", cfg.fanatical_email)
                self._fan_session_noted = True

            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            # Ownership comes from a COMPLETE order; one left INITIALISED by an unfinished checkout is not yours.
            orders = await self._fanatical_orders()
            if orders is not None:
                found = find_fanatical_item(orders, game_id, title)
                owned = bool(found) and found["status"] == "COMPLETE"
                if found and not owned:
                    logger.debug("[Fanatical] '%s' has an unfinished %s order, claiming again.", title, found["status"])
            else:
                # The giveaway page never says it is yours, so without the orders API ownership is unknown.
                logger.debug("[Fanatical] Orders could not be read, so '%s' is treated as not yet claimed.", title)
                owned = False
            if owned:
                logger.info("[Fanatical] '%s' already claimed.", title)
                if not cfg.dryrun:
                    await self._remember_fanatical(game_id, title, url, "existed")
                notify_game["status"] = "existed"
                return

            if cfg.dryrun:
                giveaway = await self._fanatical_giveaway()
                logger.debug("[Fanatical] Steps for '%s': %s", title,
                             [(s.get("type"), s.get("done")) for s in giveaway["steps"]])
                if giveaway["soldOut"]:
                    logger.info("[Fanatical] '%s' is sold out, skipping.", title)
                    self.notify_games.remove(notify_game)
                    return
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            if not await self._clear_challenge("Fanatical"):
                notify_game["status"] = "failed:challenge"
                return

            # Read before any step: the claim can sign you up through a step, an old consent or a pre-ticked box.
            had_newsletter = None if cfg.fanatical_newsletter else await self._fanatical_newsletter()

            steps = await self._fanatical_finish_steps(title)
            if steps == "sold-out":
                # GamerPower keeps listing a giveaway after its keys run out, that is no news for you.
                logger.info("[Fanatical] '%s' is sold out, skipping.", title)
                self.notify_games.remove(notify_game)
                return
            if steps == "steam":
                notify_game["status"] = "failed:steam-not-linked"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")
                return
            if steps == "no-steps":
                logger.warning("[Fanatical] '%s' shows no giveaway steps, it may have ended.", title)
                notify_game["status"] = "failed:no-steps"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")
                return
            if steps != "ready":
                logger.warning("[Fanatical] '%s' still has an unfinished step, not claimed.", title)
                notify_game["status"] = "failed:steps"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")
                return

            # The claim only puts the game in the cart; the free order behind it is made at checkout.
            if not await self._fanatical_click(FAN_MARK_MAIN_JS, "Claim button"):
                logger.warning("[Fanatical] '%s' offers no claim button.", title)
                notify_game["status"] = "failed:claim"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")
                return
            outcome, receipt = await self._fanatical_checkout(title)
            logger.debug("[Fanatical] Checkout for '%s' ended: %s", title, outcome)
            if outcome in ("not-free", "no-cart"):
                if outcome == "no-cart":
                    logger.warning("[Fanatical] '%s' never reached the cart, Fanatical did not take the claim.", title)
                notify_game["status"] = "failed:not-free" if outcome == "not-free" else "failed:checkout"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")
                return

            if outcome == "stuck":
                logger.warning("[Fanatical] The checkout for '%s' needs you.", title)
                notice = self._vnc_notice("Fanatical: finish the checkout",
                                          f"'{title}' is in your Fanatical cart. "
                                          "Finish the free checkout in the browser.")
                if await self._wait_for_vnc_login(self._fanatical_on_receipt, custom_msg=notice, store_key="fanatical"):
                    outcome, receipt = "receipt", await self._fanatical_path()

            order_id = fanatical_receipt_order(receipt)
            authorised = fanatical_receipt_authorised(receipt)
            logger.debug("[Fanatical] Receipt for '%s': order id %s, authorised %s", title, bool(order_id), authorised)
            found = None
            if outcome == "receipt" and (order_id or orders is not None):
                found = await self._fanatical_complete_order(game_id, title, oid=order_id)
            elif outcome == "stuck" and orders is not None:
                # You may have finished it over VNC and moved on from the receipt; the account knows.
                found = await self._fanatical_complete_order(game_id, title, seconds=0)

            # A receipt the account cannot confirm still counts, but only one that says the order went through.
            if found or (outcome == "receipt" and orders is None and authorised):
                # A bundle has one key per game inside, a list-only item lacks the ids: both stay in your library.
                single = bool(found) and bool(found["item"].get("_id")) and found["item"].get("type") != "bundle"
                key = await self._fanatical_reveal_key(found, title) if single else ""
                steam_key = key if key and fanatical_item_is_steam(found["item"]) else ""
                await self._remember_fanatical(game_id, title, url, "claimed", steam_key)
                if steam_key and is_store_active("steam"):
                    logger.info("✓ [Fanatical] Claimed '%s', its Steam key is activated at the end of this run.", title)
                    notify_game["status"] = "claimed"
                else:
                    logger.info("✓ [Fanatical] Claimed '%s', the key is in your Fanatical library.", title)
                    notify_game["status"] = "claimed, key in your Fanatical library 🔑"
                await self.take_screenshot(f"fanatical_{filenamify(title)}")
            else:
                logger.warning("[Fanatical] '%s' was not confirmed as claimed (checkout: %s).", title, outcome)
                notify_game["status"] = "failed:unconfirmed" if outcome == "receipt" else "failed:checkout"
                await self.take_screenshot(f"fanatical_fail_{filenamify(title)}")

            # The order signs you up once the checkout runs, whether or not the claim is confirmed after.
            if not cfg.fanatical_newsletter:
                now_subscribed = await self._fanatical_newsletter(tries=3) if had_newsletter is False else None
                logger.debug("[Fanatical] Newsletter for '%s': before %s, after %s",
                             title, had_newsletter, now_subscribed)
                if fanatical_should_unsubscribe(had_newsletter, now_subscribed):
                    await self._fanatical_unsubscribe(title)
                elif had_newsletter is None or (had_newsletter is False and now_subscribed is None):
                    logger.warning("[Fanatical] Could not check your newsletter after '%s', "
                                   "check it in your account.", title)

        except Exception:
            logger.exception("[Fanatical] Error claiming '%s'", title)

    async def _claim_alienware_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)

        notify_game = {"title": f"{title} (Alienware)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            # Check if we already notified about this game to prevent spam
            async with async_session() as session:
                # We use status="notified" to distinctly mark these
                existing, created = await get_or_create(
                    session, store="alienware", user=self.user,
                    game_id=giveaway_url, title=title, url=url, status="notified"
                )
                
                if not created:
                    logger.info("⏭️ [Alienware] '%s', already notified before.", title)
                    notify_game["status"] = "existed"
                    return

                # If it's new, we just notify
                logger.info("🔔 [Alienware] '%s': Please claim manually (requires ARP points): %s", title, url)
                existing.status = "notified"
                await session.commit()

            # Alienware keys need ARP points and solve a captcha, so this one is yours to finish.
            notify_game["status"] = "notified, claim it yourself 🔔"

        except Exception:
            logger.exception("[Alienware] Error processing notification for '%s'", title)

    # ─────────────────────────────────────────────────────────────────────
    # Itch.io
    # ─────────────────────────────────────────────────────────────────────
    async def _submit_itch_credentials(self, email: str, password: str) -> None:
        """Fill itch.io's sign-in form and send it."""
        js_email = json.dumps(email)
        js_password = json.dumps(password)
        await self.page.evaluate(f'''
            (() => {{
                const emailInp = document.querySelector('input[name="username"], input[type="email"]');
                const passInp = document.querySelector('input[name="password"], input[type="password"]');
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                if (emailInp) {{
                    setter.call(emailInp, {js_email});
                    emailInp.dispatchEvent(new Event("input", {{bubbles: true}}));
                }}
                if (passInp) {{
                    setter.call(passInp, {js_password});
                    passInp.dispatchEvent(new Event("input", {{bubbles: true}}));
                }}
                const submit = document.querySelector('button[type="submit"]')
                    || [...document.querySelectorAll('button')]
                        .find(b => (b.textContent || '').toLowerCase().includes('log in'));
                if (submit) submit.click();
            }})()
        ''')
        await self.sleep(5)

    async def _itch_session_ready(self) -> bool:
        """Sign in to itch.io once per run, judged on itch.io itself.

        A creator's own subdomain shows no sign-in link, so asking the game page whether a
        login is needed answered "no" while signed out, and every check after it was wrong.
        """
        if self._itch_session_ok:
            return True

        await self.page.get("https://itch.io/")
        await self.sleep(3)
        # Cloudflare's "Just a moment" page has no logout link, so judging it read as signed out (#59).
        if await self._human_challenge_present():
            logger.debug("[Itch.io] Waiting for the Cloudflare check to pass before judging the session.")
            if not await self._wait_out_challenge("Itch.io", store_key="itchio"):
                return False
        # Right after the check clears itch.io is still loading, so one look read a signed-in session as out (#59).
        if await self._itch_logged_in_after_load():
            self._itch_session_ok = True
            return True

        email = cfg.itchio_email
        password = cfg.itchio_password
        if not (email and password):
            logger.warning("[Itch.io] No credentials set (ITCHIO_EMAIL/PASSWORD). Waiting for VNC...")
            self._itch_session_ok = await self._wait_for_vnc_login(
                self._itch_logged_in, custom_msg=self._no_credentials_notice("Itch.io", "ITCHIO"),
                store_key="itchio")
            return self._itch_session_ok

        logger.info("[Itch.io] Logging in as %s…", mask_account(email))
        await self.page.get("https://itch.io/login")
        await self.sleep(3)
        await self._submit_itch_credentials(email, password)

        # A code screen or a rejected password used to pass silently from here.
        if not await self._confirm_side_login(
                "Itch.io", self._itch_logged_in,
                backup_codes=cfg.itchio_otp_codes,
                backup_file="used_itchio_codes.txt", otp_key=cfg.itchio_otp_key,
                credentials=(email, password)):
            return False
        self._log_side_signed_in("Itch.io", email)
        self._itch_session_ok = True
        return True

    async def _claim_itchio_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("final_url") or game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)
        game_id = itch_game_id(url) or giveaway_url

        notify_game = {"title": f"{title} (Itch.io)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # Signing in is decided on itch.io itself and done once per run.
            if not await self._itch_session_ready():
                return

            # Staying on the previous game's page made every later giveaway inherit its banner.
            await self.page.get(url)
            await self.sleep(4)

            # A signed-out page never shows the ownership banner, so this waits for the session.
            if await self._itch_owns_this():
                logger.info("[Itch.io] '%s' already owned.", title)
                if cfg.dryrun:
                    notify_game["status"] = "existed"
                    return
                async with async_session() as session:
                    obj, _ = await get_or_create(
                        session, store="itchio", user=self.user,
                        game_id=game_id, title=title, url=url, status="existed",
                    )
                    obj.status = "existed"
                    await session.commit()
                notify_game["status"] = "existed"
                return

            # Try to claim: click "Download or Claim" or "Claim" button
            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            walked = await self._itch_run_claim(title)
            if walked == "not-free":
                # GamerPower keeps listing a sale after it drops below 100%, that is no news for you.
                logger.info("[Itch.io] '%s' is not free on itch.io right now, skipping.", title)
                self.notify_games.remove(notify_game)
                return

            # The claim only counts when itch.io says the game is on the account. Clicking
            # through the downloads without claiming leaves you with a file and nothing else.
            await self.page.get(url)
            await self.sleep(5)
            owned = await self._itch_owns_this()

            if owned:
                logger.info("✓ [Itch.io] Claimed '%s'!", title)
                await self._remember_itchio(game_id, title, url, "claimed")
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"itchio_{filenamify(title)}")
            elif walked == "download-only":
                # Plenty of itch.io giveaways are a file and nothing else: no control puts
                # them on the account, so this is not a failed claim, it is all there is.
                first_time = await self._remember_itchio(game_id, title, url, "skipped:download-only")
                if first_time:
                    logger.info("[Itch.io] '%s' is handed out as a download only, there is nothing to "
                                "claim onto the account. Saying so once, later runs stay quiet.", title)
                else:
                    logger.debug("[Itch.io] '%s' is still download only, already reported.", title)
                notify_game["status"] = download_only_status(first_time)
            else:
                logger.warning("[Itch.io] '%s' is not on the account after the claim walk "
                               "(claim step: %s).", title, walked)
                notify_game["status"] = "failed:unconfirmed"
                await self.take_screenshot(f"itchio_fail_{filenamify(title)}")

        except Exception:
            logger.exception("[Itch.io] Error claiming '%s'", title)

    # ─────────────────────────────────────────────────────────────────────
    # IndieGala
    # ─────────────────────────────────────────────────────────────────────
    async def _indiegala_login(self, email: str, password: str) -> None:
        """Type into IndieGala's login page the way a person would; the old fill never found the e-mail field."""
        await self.page.get("https://www.indiegala.com/login")
        await self.sleep(4)
        await self.page.evaluate(IG_DISMISS_JS)
        await self.sleep(1)
        if not await self.page.evaluate(IG_MARK_FIELDS_JS):
            logger.debug("[IndieGala] The login page did not offer both fields.")
            return
        for selector, value in (("[data-fgc-mail]", email), ("[data-fgc-pass]", password)):
            field = await self.page.select(selector, timeout=8)
            if not field:
                return
            await field.click()
            await self.sleep(0.4)
            await field.send_keys(value)
            await self.sleep(0.6)
        # Without a ticked box IndieGala answers "[e030] Please answer the captcha" and signs nobody in.
        if await self._ig_captcha_unsolved():
            logger.info("[IndieGala] The login page asks for its captcha, handing it to you.")
            if not await self._wait_out_challenge("IndieGala", store_key="indiegala",
                                                  present_fn=self._ig_captcha_unsolved):
                return
        try:
            if not await self.page.evaluate(IG_SUBMIT_JS):
                logger.debug("[IndieGala] No LOGIN button beside the password field.")
        except Exception as e:
            # You may have pressed LOGIN yourself after ticking the box, and the page is already leaving.
            logger.debug("[IndieGala] LOGIN not pressed, the page was navigating: %s", e)
        await self.sleep(6)

    async def _ig_captcha_unsolved(self) -> bool:
        """True while the login page's reCAPTCHA box is shown and not yet ticked."""
        try:
            return bool(await self.page.evaluate(IG_CAPTCHA_UNSOLVED_JS))
        except Exception as e:
            logger.debug("[IndieGala] Captcha check failed: %s", e)
            return False

    async def _ig_owns_this(self) -> bool:
        """True when the open freebie page says it is in your library. Only meaningful signed in."""
        try:
            return bool(await self.page.evaluate(IG_OWNED_JS))
        except Exception as e:
            logger.debug("[IndieGala] Ownership check failed: %s", e)
            return False

    async def _ig_note_session(self) -> None:
        """Say once per run until when IndieGala keeps this sign-in; it is 14 days and visits do not extend it."""
        if self._ig_session_noted:
            return
        self._ig_session_noted = True
        try:
            cookies = await self.page.send(uc.cdp.network.get_cookies(urls=["https://www.indiegala.com/"]))
        except Exception as e:
            logger.debug("[IndieGala] Could not read the session cookie: %s", e)
            return
        sid = next((c for c in cookies if c.name == "sessionid"), None)
        if not sid or sid.session:
            logger.debug("[IndieGala] No dated session cookie to report.")
            return
        until = datetime.fromtimestamp(sid.expires, timezone.utc)
        if until - datetime.now(timezone.utc) < timedelta(days=2):
            logger.info("[IndieGala] The sign-in ends on %s, the next IndieGala giveaway after that asks you "
                        "for its captcha again.", until.strftime("%Y-%m-%d"))
        else:
            logger.debug("[IndieGala] Signed in until %s.", until.strftime("%Y-%m-%d %H:%M UTC"))

    async def _remember_indiegala(self, game_id: str, title: str, url: str, status: str) -> None:
        """Store an IndieGala outcome under IndieGala's own slug."""
        async with async_session() as session:
            obj, _ = await get_or_create(
                session, store="indiegala", user=self.user,
                game_id=game_id, title=title, url=url, status=status,
            )
            obj.status = status
            await session.commit()

    async def _claim_indiegala_game(self, game: dict) -> None:
        title = game.get("title", "Unknown")
        url = game.get("url", "")
        giveaway_url = game.get("giveaway_url", url)
        game_id = indiegala_game_id(game.get("final_url") or url) or giveaway_url

        notify_game = {"title": f"{title} (IndieGala)", "url": url, "status": "failed"}
        self.notify_games.append(notify_game)

        try:
            # The host alone is not enough: staying on the previous game's page would judge
            # this one by that page.
            current_url = str(await self.page.evaluate("window.location.href") or "")
            if not current_url.startswith(url):
                await self.page.get(url)
                await self.sleep(4)

            # Check if login needed
            # One check decides, so detection and confirmation cannot drift apart (issue #47).
            if not await self._ig_logged_in():
                email = cfg.indiegala_email
                password = cfg.indiegala_password
                if email and password:
                    logger.info("[IndieGala] Logging in as %s…", mask_account(email))
                    await self._indiegala_login(email, password)

                    if not await self._confirm_side_login("IndieGala", self._ig_logged_in,
                                                          credentials=(email, password)):
                        return
                    self._log_side_signed_in("IndieGala", email)

                    # Navigate back to game page
                    await self.page.get(url)
                    await self.sleep(4)
                else:
                    logger.warning("[IndieGala] No credentials set (INDIEGALA_EMAIL/PASSWORD). Waiting for VNC...")
                    if not await self._wait_for_vnc_login(
                            self._ig_logged_in, custom_msg=self._no_credentials_notice("IndieGala", "INDIEGALA"),
                            store_key="indiegala"):
                        return

            await self._ig_note_session()
            if not await self._clear_challenge("IndieGala"):
                return

            # Judged only now: signed out, no page says whether you own it.
            if await self._ig_owns_this():
                logger.info("[IndieGala] '%s' already owned.", title)
                if not cfg.dryrun:
                    await self._remember_indiegala(game_id, title, url, "existed")
                notify_game["status"] = "existed"
                return

            if cfg.dryrun:
                logger.info("DRYRUN – skipped '%s'.", title)
                notify_game["status"] = "available (dry run)"
                return

            if not await self.page.evaluate(IG_MARK_CLAIM_JS):
                logger.warning("[IndieGala] '%s' offers no ADD TO LIBRARY button.", title)
                await self.take_screenshot(f"indiegala_fail_{filenamify(title)}")
                return
            button = await self.page.select("[data-fgc-claim]", timeout=8)
            await button.scroll_into_view()
            await self.sleep(0.8)
            await button.click()
            # The button reads "Added!" for a moment and then goes; the reloaded page is what counts.
            await self.sleep(4)
            await self.page.get(url)
            await self.sleep(5)

            if await self._ig_owns_this():
                logger.info("✓ [IndieGala] Claimed '%s'!", title)
                await self._remember_indiegala(game_id, title, url, "claimed")
                notify_game["status"] = "claimed"
                await self.take_screenshot(f"indiegala_{filenamify(title)}")
            else:
                logger.warning("[IndieGala] '%s' is not in your library after the click.", title)
                notify_game["status"] = "failed:unconfirmed"
                await self.take_screenshot(f"indiegala_fail_{filenamify(title)}")

        except Exception:
            logger.exception("[IndieGala] Error claiming '%s'", title)


async def claim_side_stores(routed: dict | None = None) -> dict:
    """Entry point for the sites that have no store module of their own."""
    claimer = GamerPowerClaimer()
    await claimer.run(routed)
    return {"store": "GamerPower", "user": None, "games": claimer.notify_games}
