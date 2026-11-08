"""Microsoft Rewards, only when STORES names it and at your own risk: the dashboard tasks, the daily set, punch cards, the Keep earning searches, Bing searches up to the day's limit, streak protection and claiming the points.
Microsoft's terms do not count searches made by a bot and let it close the Rewards account, so it never runs by default."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import socket
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from src.core.claimer import OTP_KEY_ATTEMPTS, account_key, mask_account, older_than, row_extra, text_finder
from src.core.config import cfg
from src.core.database import ClaimedGame, async_session, get_or_create
from src.core.url_security import url_has_allowed_host
from src.stores.microsoft import LOGIN_HOSTS, MicrosoftClaimer

logger = logging.getLogger("fgc.msrewards")

URL_HOME = "https://rewards.bing.com/"
URL_EARN = "https://rewards.bing.com/earn"
URL_BING = "https://www.bing.com/"
REWARDS_HOST = "rewards.bing.com"
BING_HOST = "www.bing.com"
# The lowest level's limit, used when the page does not say: 15 search points a day at 3 a search.
DEFAULT_SEARCH_POINTS = 15
DEFAULT_POINTS_PER_SEARCH = 3
# A search that brings back no results this many times in a row ends the searching for this run.
MAX_EMPTY_SEARCHES = 2
# No search is made again within this many days, and a Keep earning card is opened again after this many.
QUERY_DAYS = 14
CARD_DAYS = 7
# Sign-in looks at a page it does not know this many times before it hands over to you.
UNKNOWN_LOOKS = 3
# The Earn page's "Today's points" and "Daily streak" cards, found by their icons whatever language the page is in.
BREAKDOWN_BUTTON = "() => { const i = document.querySelector('img[src*=\"Icons.Coins.svg\"]'); return i ? i.closest('button') : null; }"
STREAK_BUTTON = "() => { const i = document.querySelector('img[src*=\"Icons.Fire\"]'); return i ? i.closest('button') : null; }"
_OPEN_PANEL = "[...document.querySelectorAll('[role=dialog]')].find(e => e.getClientRects().length)"
# "Points breakdown" shows the day's search points as two spans, "15" and "/15".
PANEL_COUNTER_JS = (f"(() => {{ const d = {_OPEN_PANEL}; const row = d && [...d.querySelectorAll('div')].find(e => e.children.length === 2"
                    " && [...e.children].every(c => c.tagName === 'SPAN') && /^\\s*\\/\\s*\\d/.test(e.children[1].textContent));"
                    " return row ? row.textContent : ''; })()")
PROTECTION_JS = (f"(() => {{ const d = {_OPEN_PANEL}; const s = d && d.querySelector('input[role=switch]');"
                 " return s ? (s.checked ? 'on' : 'off') : ''; })()")
PROTECTION_TOGGLE = f"() => {{ const d = {_OPEN_PANEL}; const s = d && d.querySelector('input[role=switch]'); return s ? s.closest('label') : null; }}"

# Every install has this same list, so a search comes from it only when Bing's page offers neither suggestions nor headlines.
QUERIES = (
    "weather this weekend", "easy dinner recipes", "how to clean a cast iron pan", "best hiking trails",
    "how long to boil an egg", "movies out this week", "cheap healthy lunch ideas", "how to fix a squeaky door",
    "best board games for families", "what time is sunset today", "how to grow basil indoors", "football results",
    "banana bread recipe", "how to remove a coffee stain", "museums open on sunday", "simple stretching routine",
    "how many cups in a liter", "best budget headphones", "how to make cold brew coffee", "local farmers market",
    "history of the bicycle", "how to tie a tie", "tips for better sleep", "easy houseplants for beginners",
    "how does a rainbow form", "quick pasta sauce", "new book releases", "how to change a bike tire",
    "word of the day", "best time to visit iceland", "how to descale a kettle", "kids science experiments",
)

# Next.js puts the data a page was drawn from into these script calls.
FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')

SEARCH_BOX = "() => document.querySelector('#sb_form_q')"
SIGN_IN_LINK = "() => [...document.querySelectorAll('a[href*=\"/auth/login\"]')].find(a => a.getClientRects().length)"
SIGNED_OUT_JS = "!![...document.querySelectorAll('a[href*=\"/auth/login\"]')].find(a => a.getClientRects().length)"
# "Stay signed in?" by its own markers, so no other page after the password gets its button pressed.
KMSI_JS = ("!!document.querySelector('#KmsiCheckboxField, input[name=DontShowAgain]')"
           " || /stay signed in/i.test(document.title)")
STAY_SIGNED_IN = "() => document.querySelector('#idSIButton9, button[data-testid=\"primaryButton\"]')"
DIALOG_BUTTON = ("() => { const d = [...document.querySelectorAll('[role=dialog]')].find(e => e.getClientRects().length);"
                 " return d ? d.querySelector('button') : null; }")
DAILY_SET_TOGGLE = "() => document.querySelector('section#dailyset button[slot=trigger][aria-expanded=false]')"
EARN_TAB = "() => [...document.querySelectorAll('a[href]')].find(a => a.getAttribute('href') === '/earn' && a.getClientRects().length)"
# "Ready to claim" is the one button with the coins icon, whatever language the page is in.
READY_BUTTON = "() => { const i = document.querySelector('img[src*=\"CoinsTransparent\"]'); return i ? i.closest('button') : null; }"
READY_JS = "(() => { const i = document.querySelector('img[src*=\"CoinsTransparent\"]'); const b = i && i.closest('button'); return b ? b.innerText : ''; })()"
# Each punch card on the Earn page, with the "2/7 tasks" its card shows.
QUEST_LINKS_JS = """JSON.stringify([...document.querySelectorAll('a[href^="/earn/quest/"]')].map(a => ({href: a.getAttribute('href'), text: a.innerText || ''})))"""
TODAY_POINTS_JS = "(() => { const i = document.querySelector('img[src*=\"Icons.Coins.svg\"]'); const b = i && i.closest('button'); return b ? b.innerText : ''; })()"
DIALOG_OPEN_JS = "!![...document.querySelectorAll('[role=dialog]')].find(e => e.getClientRects().length)"
PANEL_CLOSE = "() => { const d = [...document.querySelectorAll('[role=dialog]')].find(e => e.getClientRects().length); return d ? d.querySelector('button[slot=close]') : null; }"
EARN_CARDS_JS = """JSON.stringify([...document.querySelectorAll('section#moreactivities a[href]')].map(a => ({
    url: a.getAttribute('href') || '', title: ((a.querySelector('p') || a).innerText || '').trim().slice(0, 80)})))"""
EARN_PROGRESS_JS = """(() => { const s = document.querySelector('section#moreactivities');
    const p = s && [...s.querySelectorAll('p')].find(p => /^\\s*\\d[\\d,.\\s]*\\/\\s*\\d/.test(p.innerText));
    return p ? p.innerText : ''; })()"""
# textContent: Bing draws this section only once it is on screen, so innerText reads empty before that (live 5.11: 0 of 10 queries).
SUGGESTIONS_JS = "JSON.stringify([...document.querySelectorAll('#brsv3 .b_suggestionText')].map(e => e.textContent.replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 10))"
# A query never runs across a full stop, comma, colon or spaced dash: "dniach kierowcy" read like a bot.
PHRASE_END = re.compile(r"[.,:;!?|]+(?=\s|$)|\s[-\u2013]\s")
# The news on Bing's home page, in the account's own language.
HEADLINES_JS = """JSON.stringify([...document.querySelectorAll('h2, h3')].filter(h => h.getClientRects().length)
    .map(h => h.innerText.replace(/\\s+/g, ' ').trim()).filter(t => t.length > 15 && t.length < 160).slice(0, 12))"""


# ----- Reading the pages, all pure -----

def flight_rows(html: str) -> list:
    """The data rows Next.js embedded in a page it drew, each parsed from JSON; rows that are not JSON are left out."""
    try:
        flight = "".join(json.loads(m.group(1)) for m in FLIGHT_CHUNK.finditer(html or ""))
    except ValueError:
        return []
    rows = []
    for line in flight.split("\n"):
        try:
            rows.append(json.loads(line.partition(":")[2]))
        except ValueError:
            continue
    return rows


def _dicts(node):
    """Every dict inside a parsed row, in the order the page has them."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _dict(value) -> dict:
    """A value as a dict; Next.js writes a missing one as the text "$undefined"."""
    return value if isinstance(value, dict) else {}


def _number(value, default=0):
    """A whole number from the page, which writes them as 1,250 or "15"; the default when it is not one."""
    try:
        return int(re.sub(r"[\s,.  ]", "", str(value)))
    except (TypeError, ValueError):
        return default


def is_search_boost(offer: dict) -> bool:
    """A "search Nx more" offer, which raises the day's search limit once taken (the signs TheNetsky's script reads)."""
    text = " ".join(str(offer.get(k) or "") for k in ("title", "description"))
    return bool(re.search(r"optin[_-]?\d+x(?:[_-]|$)", str(offer.get("offerId") or ""), re.I)
                or re.search(r"search\s*\d+\s*x\s*more", text, re.I))


def read_dashboard(html: str) -> dict:
    """What the dashboard was drawn from: balance, level, the day's search points, the daily set and the claim wording."""
    state = {"loaded": False, "balance": None, "level": 1, "per_search": DEFAULT_POINTS_PER_SEARCH,
             "search_points": DEFAULT_SEARCH_POINTS, "searched_today": False, "daily_set": [], "claim_text": "", "claim_entry": None,
             "streaks": set(), "sections": [], "quests": [], "protection": None, "perks": []}
    limits = {}
    for row in flight_rows(html):
        for d in _dicts(row):
            kind, model = d.get("type"), _dict(d.get("model"))
            if set(d) == {"balance", "level"}:
                state["balance"] = _number(d["balance"], None)
            elif kind == "membership" and model:
                # Every Rewards page shows the balance; only the dashboard carries the membership.
                state["loaded"] = True
                member = _dict(model.get("membership"))
                state["level"] = _number(member.get("level"), 1)
                state["per_search"] = _number(member.get("pointsPerSearch"), DEFAULT_POINTS_PER_SEARCH)
                benefits = member.get("levelBenefits") if isinstance(member.get("levelBenefits"), list) else []
                limits = next((_dict(b.get("supportedLevels")) for b in benefits
                               if isinstance(b, dict) and b.get("key") == "SearchAndEarn"), {})
            elif kind == "streak" and _dict(model.get("streak")).get("partner"):
                streak = model["streak"]
                state["streaks"].add(str(streak["partner"]))
                if streak["partner"] == "bing":
                    state["searched_today"] = streak.get("isCurrentDayCompleted") is True or (
                        _number(streak.get("activitiesCompleted")) >= max(1, _number(streak.get("activitiesTotal"), 1)))
            elif kind == "dailyset" and model:
                state["streaks"].add("dailyset")
                items = model.get("dailySetItems") if isinstance(model.get("dailySetItems"), list) else []
                state["daily_set"] = [
                    {"offer_id": str(item.get("offerId") or ""), "title": str(item.get("title") or item.get("description") or "Daily set"),
                     "url": str(item.get("destination") or ""), "points": _number(item.get("points")), "done": item.get("isCompleted") is True}
                    for item in items if isinstance(item, dict) and str(item.get("destination") or "").startswith("http")]
            elif kind == "pointsclaim" and model:
                state["claim_entry"] = model.get("pointClaim")
            elif kind == "quest" and model:
                quest = _dict(model.get("quest"))
                activities = quest.get("activities") if isinstance(quest.get("activities"), list) else []
                state["quests"].append({"name": str(quest.get("name") or ""), "tasks": [
                    {"offer_id": str(a.get("offerId") or ""), "title": str(a.get("title") or "Quest task"), "url": str(a.get("ctaUrl") or ""),
                     "type": str(a.get("type") or ""), "done": a.get("isCompleted") is True, "locked": a.get("isLocked") is True,
                     "steps": _number(a.get("progressMax"), 1)} for a in activities if isinstance(a, dict)]})
            elif kind == "dailystreak" and model:
                guard = _dict(model.get("streakProtection"))
                state["protection"] = {"on": guard.get("isProtectionOn") is True, "days": _number(guard.get("remainingDays"))}
            if isinstance(d.get("id"), str) and isinstance(d.get("discovery"), dict):
                # A folded dashboard section: opening it once completes its "discovery" offer.
                state["sections"].append({"id": d["id"], "title": str(d.get("title") or d["id"]),
                                          "done": d["discovery"].get("isCompleted") is True})
            if isinstance(d.get("offerId"), str) and is_search_boost(d) and d["offerId"] not in {p["offer_id"] for p in state["perks"]}:
                state["perks"].append({"offer_id": d["offerId"], "title": str(d.get("title") or d.get("description") or "")})
            if isinstance(d.get("claimCta"), str):
                state["claim_text"] = d["claimCta"]
    state["search_points"] = _number(limits.get(f"newLevel{state['level']}"), DEFAULT_SEARCH_POINTS)
    return state


def quest_tasks_to_open(state: dict) -> list:
    """Quest tasks one visit completes: a Rewards page to open, not done, not locked, not a search and not one of several steps."""
    # A reward goal on /redeem is yours to choose, so the bot leaves it alone.
    return [t for q in state.get("quests") or [] for t in q["tasks"]
            if not t["done"] and not t["locked"] and t["type"] == "urlreward" and t["steps"] <= 1
            and t["url"].startswith("/") and not t["url"].startswith("/redeem")]


def account_warning(html: str, state: dict) -> str:
    """Why Microsoft treats this account as a bot's right now, empty when nothing says so."""
    if "Fraud_UserWarning" in (html or ""):
        return "Microsoft shows its bot warning on the dashboard"
    # A limited account loses its daily streaks first (the same sign TheNetsky's script reads).
    if state.get("loaded") and not {"bing", "dailyset"} & set(state.get("streaks") or ()):
        return "the daily streaks are gone from the dashboard"
    return ""


def search_counter(text: str) -> dict | None:
    """Points breakdown's "15/15" as the day's search points earned and the most there are; None when it shows none."""
    found = progress(text)
    return {"earned": min(found[0], found[1]), "most": found[1]} if found else None


def ready_points(text: str) -> int:
    """The number on the "Ready to claim" button, 0 when it shows none; numbers on other lines stay apart."""
    found = re.search(r"\d(?:[^\S\n]?[\d,.  ])*", text or "")
    return _number(found.group(0)) if found else 0


def progress(text: str) -> tuple[int, int] | None:
    """A "3/29" counter as (3, 29), None when the text is not one."""
    found = re.search(r"(\d[\d,.]*)\s*/\s*(\d[\d,.]*)", text or "")
    return (_number(found.group(1)), _number(found.group(2))) if found else None


def searches_to_make(search_points: int, per_search: int, done_today: int) -> int:
    """Searches still worth making today: the day's search points over the points one search gives, minus those made."""
    per = per_search if per_search > 0 else DEFAULT_POINTS_PER_SEARCH
    return max(0, math.ceil(max(search_points, 0) / per) - max(done_today, 0))


def searches_done(counter: dict | None) -> bool:
    """Microsoft's search counter shows the day's search points all earned."""
    return bool(counter and counter["earned"] >= counter["most"] > 0)


def headline_query(headline: str, rng=random) -> str:
    """A few words in a row from one phrase of a headline or title, picked anew each time, so no two accounts type the same rare query."""
    phrases = [re.sub(r"[^\w\s'-]", " ", part).split() for part in PHRASE_END.split(headline or "")]
    usable = [words for words in phrases if sum(len(word) > 2 for word in words) >= 2]
    if not usable:
        words = re.sub(r"[^\w\s'-]", " ", headline or "").split()
        short = " ".join(words[:4]).lower()
        # A headline without spaces (Chinese, Japanese) is one long "word": a piece of it, at a random place.
        if len(words) > 1 or len(short) <= 12:
            return short
        size = rng.randint(6, 12)
        start = rng.randint(0, len(short) - size)
        return short[start:start + size].strip()
    # Words in a row, as a person copies them from a headline; scattered words ("domagają osiągać miliardy") read like a bot.
    words = rng.choice(usable)
    starts = [i for i, word in enumerate(words) if len(word) > 2]
    # It ends on a longer word or the phrase's last one, never on a little word ("w", "przy", "dla", "of").
    ends = [i for i in starts if len(words[i]) > 4 or i == starts[-1]]
    first = rng.choice(starts[:-1])
    after = next(i for i in ends if i > first)
    reach = max(after, min(len(words) - 1, first + rng.randint(1, 4)))
    last = max(i for i in ends if after <= i <= reach)
    return " ".join(words[first:last + 1]).lower()


def plural(count: int, word: str) -> str:
    """'1 card', '3 cards', '2 searches'."""
    return f"{count} {word}" + ("" if count == 1 else "es" if word.endswith("ch") else "s")


def account_share(account: str, byte: int) -> float:
    """A number from 0 to 1 of this account's own, the same from run to run but different from other people's."""
    return hashlib.sha256((account or "").encode("utf-8")).digest()[byte] / 255


def account_pace(account: str) -> float:
    """This account's own speed: 0.8 to 1.35 times the base."""
    return 0.8 + account_share(account, 0) * 0.55


def account_follow(account: str) -> float:
    """How often this account takes one of Bing's Related searches when there are some: 55 to 85 in a hundred."""
    return 0.55 + account_share(account, 1) * 0.3


def next_query(used: set, suggestions: list, headlines: list, rng=random, follow: float = 0.7) -> str:
    """Mostly Bing's own Related searches, otherwise the day's news, an everyday topic only when Bing offers neither; none searched lately."""
    fresh = lambda items: [s for s in items if s and plain_letters(s).lower() not in used]  # noqa: E731
    related, news = fresh(suggestions), fresh(headlines)
    if related and (not news or rng.random() < follow):
        return rng.choice(related)
    if news:
        return rng.choice(news)
    return rng.choice(fresh(QUERIES) or list(QUERIES))


# Latin letters NFKD does not take apart; the rest lose their accents there.
_PLAIN = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae", "Æ": "AE",
                        "œ": "oe", "Œ": "OE", "ı": "i"})


def plain_letters(text: str) -> str:
    """A query as many people type it into a search box: Latin letters without accents, other scripts as they are."""
    out = []
    for char in str(text).translate(_PLAIN):
        base = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c))
        out.append(base if base and base.isascii() else char)
    return "".join(out)


def is_quiz(url: str) -> bool:
    """A daily set card that asks questions, which the bot does not answer."""
    query = parse_qs(urlsplit(url or "").query)
    form = ((query.get("form") or query.get("FORM") or [""])[0]).lower()
    return form == "dsetqu" or "quiz" in (query.get("q") or [""])[0].lower() or "isconversation" in (url or "").lower()


def card_key(url: str) -> str:
    """What a Keep earning card is remembered under: the search it opens."""
    return "earn:" + (parse_qs(urlsplit(url or "").query).get("q") or [""])[0].strip().lower()


def earn_cards(cards: list, recent: set) -> list:
    """The Keep earning cards worth a click: a Bing search not opened lately; sign-ups, apps and extensions stay yours."""
    return [c for c in cards if url_has_allowed_host(c.get("url", ""), BING_HOST) and urlsplit(c["url"]).path == "/search"
            and not is_quiz(c["url"]) and card_key(c["url"]) != "earn:" and card_key(c["url"]) not in recent]


def quest_page_steps(html: str) -> list:
    """The steps a punch card's own page offers, each with its link and the label of its button; done steps may be left out by the page."""
    steps, seen = [], set()
    for row in flight_rows(html):
        for d in _dicts(row):
            offer = d.get("offerId")
            # Punch card steps are the "pcchild" offers (the same sign TheNetsky's script reads).
            if isinstance(offer, str) and "pcchild" in offer.lower() and isinstance(d.get("href"), str) and offer not in seen:
                seen.add(offer)
                label = str(d.get("ariaLabel") or "")
                steps.append({"offer_id": offer, "title": label.split(", ", 1)[-1] or offer, "label": label, "url": d["href"],
                              "done": d.get("isCompleted") is True, "locked": d.get("isLocked") is True})
    return steps


def quests_to_visit(cards: list, checked: set) -> list:
    """The punch cards worth opening: not full by their "X/Y tasks" and not already looked at today."""
    links = []
    for card in cards:
        link = card.get("href") if isinstance(card, dict) else None
        if not isinstance(link, str) or link in checked or link in links:
            continue
        done = progress(str(card.get("text") or ""))
        if not (done and done[0] >= done[1] > 0):
            links.append(link)
    return links


def quest_steps_to_open(steps: list) -> list:
    """Steps one visit completes; the reward goal, the dashboard tour and searches over several days stay out."""
    def one_visit(url: str) -> bool:
        # A Bing link with no search in it is a "search on N days" step, which the daily searches fill (TheNetsky skips them too).
        parts = urlsplit(url)
        if ((url_has_allowed_host(url, BING_HOST) or url_has_allowed_host(url, "bing.com")) and parts.path in ("", "/", "/search")
                and "q" not in parse_qs(parts.query)):
            return False
        return not url.startswith(("/redeem", "/dashboard"))
    return [s for s in steps if not s["done"] and not s["locked"] and one_visit(s["url"])]


def step_finder(url: str, label: str) -> str:
    """A finder for BOX_JS: a quest step's button, by its address and its exact label, so the page footer's same link is never taken."""
    return (f"() => [...document.querySelectorAll('a[href]')].find(a => a.getAttribute('href') === {json.dumps(url)}"
            f" && a.getAttribute('aria-label') === {json.dumps(label)} && a.getClientRects().length)")


def href_finder(url: str, within: str = "") -> str:
    """A finder for BOX_JS: the visible link to exactly this address, inside `within` when given."""
    return (f"() => [...document.querySelectorAll({json.dumps((within + ' a[href]').strip())})].find(a => "
            f"a.getAttribute('href') === {json.dumps(url)} && a.getClientRects().length)")


def section_toggle(section_id: str) -> str:
    """A finder for BOX_JS: the button that unfolds this dashboard section, while it is folded."""
    return f"() => document.querySelector({json.dumps(f'section[id={json.dumps(section_id)}] button[slot=trigger][aria-expanded=false]')})"


class MsRewardsClaimer(MicrosoftClaimer):
    store_name = "msrewards"

    def __init__(self) -> None:
        super().__init__()
        # The Microsoft Store's own account shares its browser profile, so it signs in once for both.
        if (cfg.ms_rewards_email or "").lower() == (cfg.ms_email or "").lower():
            self.profile_name = "microsoft"
        self._used: set[str] = set()
        self._suggestions: list[str] = []
        self._headlines: list[str] = []
        self._bing_check = self._not_counted = self._no_results = False
        self._counter_before: dict | None = None
        self._counter_after: dict | None = None
        self._daily_tried: list[str] = []
        self._quizzes_today: set[str] = set()
        self._earn_todo = 0
        # One day per run, so a run that crosses midnight keeps writing the row it read.
        self._day = datetime.now().date().isoformat()
        self._protection_tried = False
        self._quest_tried: list[dict] = []
        self._quest_done: list[str] = []
        self._today_points: int | None = None
        # Each install keeps a pace of its own; signed in by hand there is no e-mail, so the container's name stands in.
        self._pace = account_pace(cfg.ms_rewards_email or socket.gethostname())
        self._follow = account_follow(cfg.ms_rewards_email or socket.gethostname())
        self.typing_pace = self._pace

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self._pace)

    async def run(self) -> None:
        try:
            # start_browser() logs "Browser ready" itself.
            await self.start_browser()
            if not await self._ensure_logged_in():
                logger.error("Aborting Microsoft Rewards due to login failure.")
                return
            await self._collect()
        except Exception as exc:
            logger.exception("Fatal error during Microsoft Rewards flow")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _is_logged_in(self) -> bool:
        """Signed in when Rewards keeps us and offers no Sign in link."""
        url = await self._current_url()
        if any(url_has_allowed_host(url, host) for host in LOGIN_HOSTS) or not url_has_allowed_host(url, REWARDS_HOST):
            return False
        if urlsplit(url).path.startswith("/auth") or await self.page.evaluate("document.readyState") != "complete":
            return False
        return not await self.page.evaluate(SIGNED_OUT_JS)

    async def _ensure_logged_in(self) -> bool:
        """Come to Rewards from Bing the way a person does, and sign in when it asks."""
        await self._open(URL_BING)
        await self.sleep(random.uniform(4, 7))
        await self._accept_cookies()
        await self._read_headlines()
        await self._look_around(random.uniform(3, 6))
        await self._open(URL_HOME)
        await self.sleep(random.uniform(6, 9))
        if await self._is_logged_in():
            await self._signed_in()
            return True

        logger.warning("Not signed in to Microsoft Rewards, attempting login...")
        if cfg.ms_rewards_email and cfg.ms_rewards_password:
            if await self._sign_in_by_hand():
                await self._signed_in()
                return True
        else:
            logger.warning("MS_REWARDS_EMAIL / MS_REWARDS_PASSWORD (or MS_EMAIL / MS_PASSWORD) not set.")
        await self.take_screenshot("msrewards_login_unfinished")
        logged_in = await self._wait_for_vnc_login(
            self._is_logged_in,
            custom_msg=self._vnc_notice(
                "Microsoft Rewards: sign-in needed",
                "Microsoft wants you to finish signing in. Open the browser, and if it asks for a passkey "
                "choose another way and use your password or the e-mail code."),
            store_key=self.store_name)
        if logged_in:
            await self._signed_in()
        else:
            logger.warning("VNC login timed out, skipping Microsoft Rewards.")
        return logged_in

    async def _signed_in(self) -> None:
        self.user = mask_account(cfg.ms_rewards_email) or "Microsoft Rewards"
        self.log_signed_in(self.user)

    async def _sign_in_by_hand(self) -> bool:
        """Sign in with the mouse and keys: Sign in, the e-mail, the password instead of a code, a code when set, Stay signed in."""
        typed = {"e-mail field": 0, "password field": 0, "code field": 0}
        unknown = 0
        for step in range(12):
            await self.sleep(random.uniform(4, 6))
            if await self._is_logged_in():
                return True
            url = await self._current_url()
            logger.debug("Rewards sign-in step %d at %s", step, self._short_url(url))
            if await self._human_challenge_present():
                if not await self._wait_out_challenge("Microsoft Rewards", settle=30):
                    return False
                continue
            label, field = await self._sign_in_field()
            if field:
                # The field was waited out after Enter, so seeing it again means it was refused; retyping could lock the account.
                if typed[label] >= (OTP_KEY_ATTEMPTS if label == "code field" else 1):
                    await self._log_page_state(f"after the {label} was refused")
                    return False
                if label == "code field" and not cfg.ms_rewards_otp_key:
                    logger.debug("Microsoft asks for a code and no authenticator key is set, leaving it to you.")
                    return False
                if label == "code field":
                    self._last_totp = await self._fresh_totp(cfg.ms_rewards_otp_key, self._last_totp)
                value = {"e-mail field": cfg.ms_rewards_email, "password field": cfg.ms_rewards_password}.get(label, self._last_totp)
                if await self._fill(field, value, label):
                    typed[label] += 1
                    unknown = 0
                    await self._wait_until_gone(field)
                    continue
            elif urlsplit(url).path == "/ppsecure/post.srf" and await self.page.evaluate(KMSI_JS):
                # "Stay signed in?": yes keeps the session in the profile.
                if await self._click_like_a_hand(STAY_SIGNED_IN, "Stay signed in"):
                    unknown = 0
                    continue
            elif await self._click_first(("Use your password", "Other ways to sign in"), "sign-in method"):
                unknown = 0
                continue
            elif url_has_allowed_host(url, REWARDS_HOST) and await self._click_like_a_hand(SIGN_IN_LINK, "Sign in"):
                unknown = 0
                continue
            # A page still loading or bouncing between sign-in hosts gets another look before it counts as unknown.
            unknown += 1
            if unknown >= UNKNOWN_LOOKS:
                await self._log_page_state("on a sign-in page the bot does not know")
                return False
        return await self._is_logged_in()

    async def _sign_in_field(self) -> tuple[str, str]:
        """Which of the e-mail, password and code fields the page shows, and the selector it was found by."""
        for label, selectors in (("e-mail field", self.EMAIL_FIELDS), ("password field", self.PASSWORD_FIELDS),
                                 ("code field", self.CODE_FIELDS)):
            field = await self._visible_field(selectors)
            if field:
                return label, field
        return "", ""

    async def _visible_field(self, selectors: tuple) -> str:
        """The first of these fields shown on the page, empty when none is."""
        found = await self.page.evaluate(
            f"{json.dumps(list(selectors))}.find(s => {{ const e = document.querySelector(s); return e && e.getClientRects().length; }}) || ''")
        return found if isinstance(found, str) else ""

    async def _wait_until_gone(self, selector: str, seconds: int = 20) -> None:
        """Give the page time to take a typed field away, a slow device included."""
        for _ in range(seconds // 2):
            if not await self._visible_field((selector,)):
                return
            await self.sleep(2)
        logger.debug("The field %s is still shown %ds after Enter.", selector, seconds)

    async def _fill(self, selector: str, text: str, label: str) -> bool:
        """Click a field, replace what it holds by typing key by key, and press Enter."""
        if not await self._click_like_a_hand(f"() => document.querySelector({json.dumps(selector)})", label):
            return False
        await self.sleep(random.uniform(0.4, 1.0))
        await self._select_all()
        await self._type_like_a_hand(text)
        await self.sleep(random.uniform(0.5, 1.2))
        await self._press_enter()
        return True

    async def _click_first(self, words: tuple, label: str) -> bool:
        """Click the first button or link showing one of these words."""
        for word in words:
            if await self._click_like_a_hand(text_finder(word, tag="button, a, [role=button]"), f"{label}: {word}"):
                return True
        return False

    # ------------------------------------------------------------------
    # The day's points
    # ------------------------------------------------------------------

    async def _collect(self) -> None:
        on_dashboard = urlsplit(await self._current_url()).path.rstrip("/") == "/dashboard"
        state = await self._read_dashboard(load=not on_dashboard, shot="msrewards_dashboard")
        if not state["loaded"]:
            logger.warning("Could not read the Rewards dashboard, nothing done this run.")
            await self.take_screenshot("msrewards_dashboard_unread")
            self.notify_games.append({"title": "Microsoft Rewards", "url": URL_HOME, "status": "failed: dashboard not readable"})
            return
        if state["warning"]:
            await self._stand_down(state)
            return
        log = await self._search_log()
        made_before, self._used, stopped = log["made"], log["used"], log["stopped"]
        self._quizzes_today = set(log["quizzes"])
        planned = 0 if stopped or log["counter_full"] else searches_to_make(state["search_points"], state["per_search"], made_before)
        if stopped:
            logger.debug("Searches stopped earlier today (%s), none until tomorrow.", stopped)
        # A quiz counts hours after it is opened (seen live on 2026-10-26), so each is opened once a day.
        open_items = [i for i in state["daily_set"] if not i["done"] and not (is_quiz(i["url"]) and i["offer_id"] in log["quizzes"])]
        logger.info("Rewards: %s points, daily set %d/%d done.", state["balance"],
                    sum(i["done"] for i in state["daily_set"]), len(state["daily_set"]))
        logger.debug("Searches planned from the level, before Microsoft's counter is read: %d.", planned)
        tasks = len([s for s in state["sections"] if not s["done"]]) + len(quest_tasks_to_open(state))
        if cfg.dryrun:
            self.notify_games.append({"title": "Microsoft Rewards", "url": URL_HOME, "status": (
                f"{tasks} dashboard task(s), {len(open_items)} daily set item(s), {planned} search(es), "
                f"{state['ready']} points to claim (dry run)")})
            return

        await self._close_dialogs()
        # Every install runs this code, so the order of independent work is drawn anew, as a person starts where they like.
        dashboard_work = [lambda: self._do_dashboard_tasks(state) if tasks else None,
                          lambda: self._do_daily_set(open_items) if open_items else None]
        for work in random.sample(dashboard_work, len(dashboard_work)):
            pending = work()
            if pending is not None:
                await pending
        earn, made = (0, None, None), 0
        visit_earn = not (log["earn_idle"] and log["counter_full"])
        if not visit_earn:
            logger.debug("The Earn page had nothing left earlier today and the searches are done, so it is not opened again.")
        else:
            earn = await self._do_earn_page(state, log)
            # Called even when the level says the searches are made: Microsoft's counter, read on the Earn page, decides.
            made = 0 if stopped else await self._do_searches(planned, made_before, state["per_search"])

        after = await self._read_dashboard()
        if visit_earn:
            await self._note_earn_idle(log, earn, after)
        claimed = await self._claim(after)
        self._report(state, after, earn, made, made_before + made, planned + made_before, claimed)

    async def _read_dashboard(self, load: bool = True, shot: str = "") -> dict:
        """The dashboard, opened afresh so its data is current, read from what the page was drawn from."""
        if load:
            await self._open(URL_HOME)
            await self.sleep(random.uniform(6, 9))
        html = await self.page.evaluate("document.documentElement.outerHTML")
        html = html if isinstance(html, str) else ""
        state = read_dashboard(html)
        state["warning"] = account_warning(html, state)
        state["ready"] = ready_points(await self.page.evaluate(READY_JS))
        logger.debug("Rewards dashboard: loaded=%s balance=%s level=%s search=%s/%s a day, searched_today=%s, "
                     "ready=%s, streaks=%s, warning=%r, claim entry=%s, daily set=%s, sections=%s, quest tasks=%s, "
                     "streak protection=%s, search boosts=%s", state["loaded"], state["balance"],
                     state["level"], state["per_search"], state["search_points"], state["searched_today"], state["ready"],
                     sorted(state["streaks"]), state["warning"], json.dumps(state["claim_entry"])[:200],
                     [(i["offer_id"], i["done"]) for i in state["daily_set"]], [(s["id"], s["done"]) for s in state["sections"]],
                     [(t["title"], t["done"]) for q in state["quests"] for t in q["tasks"]], state["protection"], state["perks"])
        if shot:
            await self.take_screenshot(shot, failed=False)
        return state

    async def _stand_down(self, state: dict) -> None:
        """Microsoft treats the account as a bot's: no cards and no searches, only the points already earned are claimed."""
        logger.warning("Microsoft Rewards: %s, so the bot does nothing else on this account this run.", state["warning"])
        await self.take_screenshot("msrewards_limited")
        self.notify_games.append({"title": "Microsoft Rewards", "url": URL_HOME, "status": "failed: Microsoft limited the account"})
        if cfg.dryrun or state["ready"] <= 0:
            return
        # Not _close_dialogs(): the warning itself may be a dialog, and its buttons are yours to press.
        await self._accept_cookies()
        claimed = await self._claim(state)
        if claimed:
            self.notify_games.append({"title": "Points", "url": URL_HOME, "status": f"{claimed} claimed, balance {state['balance']}"})
            logger.info("Rewards: claimed %d points, balance %s.", claimed, state["balance"])
        elif claimed is None:
            self.notify_games.append({"title": "Points", "url": URL_HOME, "status": f"failed: {state['ready']} points waiting, not claimed"})

    async def _close_dialogs(self) -> None:
        """Answer the welcome window and the cookie banner, which sit over the cards."""
        await self._dismiss_dialogs()
        await self._accept_cookies()

    async def _dismiss_dialogs(self) -> None:
        """Close the welcome window or a first-visit hint, which cover the page until closed."""
        for _ in range(3):
            if not await self._click_like_a_hand(DIALOG_BUTTON, "Rewards dialog"):
                break
            await self.sleep(random.uniform(1.5, 3))
        # Escape only for a window its button did not close; a key pressed at nothing would be a habit no person has.
        if await self.page.evaluate(DIALOG_OPEN_JS):
            logger.debug("A dialog is still open after its button, closing it with Escape.")
            await self._press_key("Escape", "Escape", 27)
            await self.sleep(random.uniform(0.5, 1.2))

    async def _close_panel(self) -> None:
        """Close a side panel the way people do, sometimes with its X and sometimes with Escape."""
        if random.random() < 0.5 and await self._click_like_a_hand(PANEL_CLOSE, "panel close"):
            await self.sleep(random.uniform(1, 2))
            return
        logger.debug("Closing the panel with Escape.")
        await self._press_key("Escape", "Escape", 27)
        await self.sleep(random.uniform(1, 2))

    async def _accept_cookies(self) -> None:
        """Accept the cookie banner, a link on Bing and a button on Rewards, when it is there."""
        if await self._click_like_a_hand(text_finder("Accept", tag="a, button", exact=True), "cookie banner"):
            await self.sleep(random.uniform(1.5, 3))

    async def _do_earn_page(self, state: dict, log: dict) -> tuple:
        """The Earn page: Keep earning and the punch cards in either order, then the search counter and streak protection."""
        if not await self._click_like_a_hand(EARN_TAB, "Earn tab"):
            await self._open(URL_EARN)
        await self.sleep(random.uniform(5, 8))
        await self._dismiss_dialogs()
        earn = (0, None, None)
        quests_first = random.random() < 0.5
        if not quests_first:
            earn = await self._do_keep_earning()
        quests = quests_to_visit(await self._quest_cards(), log["quests_checked"])
        if quests:
            await self._do_quests(quests)
            await self._open(URL_EARN)
            await self.sleep(random.uniform(5, 8))
        if quests_first:
            earn = await self._do_keep_earning()
        if not log["counter_full"]:
            self._counter_before = await self._read_breakdown()
        await self._protect_streak(state)
        return earn

    async def _note_earn_idle(self, log: dict, earn: tuple, after: dict) -> None:
        """Remember when this run left nothing on the Earn page, so later runs today skip it."""
        opened, before, now = earn
        cards_done = opened == self._earn_todo and (not opened or bool(before and now and now[0] - before[0] >= opened))
        full = log["counter_full"] or searches_done(self._counter_before) or searches_done(self._counter_after)
        guard = after.get("protection")
        protected = not self._protection_tried or bool(guard and guard["on"])
        logger.debug("Earn page left idle: searches full=%s, Keep earning done=%s (%d of %d), protection settled=%s.",
                     full, cards_done, opened, self._earn_todo, protected)
        if full and cards_done and protected:
            await self._today_row(lambda extra: {**extra, "earn_idle": True})

    async def _do_dashboard_tasks(self, state: dict) -> None:
        """Unfold the dashboard's folded sections and open the quest tasks one visit completes, like someone exploring the page."""
        folded = [s for s in state["sections"] if not s["done"]]
        for section in random.sample(folded, len(folded)):
            if await self._click_like_a_hand(section_toggle(section["id"]), f"{section['title']} section"):
                await self.sleep(random.uniform(1.5, 3))
                # Each section shows a hint the first time it opens, and the hint covers what is below it.
                await self._dismiss_dialogs()
        tasks = quest_tasks_to_open(state)
        for task in random.sample(tasks, len(tasks)):
            if not await self._open_card(href_finder(task["url"], "section[id^=quest_]"), task["title"]):
                logger.debug("Quest task '%s' could not be opened.", task["title"])
                continue
            await self._dismiss_dialogs()
            if urlsplit(await self._current_url()).path.rstrip("/") != "/dashboard":
                await self._open(URL_HOME)
                await self.sleep(random.uniform(4, 6))
        await self.take_screenshot("msrewards_dashboard_tasks", failed=False)

    async def _do_daily_set(self, items: list) -> None:
        """Open each daily set card that is not done yet; the section is opened first, like a person would."""
        if await self._click_like_a_hand(DAILY_SET_TOGGLE, "Daily set section"):
            await self.sleep(random.uniform(1.5, 3))
            # Opening the section the first time brings a hint that covers the cards.
            await self._dismiss_dialogs()
        for item in random.sample(items, len(items)):
            self._daily_tried.append(item["offer_id"])
            if not await self._open_card(href_finder(item["url"]), item["title"]):
                logger.debug("Daily set card '%s' could not be opened.", item["title"])
                continue
            if is_quiz(item["url"]):
                self._quizzes_today.add(item["offer_id"])
                await self._today_row(lambda extra, quiz=item["offer_id"]: {**extra, "quizzes": [*(extra.get("quizzes") or []), quiz]})
        await self.take_screenshot("msrewards_daily_set", failed=False)

    async def _do_keep_earning(self) -> tuple:
        """On the Earn page, open the Keep earning cards that are a Bing search: how many were opened, the counter before and after."""
        before = await self._earn_counter()
        try:
            cards = json.loads(await self.page.evaluate(EARN_CARDS_JS))
        except (TypeError, ValueError):
            cards = []
        todo = earn_cards(cards, await self._recent_cards())
        self._earn_todo = len(todo)
        logger.debug("Keep earning: counter %s, %d card(s), %d to open: %s", before, len(cards), len(todo),
                     [c["title"] for c in todo])
        await self.take_screenshot("msrewards_earn", failed=False)
        opened = [card for card in random.sample(todo, len(todo)) if await self._open_card(href_finder(card["url"]), card["title"])]
        if not opened:
            return 0, before, before
        await self._open(URL_EARN)
        await self.sleep(random.uniform(5, 8))
        after = await self._earn_counter()
        logger.debug("Keep earning: counter %s -> %s after %d card(s).", before, after, len(opened))
        # Remembered only when the page counted every one: which card did not count cannot be told, so all come back next run.
        if before and after and after[0] - before[0] >= len(opened):
            for card in opened:
                await self._remember_card(card)
        return len(opened), before, after

    async def _quest_cards(self) -> list:
        """The punch cards the Earn page lists: their own page's address and the "X/Y tasks" their card shows."""
        try:
            cards = json.loads(await self.page.evaluate(QUEST_LINKS_JS))
        except (TypeError, ValueError):
            return []
        logger.debug("Punch cards on the Earn page: %s", [(c.get("href"), progress(str(c.get("text") or ""))) for c in cards if isinstance(c, dict)])
        return [c for c in cards if isinstance(c, dict)]

    async def _do_quests(self, links: list) -> None:
        """Open each punch card's page and do the steps one visit completes, each at most once a day, then check them there."""
        for link in random.sample(links, len(links)):
            url = "https://rewards.bing.com" + link
            await self._open(url)
            await self.sleep(random.uniform(5, 8))
            await self._dismiss_dialogs()
            await self._today_row(lambda extra: {**extra, "quests_checked": [*(extra.get("quests_checked") or []), link]})
            html = await self.page.evaluate("document.documentElement.outerHTML")
            steps = quest_steps_to_open(quest_page_steps(html if isinstance(html, str) else ""))
            logger.debug("Punch card %s: %d step(s) to open: %s", link, len(steps), [s["title"] for s in steps])
            if not steps:
                continue
            for step in random.sample(steps, len(steps)):
                logger.debug("Punch card step '%s' goes to %s", step["title"], step["url"][:160])
                # Tried whether or not it opened: a step that stays on the page is reported as not counted.
                self._quest_tried.append(step)
                if not await self._open_card(step_finder(step["url"], step["label"]), step["title"]):
                    logger.debug("Punch card step '%s' could not be opened.", step["title"])
                if urlsplit(await self._current_url()).path != link:
                    await self._open(url)
                    await self.sleep(random.uniform(4, 6))
            # Done steps lose their button on the page, so a step counts when it is gone or marked done.
            await self._open(url)
            await self.sleep(random.uniform(5, 8))
            html = await self.page.evaluate("document.documentElement.outerHTML")
            left = {s["offer_id"] for s in quest_page_steps(html if isinstance(html, str) else "") if not s["done"]}
            self._quest_done.extend(s["offer_id"] for s in steps if s["offer_id"] not in left)
            await self.take_screenshot("msrewards_quest", failed=False)

    async def _earn_counter(self) -> tuple[int, int] | None:
        """The Keep earning counter as the page shows it, e.g. (20, 29)."""
        text = await self.page.evaluate(EARN_PROGRESS_JS)
        bar = await self.page.evaluate("(() => { const b = document.querySelector('section#moreactivities [role=progressbar]');"
                                       " return b ? b.getAttribute('aria-valuetext') || '' : ''; })()")
        logger.debug("Keep earning counter on the page: %r, progress bar %r.", text, bar)
        return progress(text if isinstance(text, str) else "")

    async def _open_card(self, finder: str, label: str) -> bool:
        """Click a card, look at the page it opens for a while, close its tab and come back."""
        home = self.page
        before = {t.target.target_id for t in self.browser.tabs}
        if not await self._click_like_a_hand(finder, label):
            return False
        opened = None
        for _ in range(10):
            await self.sleep(1)
            await self.browser.update_targets()
            opened = next((t for t in self.browser.tabs if t.target.target_id not in before), None)
            if opened:
                break
        if opened is None:
            logger.debug("'%s' opened no new tab, it is read where it opened.", label)
            await self._look_around(random.uniform(8, 15))
            if not url_has_allowed_host(await self._current_url(), REWARDS_HOST):
                await self.page.back()
                await self.sleep(random.uniform(3, 5))
            return True
        mouse, self._mouse_xy, self.page = self._mouse_xy, (), opened
        try:
            await opened.bring_to_front()
            await self.sleep(random.uniform(3, 5))
            logger.debug("'%s' opened %s", label, self._short_url(await self._current_url()))
            await self._look_around(random.uniform(8, 18))
        finally:
            self.page, self._mouse_xy = home, mouse
            await opened.close()
            await home.bring_to_front()
            await self.sleep(random.uniform(1.5, 3))
        return True

    async def _look_around(self, seconds: float) -> None:
        """Read a page for a while: a few turns of the mouse wheel at uneven moments, and now and then a little back up."""
        x, y = await self._page_wheel_point()
        pauses = [random.uniform(0.5, 1.5) for _ in range(random.randint(2, 5))]
        share = seconds / sum(pauses)
        for pause in pauses[:-1]:
            await self._wheel_scroll(x, y, random.choice([120, 240, 360, 480]))
            await self.sleep(pause * share)
        if random.random() < 0.6:
            await self._wheel_scroll(x, y, -random.choice([120, 240, 360]))
        await self.sleep(pauses[-1] * share)

    # ------------------------------------------------------------------
    # Searches
    # ------------------------------------------------------------------

    async def _do_searches(self, planned: int, made_before: int, per_search: int) -> int:
        """Search Bing by hand up to the day's limit; a check, a page without results or a counter that stops ends it, never a retry."""
        before = self._counter_before
        if before is not None:
            planned = searches_to_make(before["most"] - before["earned"], per_search, 0)
            logger.debug("Microsoft's counter: %d of %d search points today, %d search(es) to make.", before["earned"], before["most"], planned)
        if not planned:
            return 0
        await self._open(URL_BING)
        await self.sleep(random.uniform(4, 7))
        await self._accept_cookies()
        await self._read_headlines()
        made = empty = 0
        for number in range(1, planned + 1):
            # Typed without accents, as many people search; a letter with no key on the keyboard would stand out.
            query = plain_letters(next_query(self._used, self._suggestions, self._headlines, follow=self._follow))
            # Marked as used before the search, so a search that fails is not picked again.
            self._used.add(query.lower())
            found = await self._search(query)
            reason = await self._human_challenge_reason()
            if reason:
                await self._count_check(self.store_name, reason)
                # A check here means Bing doubts this session; more searches would only add to that.
                self._bing_check = True
                logger.warning("Bing asked for a check after %d search(es), the searches wait for tomorrow.", made)
                logger.debug("Bing check: %s", reason)
                await self.take_screenshot("msrewards_search_check")
                await self._stop_searches_today("Bing check")
                break
            if not found:
                empty += 1
                logger.debug("Search '%s' brought back no results (%d in a row).", query, empty)
                if empty >= MAX_EMPTY_SEARCHES:
                    self._no_results = True
                    logger.warning("Bing searches: %d searches in a row brought back no results, the rest wait for the next run.", empty)
                    await self.take_screenshot("msrewards_search_empty")
                    break
                continue
            empty = 0
            made += 1
            await self._count_search(made_before + made, query)
            if number in (1, planned):
                await self.take_screenshot(f"msrewards_search_{number}", failed=False)
            await self._read_results()
        if made and before is not None:
            # Back to the Earn page, where a person checks what the searches brought.
            await self._open(URL_EARN)
            await self.sleep(random.uniform(5, 8))
            self._counter_after = await self._read_breakdown()
        after = self._counter_after
        if made and before is not None and after is not None and after["earned"] <= before["earned"]:
            # Points that stop coming mean Microsoft does not count these searches; more of them would only add to that.
            self._not_counted = True
            await self._stop_searches_today("counter did not move")
        logger.debug("Searches: %d made of %d planned, counter %s -> %s.", made, planned, before, after)
        return made

    async def _read_breakdown(self) -> dict | None:
        """Microsoft's own search counter from the Earn page's Points breakdown: opened, read and closed like a person."""
        if not await self._click_like_a_hand(BREAKDOWN_BUTTON, "Points breakdown"):
            return None
        await self.sleep(random.uniform(2, 4))
        text = await self.page.evaluate(PANEL_COUNTER_JS)
        counter = search_counter(text if isinstance(text, str) else "")
        today = await self.page.evaluate(TODAY_POINTS_JS)
        self._today_points = ready_points(today) if isinstance(today, str) and today else self._today_points
        logger.debug("Points breakdown: search %r -> %s, today's points %s", text, counter, self._today_points)
        await self.sleep(random.uniform(1, 3))
        await self._close_panel()
        if searches_done(counter):
            await self._today_row(lambda extra: {**extra, "counter_full": True})
        return counter

    async def _protect_streak(self, state: dict) -> None:
        """Switch streak protection on when it is off and has days left, so one missed day does not reset the streaks."""
        guard = state.get("protection")
        if not guard or guard["on"] or guard["days"] <= 0:
            return
        if not await self._click_like_a_hand(STREAK_BUTTON, "Daily streak"):
            return
        await self.sleep(random.uniform(2, 4))
        seen = await self.page.evaluate(PROTECTION_JS)
        if seen == "off" and await self._click_like_a_hand(PROTECTION_TOGGLE, "Streak protection"):
            await self.sleep(random.uniform(2, 3))
        logger.debug("Streak protection switch: %r before, %r now.", seen, await self.page.evaluate(PROTECTION_JS))
        await self._close_panel()
        self._protection_tried = True

    async def _search(self, query: str) -> bool:
        """Click the search box, replace its text by typing, press Enter; True when results came back."""
        if not await self._click_like_a_hand(SEARCH_BOX, "search box"):
            return False
        await self.sleep(random.uniform(0.3, 0.9))
        await self._select_all()
        await self._type_like_a_hand(query)
        await self.sleep(random.uniform(0.4, 1.2))
        await self._press_enter()
        await self.sleep(random.uniform(4, 7))
        results = await self.page.evaluate("document.querySelectorAll('#b_results > li').length")
        logger.debug("Searched '%s': %s result block(s).", query, results)
        return isinstance(results, int) and results > 0

    async def _read_results(self) -> None:
        """Read the results like a person, and keep the Related searches for the next query."""
        await self._look_around(random.uniform(20, 40))
        try:
            found = json.loads(await self.page.evaluate(SUGGESTIONS_JS))
        except (TypeError, ValueError):
            found = []
        # A page without them keeps the earlier ones, so one natural query is not dropped for a headline at once.
        fresh = [s for s in found + self._suggestions if plain_letters(s).lower() not in self._used]
        self._suggestions = list(dict.fromkeys(fresh))[:20]
        logger.debug("Related searches on the page: %d, %d kept for the next query.", len(found), len(self._suggestions))

    async def _read_headlines(self) -> None:
        """Note the news on Bing's home page as possible queries."""
        try:
            self._headlines = [q for q in (headline_query(h) for h in json.loads(await self.page.evaluate(HEADLINES_JS))) if q]
        except (TypeError, ValueError):
            self._headlines = []
        logger.debug("Bing home page: %d headline(s) to search for.", len(self._headlines))

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    async def _claim(self, state: dict) -> int | None:
        """Move the points waiting in "Ready to claim" to the balance; the points gained, None when it did not happen."""
        if state["ready"] <= 0:
            logger.debug("Nothing waiting in Ready to claim.")
            return 0
        if not await self._click_like_a_hand(READY_BUTTON, "Ready to claim"):
            return None
        await self.sleep(random.uniform(2, 4))
        await self.take_screenshot("msrewards_claim", failed=False)
        wording = state["claim_text"] or "Claim points"
        if not await self._click_like_a_hand(text_finder(wording, tag="button"), wording):
            logger.debug("The claim panel shows no '%s' button.", wording)
            await self.take_screenshot("msrewards_claim_no_button")
            return None
        await self.sleep(random.uniform(5, 8))
        after = await self._read_dashboard()
        logger.debug("Claim: balance %s -> %s, ready %s -> %s.", state["balance"], after["balance"], state["ready"], after["ready"])
        # A balance that could not be read confirms nothing.
        gained = None if state["balance"] is None or after["balance"] is None else after["balance"] - state["balance"]
        if not gained or gained <= 0:
            await self.take_screenshot("msrewards_claim_unconfirmed")
            return None
        state["balance"] = after["balance"]
        return gained

    # ------------------------------------------------------------------
    # Report and database
    # ------------------------------------------------------------------

    def _report(self, first: dict, after: dict, earn: tuple, made: int, made_today: int, target: int, claimed) -> None:
        """The day on top (today's points and the balance), then one plain line for each part that changed; INFO says the same."""
        lines = []

        def line(title: str, url: str, status: str, missed: list | None = None) -> None:
            # Titles stay out of the summary: the summary hides any line with "already" or "skip" in it, which a title may hold.
            lines.append({"title": title, "url": url, "status": status})
            (logger.warning if status.startswith("failed") else logger.info)(
                "%s: %s%s", title, status, f", not counted: {', '.join(missed)}" if missed else "")

        tried = {s["id"]: s["title"] for s in first["sections"] if not s["done"]} | {t["offer_id"]: t["title"] for t in quest_tasks_to_open(first)}
        if tried:
            done_now = {s["id"] for s in after["sections"] if s["done"]} | {t["offer_id"] for q in after["quests"] for t in q["tasks"] if t["done"]}
            missed = [title for key, title in tried.items() if key not in done_now]
            counted = len(tried) - len(missed)
            line("Dashboard", URL_HOME, f"{plural(counted, 'task')} done" if not missed
                 else f"failed: {counted} of {plural(len(tried), 'task')} done", missed)
        # Only the run that opened cards reports the set.
        if self._daily_tried:
            items = after["daily_set"] or first["daily_set"]
            left = [i for i in items if not i["done"]]
            # A quiz counts hours after it is opened, so the next run is the one that sees it done.
            waiting = [i for i in left if is_quiz(i["url"]) and i["offer_id"] in self._quizzes_today]
            missed = [i["title"] for i in left if i not in waiting]
            done = f"{len(items) - len(left)} of {len(items)} done"
            line("Daily set", URL_HOME, f"failed: {done}" if missed else f"{done}, the quiz counts later" if waiting else done, missed)
        if self._quest_tried:
            missed = [s["title"] for s in self._quest_tried if s["offer_id"] not in self._quest_done]
            counted = len(self._quest_tried) - len(missed)
            line("Punch cards", URL_EARN, f"{plural(counted, 'step')} done" if not missed
                 else f"failed: {counted} of {plural(len(self._quest_tried), 'step')} done", missed)
        opened, earn_before, earn_after = earn
        counted = earn_after[0] - earn_before[0] if earn_before and earn_after else 0
        if opened and counted <= 0:
            line("Keep earning", URL_EARN, f"failed: {plural(opened, 'card')} opened, not counted")
        elif counted < opened:
            line("Keep earning", URL_EARN, f"failed: {plural(opened, 'card')} opened, {counted} counted")
        elif opened < self._earn_todo:
            line("Keep earning", URL_EARN, f"failed: {opened} of {plural(self._earn_todo, 'card')} opened")
        elif opened:
            line("Keep earning", URL_EARN, f"{plural(opened, 'card')} opened")
        before_count, after_count = self._counter_before, self._counter_after
        if self._bing_check:
            line("Bing searches", URL_BING, f"failed: Bing asked for a check after {plural(made, 'search')}")
        elif self._no_results and not made:
            line("Bing searches", URL_BING, "failed: Bing showed no results")
        elif made and (self._not_counted or (before_count and after_count and after_count["earned"] <= before_count["earned"])
                       or (not (before_count and after_count) and not after["searched_today"])):
            # Microsoft does not count searches it takes for a bot's, so its own counter is what confirms them.
            line("Bing searches", URL_BING, f"failed: {plural(made, 'search')}, not counted by Microsoft")
        elif made and before_count and after_count:
            line("Bing searches", URL_BING, f"{plural(made, 'search')}, {after_count['earned']} of {after_count['most']} points")
        elif made:
            if before_count:
                # The first counter read is what this run searched to; the level's guess may be lower than what was made today.
                target = made_today - made + searches_to_make(before_count["most"] - before_count["earned"], first["per_search"], 0)
            line("Bing searches", URL_BING, f"{made_today} of {target} searches today")
        if self._protection_tried:
            guard = after.get("protection")
            line("Streak protection", URL_EARN, f"switched on, {guard['days']} days left" if guard and guard["on"]
                 else "failed: not switched on")
        if claimed is None:
            line("Points", URL_HOME, f"failed: {after['ready']} points waiting, not claimed")
        gained = (after["balance"] - first["balance"]) if after["balance"] is not None and first["balance"] is not None else None
        # Today's points come from the Points breakdown panel; a run that skipped the panel can only say what it gained itself.
        top = ("Today", self._today_points) if self._today_points is not None else ("This run", max(gained, 0)) if gained is not None else None
        if top and (lines or top[1] > 0):
            balance = f", balance {after['balance']}" if after["balance"] is not None else ""
            self.notify_games.append({"title": top[0], "url": URL_HOME, "status": f"+{top[1]} points{balance}"})
            logger.info("Rewards: +%d points %s%s%s.", top[1], top[0].lower(), balance, f", {claimed} of them claimed now" if claimed else "")
        self.notify_games.extend(lines)
        if not self.notify_games:
            self.notify_games.append({"title": "Microsoft Rewards", "url": URL_HOME, "status": "already done today"})
            logger.info("Rewards: nothing new to do today.")

    @property
    def _account(self) -> str:
        return account_key(cfg.ms_rewards_email, self.user)

    async def _search_log(self) -> dict:
        """Searches made today, every query used in the last QUERY_DAYS days, why searching stopped today, and today's opened quizzes."""
        since = datetime.now(timezone.utc) - timedelta(days=QUERY_DAYS)
        async with async_session() as session:
            rows = (await session.execute(select(ClaimedGame).where(
                ClaimedGame.store == self.store_name, ClaimedGame.user == self._account,
                ClaimedGame.game_id.like("searches:%"), ClaimedGame.created_at >= since))).scalars().all()
        today = next((row_extra(r) for r in rows if r.game_id == f"searches:{self._day}"), {})
        used = {str(q).lower() for r in rows for q in row_extra(r).get("queries") or []}
        made, stopped, quizzes = _number(today.get("made")), str(today.get("stopped") or ""), set(today.get("quizzes") or [])
        quests_checked = set(today.get("quests_checked") or [])
        counter_full, earn_idle = today.get("counter_full") is True, today.get("earn_idle") is True
        logger.debug("Today (%s) so far: %d searches, %d queries used in %d days, stopped=%r, quizzes opened=%s, "
                     "punch cards checked=%d, counter full=%s, Earn idle=%s.", self._day, made, len(used), QUERY_DAYS, stopped,
                     sorted(quizzes), len(quests_checked), counter_full, earn_idle)
        return {"made": made, "used": used, "stopped": stopped, "quizzes": quizzes,
                "quests_checked": quests_checked, "counter_full": counter_full, "earn_idle": earn_idle}

    async def _today_row(self, change) -> None:
        """Change today's search row's extra data and save it."""
        async with async_session() as session:
            row, _ = await get_or_create(session, store=self.store_name, user=self._account,
                                         game_id=f"searches:{self._day}", title="Bing searches",
                                         url=URL_BING, status="searched")
            row.extra = json.dumps(change(row_extra(row)))
            await session.commit()

    async def _count_search(self, made_today: int, query: str) -> None:
        await self._today_row(lambda extra: {**extra, "made": made_today, "queries": [*(extra.get("queries") or []), query.lower()]})

    async def _stop_searches_today(self, reason: str) -> None:
        """Remember that searching stopped, so a later run the same day does not search again (mistake #6)."""
        await self._today_row(lambda extra: {**extra, "stopped": reason})

    async def _recent_cards(self) -> set:
        """The Keep earning cards opened in the last CARD_DAYS days."""
        async with async_session() as session:
            rows = (await session.execute(select(ClaimedGame).where(
                ClaimedGame.store == self.store_name, ClaimedGame.user == self._account,
                ClaimedGame.game_id.like("earn:%")))).scalars().all()
        return {r.game_id for r in rows if not older_than(row_extra(r).get("opened"), CARD_DAYS)}

    async def _remember_card(self, card: dict) -> None:
        async with async_session() as session:
            row, _ = await get_or_create(session, store=self.store_name, user=self._account, game_id=card_key(card["url"]),
                                         title=card["title"] or "Keep earning", url=card["url"], status="opened")
            row.extra = json.dumps({**row_extra(row), "opened": datetime.now(timezone.utc).isoformat()})
            await session.commit()


async def claim_ms_rewards() -> dict:
    """Convenience entry point."""
    claimer = MsRewardsClaimer()
    await claimer.run()
    return {"store": "Microsoft Rewards", "user": claimer.user, "games": claimer.notify_games}
