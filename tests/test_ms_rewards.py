"""Microsoft Rewards: reading the dashboard the way the 2026 site embeds it, and the steps that keep the account safe.

The page below copies the structure rewards.bing.com served on 2026-10-25 (Next.js flight rows), with made-up values.
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.stores.ms_rewards as rewards
from src.core.database import Base, ClaimedGame
from src.stores.ms_rewards import (MsRewardsClaimer, card_key, earn_cards, flight_rows, headline_query, is_quiz,
                                   next_query, progress, read_dashboard, ready_points, searches_to_make)


def page(*rows) -> str:
    """A page carrying these rows the way Next.js does, split over two script chunks mid-row."""
    flight = "".join(f"{i:x}:{json.dumps(row)}\n" for i, row in enumerate(rows))
    half = len(flight) // 2
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>" for part in (flight[:half], flight[half:]))
    return f"<html><body><p>Dashboard</p>{scripts}</body></html>"


def item(n: int, done: bool = False, form: str = "ML2X9G") -> dict:
    return {"date": "10/25/2026", "description": f"Item {n}", "destination": f"https://www.bing.com/search?q=item+{n}&form={form}",
            "hash": "ab" * 8, "isCompleted": done, "isLocked": "$undefined", "offerId": f"Global_DailySet_20261025_Child{n}",
            "points": 10, "title": f"Card {n}"}


def daily(*items) -> list:
    return ["$", "$L87", None, {"type": "dailyset", "model": {"streak": {"partner": "dailyset"}, "dailySetItems": list(items)}}]


def bing_streak(done: int) -> list:
    return ["$", "$L87", None, {"type": "streak", "model": {"streak": {
        "partner": "bing", "activitiesCompleted": done, "activitiesTotal": 1, "isCurrentDayCompleted": False}}}]


BALANCE = ["$", "$L6e", None, {"balance": 1250, "level": 2}]
MEMBERSHIP = ["$", "$L87", None, {"type": "membership", "model": {"membership": {
    "level": 2, "pointsPerSearch": 3, "levelBenefits": [
        {"key": "ShopToEarn", "supportedLevels": {"newLevel1": "1x", "newLevel2": "10x"}},
        {"key": "SearchAndEarn", "supportedLevels": {"newLevel1": "15", "newLevel2": "30", "newLevel3": "60"}}]}}}]
EDGE_STREAK = ["$", "$L87", None, {"type": "streak", "model": {"streak": {
    "partner": "edge", "activitiesCompleted": 30, "activitiesTotal": 30, "isCurrentDayCompleted": True}}}]
CLAIM = ["$", "$L87", None, {"type": "pointsclaim", "model": {"pointClaim": None}}]
MESSAGES = ["$", "$L36", None, {"messages": {"PointClaim": {"claimCta": "Odbierz punkty", "nothingToClaim": "Nic"}}}]
NO_EARN = (0, None, None)
DASHBOARD = page(BALANCE, MEMBERSHIP, EDGE_STREAK, bing_streak(0), daily(item(1, True), item(2), item(3, form="dsetqu")), CLAIM, MESSAGES)


class TestDashboard:
    def test_reads_the_whole_day(self):
        state = read_dashboard(DASHBOARD)
        assert state["loaded"] and state["balance"] == 1250 and state["level"] == 2
        assert (state["per_search"], state["search_points"]) == (3, 30)
        assert state["searched_today"] is False
        assert [(i["offer_id"][-6:], i["title"], i["points"], i["done"]) for i in state["daily_set"]] == [
            ("Child1", "Card 1", 10, True), ("Child2", "Card 2", 10, False), ("Child3", "Card 3", 10, False)]
        assert state["daily_set"][1]["url"] == "https://www.bing.com/search?q=item+2&form=ML2X9G"
        assert state["claim_text"] == "Odbierz punkty"

    def test_only_the_bing_streak_says_whether_today_was_searched(self):
        assert read_dashboard(page(MEMBERSHIP, bing_streak(1)))["searched_today"] is True
        assert read_dashboard(page(MEMBERSHIP, EDGE_STREAK))["searched_today"] is False

    def test_a_page_without_the_membership_is_not_the_dashboard(self):
        # Every Rewards page carries the balance in its header, /about included.
        state = read_dashboard(page(BALANCE))
        assert state["loaded"] is False and state["balance"] == 1250

    def test_missing_or_strange_values_fall_back_to_the_lowest_level(self):
        odd = ["$", "$L87", None, {"type": "membership", "model": {"membership": {
            "level": "$undefined", "pointsPerSearch": "$undefined", "levelBenefits": "$undefined"}}}]
        state = read_dashboard(page(odd))
        assert (state["level"], state["per_search"], state["search_points"]) == (1, 3, 15)

    @pytest.mark.parametrize("row", [
        ["$", "$L87", None, {"type": "membership", "model": {"membership": "$undefined"}}],
        ["$", "$L87", None, {"type": "streak", "model": {"streak": "$undefined"}}],
        ["$", "$L87", None, {"type": "streak", "model": "$undefined"}],
        ["$", "$L87", None, {"type": "dailyset", "model": {"dailySetItems": "$undefined"}}],
        ["$", "$L87", None, {"type": "dailyset", "model": {"dailySetItems": [{"destination": "$undefined"}, "$undefined"]}}],
    ])
    def test_undefined_values_never_crash_the_run(self, row):
        state = read_dashboard(page(row))
        assert state["daily_set"] == [] and state["searched_today"] is False

    @pytest.mark.parametrize("html", ["", "<html>no data</html>", "self.__next_f.push([1,\"broken"])
    def test_a_page_with_no_readable_data_reads_as_not_loaded(self, html):
        assert read_dashboard(html)["loaded"] is False and flight_rows(html) == []

    def test_non_json_rows_are_skipped(self):
        html = '<script>self.__next_f.push([1,"1:I[\\"module\\",[]]\\n2:{\\"a\\":1}\\n"])</script>'
        assert flight_rows(html) == [{"a": 1}]


class TestNumbers:
    @pytest.mark.parametrize("text,expected", [
        ("Ready to claim\n0\nClaim", 0), ("Ready to claim 1,250 Claim", 1250), ("Do odebrania 1 250 Odbierz", 1250),
        ("Ready to claim\n40\n3 days left", 40), ("40\n12/31", 40), ("", 0), (None, 0)])
    def test_ready_points(self, text, expected):
        assert ready_points(text) == expected

    @pytest.mark.parametrize("text,expected", [("0/29", (0, 29)), (" 3 / 29 ", (3, 29)), ("Keep earning", None), ("", None)])
    def test_progress(self, text, expected):
        assert progress(text) == expected

    @pytest.mark.parametrize("points,per,done,expected", [(15, 3, 0, 5), (30, 3, 2, 8), (60, 3, 25, 0), (16, 3, 0, 6),
                                                          (15, 0, 0, 5), (0, 3, 0, 0), (15, 3, -1, 5)])
    def test_searches_to_make(self, points, per, done, expected):
        assert searches_to_make(points, per, done) == expected


class TestQueries:
    class _Rng:
        def __init__(self, roll):
            self.roll = roll

        def random(self):
            return self.roll

        def choice(self, items):
            return items[0]

    def test_mostly_bing_s_own_related_searches(self):
        assert next_query(set(), ["vegan lasagna"], ["autokar prandocin"], self._Rng(0.5)) == "vegan lasagna"

    def test_then_the_day_s_news(self):
        assert next_query(set(), ["vegan lasagna"], ["autokar prandocin"], self._Rng(0.8)) == "autokar prandocin"
        assert next_query(set(), [], ["autokar prandocin"], self._Rng(0.1)) == "autokar prandocin"

    def test_the_built_in_topics_only_when_bing_offers_nothing(self):
        # Review 7.11: every install has this list, so it went out from all of them 5 to 15 times in a hundred.
        assert next_query(set(), [], [], self._Rng(0.1)) in rewards.QUERIES
        assert next_query(set(), ["vegan lasagna"], ["autokar prandocin"], self._Rng(0.95)) == "autokar prandocin"
        assert next_query(set(), ["vegan lasagna"], [], self._Rng(0.95)) == "vegan lasagna"

    @pytest.mark.parametrize("text,typed", [("autokar zderzył", "autokar zderzyl"), ("Straße nach Köln", "Strasse nach Koln"),
                                            ("ąćęłńóśźż", "acelnoszz"), ("東京の天気", "東京の天気"), ("café 4,5 €", "cafe 4,5 €")])
    def test_a_query_is_typed_without_accents(self, text, typed):
        assert rewards.plain_letters(text) == typed

    def test_a_suggestion_searched_with_its_accents_counts_as_used(self):
        assert next_query({"pogoda wroclaw"}, ["pogoda wrocław"], ["autokar zderzył"], self._Rng(0.1)) == "autokar zderzył"

    def test_nothing_used_lately_is_searched_again(self):
        used = {"vegan lasagna", "autokar prandocin", *rewards.QUERIES[:-1]}
        assert next_query(used, ["Vegan Lasagna"], ["autokar prandocin"], self._Rng(0.1)) == rewards.QUERIES[-1]

    def test_with_every_topic_used_it_starts_over(self):
        assert next_query(set(rewards.QUERIES), [], [], self._Rng(0.9)) in rewards.QUERIES

    class _Pick:
        def __init__(self, picks, more):
            self.picks, self.more = list(picks), more

        def choice(self, items):
            return items[self.picks.pop(0)]

        def randint(self, low, high):
            return min(max(self.more, low), high)

    def test_a_query_is_words_in_a_row_from_one_phrase(self):
        # Live 7.11: 8 of 9 queries were scattered words ("domagają osiągać miliardy"), which no person types.
        headline = "Tragiczna noc w Małopolsce. Autokar zderzył się z osobówką"
        assert headline_query(headline, self._Pick([1, 0], 1)) == "autokar zderzył"
        assert headline_query(headline, self._Pick([0, 0], 4)) == "tragiczna noc w małopolsce"

    def test_a_query_never_ends_on_a_short_word(self):
        # "zapłacimy w" would end on the preposition, so it runs on to the next longer word.
        assert headline_query("Paliwo: tyle zapłacimy w kolejnych dniach", self._Pick([0, 1], 1)) == "zapłacimy w kolejnych"
        assert headline_query("Noc w Małopolsce", self._Pick([0, 0], 1)) == "noc w małopolsce"
        # Probe 7.11: "sprzedawca poprosi o pesel przy" stopped on a preposition in the middle of the sentence.
        assert headline_query("Sprzedawca poprosi o PESEL przy kasie", self._Pick([0, 0], 4)) == "sprzedawca poprosi o pesel"
        assert headline_query("Sprzedawca poprosi o PESEL przy kasie", self._Pick([0, 2], 1)) == "pesel przy kasie"
        assert headline_query("Theodore Roosevelt National Park", self._Pick([0, 2], 1)) == "national park"

    def test_a_query_never_runs_across_a_full_stop_or_a_comma(self):
        for headline in ("Paliwo: tyle zapłacimy w kolejnych dniach. Kierowcy muszą się przygotować",
                         "Perła w koronie została spisana na straty, zawisł nad nią wyrok"):
            for _ in range(80):
                query = headline_query(headline)
                assert query not in ("dniach kierowcy", "straty zawisł") and not query.startswith(("dniach kierowcy", "straty zawisł"))
                assert any(query in part.lower() for part in rewards.PHRASE_END.split(headline))

    def test_a_number_with_a_comma_or_a_dot_stays_whole(self):
        assert rewards.PHRASE_END.split("Inflacja 4,5 proc. w październiku") == ["Inflacja 4,5 proc", " w październiku"]
        assert rewards.PHRASE_END.split("Ceny 2.5 raza wyższe") == ["Ceny 2.5 raza wyższe"]

    def test_every_query_is_a_piece_of_its_headline(self):
        headline = "Skargi na program Doroty Wysockiej-Schnepf w TVP po wywiadzie z premierem"
        for _ in range(60):
            query = headline_query(headline)
            assert query in headline.lower() and 2 <= len(query.split()) <= 5 and len(query.split()[-1]) > 2

    def test_two_accounts_rarely_type_the_same_query(self):
        # Words in a row give fewer pieces than scattered ones did; most of the spread comes from the day's headlines themselves.
        headline = "Skargi na program Doroty Wysockiej-Schnepf w TVP po wywiadzie z premierem"
        assert len({headline_query(headline) for _ in range(200)}) >= 8

    def test_a_short_title_keeps_its_first_words(self):
        assert headline_query("Go to it, now") == "go to it now"


class TestPaceAndPlural:
    def test_each_account_keeps_its_own_pace(self):
        assert rewards.account_pace("a@example.com") == rewards.account_pace("a@example.com")
        paces = {round(rewards.account_pace(f"user{i}@example.com"), 3) for i in range(30)}
        assert len(paces) > 15 and all(0.8 <= p <= 1.35 for p in paces)

    def test_each_account_follows_bing_s_suggestions_as_often_as_it_likes(self):
        # Many people run this bot: one share for all would be a pattern of its own.
        assert rewards.account_follow("a@example.com") == rewards.account_follow("a@example.com")
        shares = {round(rewards.account_follow(f"user{i}@example.com"), 3) for i in range(30)}
        assert len(shares) > 15 and all(0.55 <= s <= 0.85 for s in shares)

    def test_an_account_s_share_decides_between_suggestions_and_news(self):
        roll = TestQueries._Rng(0.6)
        assert next_query(set(), ["vegan lasagna"], ["autokar zderzył"], roll, follow=0.55) == "autokar zderzył"
        assert next_query(set(), ["vegan lasagna"], ["autokar zderzył"], roll, follow=0.85) == "vegan lasagna"

    @pytest.mark.parametrize("count,word,expected", [(1, "card", "1 card"), (3, "card", "3 cards"), (2, "search", "2 searches"),
                                                     (0, "step", "0 steps")])
    def test_plural(self, count, word, expected):
        assert rewards.plural(count, word) == expected


class TestQuestsToVisit:
    def test_full_and_already_checked_cards_are_skipped(self):
        cards = [{"href": "/earn/quest/A", "text": "Get started 7/7 tasks"}, {"href": "/earn/quest/B", "text": "Halloween 4/6 tasks"},
                 {"href": "/earn/quest/C", "text": "Spotify"}, {"href": "/earn/quest/D", "text": "Weekly 0/8 tasks"},
                 {"href": "/earn/quest/B", "text": "Halloween again"}, "junk"]
        assert rewards.quests_to_visit(cards, {"/earn/quest/D"}) == ["/earn/quest/B", "/earn/quest/C"]


class TestKeepEarning:
    CARDS = [
        {"url": "https://www.bing.com/search?q=Lionel+Messi&form=ML23ZW", "title": "Soccer legend"},
        {"url": "https://www.bing.com/search?q=socrates&form=ML23ZW", "title": "It's all Greek"},
        {"url": "https://microsoftedgewelcome.microsoft.com/get-started?focus=privacy&form=ML23ZX", "title": "Lock and key"},
        {"url": "https://rewards.bing.com/referandearn/", "title": "Refer and earn"},
        {"url": "https://www.bing.com/set/browserextension/rewards", "title": "Extension"},
        {"url": "https://www.bing.com.evil.example/search?q=x", "title": "Fake host"},
        {"url": "https://www.bing.com/search?form=ML23ZW", "title": "No query"},
    ]

    def test_only_bing_searches_not_opened_lately(self):
        todo = earn_cards(self.CARDS, {card_key(self.CARDS[1]["url"])})
        assert [c["title"] for c in todo] == ["Soccer legend"]

    def test_a_quiz_card_is_not_opened(self):
        quiz = {"url": "https://www.bing.com/search?q=Bing+news+quiz&form=ML2BF1", "title": "News quiz"}
        assert earn_cards([quiz, self.CARDS[0]], set()) == [self.CARDS[0]]

    def test_a_card_is_remembered_by_its_search(self):
        assert card_key("https://www.bing.com/search?q=Lionel+Messi&form=ML23ZW") == "earn:lionel messi"


def section(sid: str, done: bool = False) -> list:
    return ["$", "$L49", None, {"id": sid, "title": sid.title(), "impression": "$undefined", "defaultExpanded": False,
                                "discovery": {"offerId": f"Discovery_Dashboard_{sid}", "hash": "ab", "isCompleted": done, "points": 5},
                                "featureEdu": {"title": "Hint"}, "children": []}]


def task(title: str, url: str, steps="$undefined", done: bool = False, locked: bool = False, kind: str = "urlreward") -> dict:
    return {"ctaUrl": url, "hash": "cd", "isCompleted": done, "isLocked": locked, "offerId": f"WW_pcchild_{title.replace(' ', '_')}",
            "points": 10, "progressMax": steps, "title": title, "type": kind}


QUEST = ["$", "$L87", None, {"type": "quest", "model": {"quest": {"name": "WW_pcparent_onboarding", "isCompleted": False, "activities": [
    task("Set a Rewards goal", "/redeem/?section=shop"), task("Search 1 time", "https://www.bing.com/", 1),
    task("Finish a Daily Set", "/dashboard?section=dailyset", 3), task("Browse the Earn page", "/earn"),
    task("Learn your level", "/about?section=benefits"), task("Explore the dashboard", "/dashboard/", 5),
    task("Locked one", "/earn", locked=True), task("Done one", "/about", done=True), task("A quiz", "/quiz", kind="quiz")]}}}]
GUARD = ["$", "$L87", None, {"type": "dailystreak", "model": {"streakCounter": 0, "streakProtection": {"isProtectionOn": False, "remainingDays": 14}}}]
BOOST = ["$", "$L5", None, {"promotionalItem": {"offerId": "WW_search_optin_3x_oct", "title": "Search 3x more", "description": "Opt in"}}]
EXPLORE = page(BALANCE, MEMBERSHIP, bing_streak(0), section("snapshot"), section("dailyset", True), QUEST, GUARD, BOOST)


class TestDashboardTasks:
    def test_reads_sections_quest_tasks_protection_and_boosts(self):
        state = read_dashboard(EXPLORE)
        assert state["sections"] == [{"id": "snapshot", "title": "Snapshot", "done": False}, {"id": "dailyset", "title": "Dailyset", "done": True}]
        assert state["protection"] == {"on": False, "days": 14}
        assert state["perks"] == [{"offer_id": "WW_search_optin_3x_oct", "title": "Search 3x more"}]
        assert len(state["quests"]) == 1 and len(state["quests"][0]["tasks"]) == 9

    def test_only_one_visit_tasks_are_opened(self):
        assert [t["title"] for t in rewards.quest_tasks_to_open(read_dashboard(EXPLORE))] == ["Browse the Earn page", "Learn your level"]

    def test_the_reward_goal_is_left_for_you_to_choose(self):
        # Owner, 2026-10-29: the goal opens a dialog with an auto-redeem choice, and which reward to aim for is yours.
        assert "Set a Rewards goal" not in [t["title"] for t in rewards.quest_tasks_to_open(read_dashboard(EXPLORE))]
        assert "/redeem/sku/" not in Path(rewards.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize("offer,expected", [
        ({"offerId": "ENUS_search_optin_2x"}, True), ({"offerId": "x", "description": "Search 10x more today"}, True),
        ({"offerId": "Global_DailySet_20261026_Child1", "title": "Search the news"}, False), ({}, False)])
    def test_search_boost_offers(self, offer, expected):
        assert rewards.is_search_boost(offer) is expected

    def test_finders_are_scoped_and_quoted(self):
        selector = json.loads(rewards.section_toggle("dailyset").split("querySelector(", 1)[1].rsplit(")", 1)[0])
        assert selector == 'section[id="dailyset"] button[slot=trigger][aria-expanded=false]'
        assert "section[id^=quest_] a[href]" in rewards.href_finder("/earn", "section[id^=quest_]")
        assert '"a[href]"' in rewards.href_finder("/earn")

    def test_the_flow_unfolds_sections_closes_hints_and_comes_back_after_each_task(self, stub):
        steps = []

        async def click(finder, label):
            steps.append(label)
            return label != "Rewards dialog"

        async def opened(finder, label):
            steps.append(f"open {label}")
            return True

        async def where():
            return "https://rewards.bing.com/earn"

        async def back(url, timeout=60):
            steps.append(f"back {url}")

        stub._click_like_a_hand, stub._open_card, stub._current_url, stub._open = click, opened, where, back
        asyncio.run(stub._do_dashboard_tasks(read_dashboard(EXPLORE)))
        assert steps[:2] == ["Snapshot section", "Rewards dialog"]
        # The tasks come in a fresh order each run; each is followed by closing its hint and coming back.
        groups = [steps[2:5], steps[5:8]]
        assert sorted(g[0] for g in groups) == ["open Browse the Earn page", "open Learn your level"]
        assert all(g[1:] == ["Rewards dialog", f"back {rewards.URL_HOME}"] for g in groups)

    def test_report_names_what_counted(self, stub):
        first = dict(read_dashboard(EXPLORE), ready=0)
        done = page(MEMBERSHIP, section("snapshot", True), section("dailyset", True), ["$", "$L87", None, {"type": "quest", "model": {"quest": {
            "activities": [task("Browse the Earn page", "/earn", done=True), task("Learn your level", "/about?section=benefits", done=True)]}}}])
        stub._report(first, dict(read_dashboard(done), ready=0), NO_EARN, 0, 0, 0, 0)
        # No balance on the page after, so no points line: a guess would be a wrong number.
        assert [(g["title"], g["status"]) for g in stub.notify_games] == [("Dashboard", "3 tasks done")]

    def test_report_names_what_did_not_count(self, stub, caplog):
        first = dict(read_dashboard(EXPLORE), ready=0)
        with caplog.at_level(logging.INFO, logger="fgc.msrewards"):
            stub._report(first, first, NO_EARN, 0, 0, 0, 0)
        assert [(g["title"], g["status"]) for g in stub.notify_games] == [
            ("This run", "+0 points, balance 1250"), ("Dashboard", "failed: 0 of 3 tasks done")]
        # The titles go to the log only: the summary hides a line with "already" or "skip" in it, and a title may hold one.
        assert "not counted: Snapshot, Browse the Earn page, Learn your level" in caplog.text


class TestAccountWarning:
    def test_a_healthy_dashboard_has_no_warning(self):
        assert rewards.account_warning(DASHBOARD, read_dashboard(DASHBOARD)) == ""

    def test_microsoft_s_bot_warning_stops_the_run(self):
        html = DASHBOARD.replace("Dashboard</p>", 'Dashboard</p><div data-k="Fraud_UserWarning_BotScore_UX"></div>')
        assert "bot warning" in rewards.account_warning(html, read_dashboard(html))

    def test_daily_streaks_gone_is_how_a_limited_account_looks(self):
        bare = page(BALANCE, MEMBERSHIP, CLAIM)
        assert "streaks are gone" in rewards.account_warning(bare, read_dashboard(bare))

    def test_a_page_that_is_not_the_dashboard_raises_no_warning(self):
        about = page(BALANCE)
        assert rewards.account_warning(about, read_dashboard(about)) == ""


class TestSearchCounter:
    """Points breakdown shows the day's search points as "15" and "/15" in two spans (seen live on 2026-10-26)."""

    @pytest.mark.parametrize("text,expected", [("15/15", {"earned": 15, "most": 15}), ("6 / 15", {"earned": 6, "most": 15}),
                                               ("1,200/1,500", {"earned": 1200, "most": 1500}), ("40/30", {"earned": 30, "most": 30})])
    def test_reads_the_counter(self, text, expected):
        assert rewards.search_counter(text) == expected

    @pytest.mark.parametrize("text", ["", "Bing search", None])
    def test_no_counter_is_none(self, text):
        assert rewards.search_counter(text) is None


class TestQuiz:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.bing.com/search?q=Zagreb+quiz&form=dsetqu&filters=IsConversation:%22True%22", True),
        ("https://www.bing.com/search?q=Croatia&form=dsetqu", True),
        # A plain card whose address names the quiz programme is still a plain card.
        ("https://www.bing.com/search?q=Comedy+events+near+me&form=ML2X9G&filters=BTEPOKey%3A%22REWARDSQUIZ_DailySet_UrlOffer%22", False),
        ("", False),
    ])
    def test_which_daily_set_cards_ask_questions(self, url, expected):
        assert is_quiz(url) is expected


# ----- Flows on a stub: no browser, no account -----

class _Page:
    def __init__(self, answers=None):
        self.answers, self.keys = answers or {}, []

    async def send(self, command):
        params = next(command)["params"]
        self.keys.append((params["type"], params["key"]))

    async def evaluate(self, expression, **kwargs):
        for marker, values in self.answers.items():
            if marker in expression:
                return values.pop(0) if isinstance(values, list) else values
        return False


class _Stub(MsRewardsClaimer):
    def __init__(self):
        super().__init__()
        self.clicks, self.filled, self.searches, self.counted, self.remembered = [], [], [], [], []
        self.page = _Page()

    async def sleep(self, seconds):
        return None

    async def take_screenshot(self, name, failed=True):
        return None

    async def _click_like_a_hand(self, finder, label):
        self.clicks.append(label)
        return False


async def _nothing(*args, **kwargs):
    return None


def status(stub, title: str) -> str:
    """The status of one summary line, found by its title."""
    return next(g["status"] for g in stub.notify_games if g["title"] == title)


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(rewards.cfg, "dryrun", False)
    return _Stub()


class TestDryRun:
    def test_it_reads_the_day_and_clicks_nothing(self, stub, monkeypatch):
        monkeypatch.setattr(rewards.cfg, "dryrun", True)
        state = dict(read_dashboard(DASHBOARD), ready=40, warning="")

        async def dashboard(load=True, shot=""):
            return state

        async def log():
            return {"made": 3, "used": set(), "stopped": "", "quizzes": set(), "quests_checked": set(), "counter_full": False, "earn_idle": False}

        async def url():
            return "https://rewards.bing.com/dashboard"

        stub._read_dashboard, stub._search_log, stub._current_url = dashboard, log, url
        asyncio.run(stub._collect())
        assert stub.clicks == []
        assert stub.notify_games == [{"title": "Microsoft Rewards", "url": rewards.URL_HOME,
                                      "status": "0 dashboard task(s), 2 daily set item(s), 7 search(es), 40 points to claim (dry run)"}]


class TestStandDown:
    def _run(self, stub, ready):
        state = dict(read_dashboard(DASHBOARD), ready=ready, warning="Microsoft shows its bot warning on the dashboard")
        done = []

        async def dashboard(load=True, shot=""):
            return state

        async def url():
            return "https://rewards.bing.com/dashboard"

        async def claim(st):
            done.append("claim")
            return 30

        async def forbidden(*args, **kwargs):
            raise AssertionError("a limited account must not get cards or searches")

        stub._read_dashboard, stub._current_url, stub._claim = dashboard, url, claim
        stub._do_daily_set = stub._do_keep_earning = stub._do_searches = stub._search_log = forbidden
        asyncio.run(stub._collect())
        return done

    def test_a_limited_account_gets_no_cards_and_no_searches_only_its_points_claimed(self, stub):
        assert self._run(stub, 40) == ["claim"]
        assert [g["status"] for g in stub.notify_games] == ["failed: Microsoft limited the account", "30 claimed, balance 1250"]

    def test_it_never_presses_a_button_in_a_dialog_that_may_be_the_warning(self, stub):
        clicks = []

        async def click(finder, label):
            clicks.append(label)
            return False

        stub._click_like_a_hand = click
        self._run(stub, 40)
        assert "Rewards dialog" not in clicks

    def test_a_claim_that_fails_while_limited_is_reported(self, stub):
        state = dict(read_dashboard(DASHBOARD), ready=40, warning="the daily streaks are gone from the dashboard")

        async def no_claim(st):
            return None

        stub._claim, stub._accept_cookies = no_claim, _nothing
        asyncio.run(stub._stand_down(state))
        assert [g["status"] for g in stub.notify_games] == ["failed: Microsoft limited the account", "failed: 40 points waiting, not claimed"]

    def test_with_nothing_to_claim_it_does_nothing_at_all(self, stub):
        assert self._run(stub, 0) == []
        assert [g["status"] for g in stub.notify_games] == ["failed: Microsoft limited the account"]


class TestSearchesStop:
    def _run(self, stub, results, checks, planned=5, counters=None):
        async def search(query):
            stub.searches.append(query)
            return results.pop(0) if results else True

        async def check():
            return checks.pop(0) if checks else ""

        async def count(n, query):
            stub.counted.append((n, query))

        counters = list(counters or [])

        async def counter():
            stub.counter_reads += 1
            return counters.pop(0) if counters else None

        async def stop(reason):
            stub.stopped.append(reason)

        async def opened(url, timeout=60):
            stub.pages.append(url)

        # The counter before the searches is read on the Earn page before _do_searches() starts.
        stub._counter_before = counters.pop(0) if counters else None
        stub.counter_reads, stub.stopped, stub.pages = 0, [], []
        stub._search, stub._human_challenge_reason, stub._count_search, stub._read_breakdown = search, check, count, counter
        stub._open, stub._accept_cookies, stub._read_results, stub._read_headlines = opened, _nothing, _nothing, _nothing
        stub._stop_searches_today = stop
        return asyncio.run(stub._do_searches(planned, 2, 3))

    def test_bing_s_check_is_counted(self, stub, monkeypatch):
        import src.core.claimer as claimer_module
        seen = []
        monkeypatch.setattr(claimer_module, "record_check", lambda store, puzzle, where="": seen.append((store, puzzle)))
        self._run(stub, [], ["frame: https://www.bing.com/turing/captcha"], planned=3)
        assert seen == [("msrewards", False)] and stub._bing_check

    def test_the_query_is_typed_and_kept_without_accents(self, stub):
        stub._headlines, stub._suggestions = ["autokar zderzył"], []
        self._run(stub, [], [], planned=1)
        assert stub.searches == ["autokar zderzyl"] and "autokar zderzyl" in stub._used

    def test_microsoft_s_counter_sets_how_many_searches_are_left(self, stub):
        counters = [{"earned": 6, "most": 15}, {"earned": 15, "most": 15}]
        assert self._run(stub, [], [], planned=5, counters=counters) == 3
        assert stub._counter_before == {"earned": 6, "most": 15} and stub._counter_after == {"earned": 15, "most": 15}

    def test_after_the_searches_the_counter_is_read_once_back_on_the_earn_page(self, stub):
        self._run(stub, [], [], planned=5, counters=[{"earned": 0, "most": 15}, {"earned": 15, "most": 15}])
        assert stub.counter_reads == 1 and stub.pages == [rewards.URL_BING, rewards.URL_EARN]

    def test_a_counter_that_did_not_move_stops_searching_for_the_day(self, stub):
        assert self._run(stub, [], [], planned=5, counters=[{"earned": 0, "most": 15}, {"earned": 0, "most": 15}]) == 5
        assert stub._not_counted and stub.stopped == ["counter did not move"]

    def test_a_counter_that_moved_stops_nothing(self, stub):
        assert self._run(stub, [], [], planned=5, counters=[{"earned": 0, "most": 15}, {"earned": 15, "most": 15}]) == 5
        assert not stub._not_counted and stub.stopped == []

    def test_an_unreadable_counter_after_the_searches_is_not_taken_for_a_stop(self, stub):
        assert self._run(stub, [], [], planned=5, counters=[{"earned": 0, "most": 15}]) == 5
        assert not stub._not_counted and stub.stopped == [] and stub._counter_after is None

    def test_a_full_counter_means_no_search_and_no_bing_visit(self, stub):
        assert self._run(stub, [], [], planned=5, counters=[{"earned": 15, "most": 15}]) == 0
        assert stub.searches == [] and stub.pages == []

    def test_a_check_on_bing_ends_the_searching_for_the_day_and_is_reported(self, stub):
        assert self._run(stub, [True, True, True], ["", "widget: hcaptcha"]) == 1
        assert len(stub.searches) == 2 and [n for n, _ in stub.counted] == [3] and stub._bing_check
        assert stub.stopped == ["Bing check"]

    def test_two_empty_pages_are_reported_when_nothing_was_searched(self, stub):
        assert self._run(stub, [False, False], []) == 0 and stub._no_results
        state = dict(read_dashboard(page(MEMBERSHIP)), ready=0)
        stub._report(state, state, NO_EARN, 0, 0, 5, 0)
        assert status(stub, "Bing searches") == "failed: Bing showed no results"

    def test_a_query_whose_search_failed_is_not_picked_again(self, stub):
        self._run(stub, [False, True], [], planned=2)
        assert len(set(stub.searches)) == 2 and set(stub.searches) <= stub._used

    def test_a_check_instead_of_results_is_not_retried_as_an_empty_page(self, stub):
        assert self._run(stub, [False], ["page title: verify"]) == 0
        assert len(stub.searches) == 1

    def test_two_empty_results_in_a_row_end_it(self, stub):
        assert self._run(stub, [True, False, False, True], []) == 1
        assert len(stub.searches) == 3 and not stub._bing_check

    def test_every_search_is_logged_with_its_query_and_none_repeats(self, stub):
        assert self._run(stub, [], [], planned=3) == 3
        assert [n for n, _ in stub.counted] == [3, 4, 5]
        assert len({q for _, q in stub.counted}) == 3


class TestSignIn:
    def _run(self, stub, fields, url="https://login.live.com/oauth20_authorize.srf", kmsi=False):
        async def visible(selectors):
            return fields.get(selectors[0], "")

        async def fill(selector, text, label):
            stub.filled.append(label)
            return True

        async def no(*args, **kwargs):
            return False

        async def here():
            return url

        stub.page = _Page({"KmsiCheckboxField": kmsi})
        stub._visible_field, stub._fill, stub._is_logged_in = visible, fill, no
        stub._human_challenge_present, stub._current_url, stub._log_page_state = no, here, _nothing
        return asyncio.run(stub._sign_in_by_hand())

    def test_a_refused_password_is_typed_once_only(self, stub, monkeypatch):
        monkeypatch.setattr(rewards.cfg, "ms_rewards_password", "secret")
        assert self._run(stub, {MsRewardsClaimer.PASSWORD_FIELDS[0]: "#passwordEntry"}) is False
        assert stub.filled == ["password field"]

    def test_a_code_without_an_authenticator_key_is_left_to_you(self, stub, monkeypatch):
        monkeypatch.setattr(rewards.cfg, "ms_rewards_otp_key", None)
        assert self._run(stub, {MsRewardsClaimer.CODE_FIELDS[0]: "#codeEntry"}) is False
        assert stub.filled == []

    def test_an_unknown_page_gets_a_few_looks_then_hands_over_without_guessing(self, stub):
        assert self._run(stub, {}) is False
        assert stub.filled == [] and len(stub.clicks) == 2 * rewards.UNKNOWN_LOOKS
        assert set(stub.clicks) == {"sign-in method: Use your password", "sign-in method: Other ways to sign in"}

    def test_after_the_password_only_stay_signed_in_gets_its_button_pressed(self, stub):
        self._run(stub, {}, url="https://login.live.com/ppsecure/post.srf", kmsi=False)
        assert "Stay signed in" not in stub.clicks
        stub.clicks.clear()
        self._run(stub, {}, url="https://login.live.com/ppsecure/post.srf", kmsi=True)
        assert "Stay signed in" in stub.clicks


class TestProfile:
    def test_the_microsoft_store_account_shares_its_profile(self, monkeypatch):
        monkeypatch.setattr(rewards.cfg, "ms_email", "Me@Example.com")
        monkeypatch.setattr(rewards.cfg, "ms_rewards_email", "me@example.com")
        assert MsRewardsClaimer().profile_name == "microsoft"

    def test_an_account_of_its_own_gets_its_own_profile(self, monkeypatch):
        monkeypatch.setattr(rewards.cfg, "ms_email", "me@example.com")
        monkeypatch.setattr(rewards.cfg, "ms_rewards_email", "rewards@example.com")
        claimer = MsRewardsClaimer()
        assert claimer.profile_name == "" and claimer.store_name == "msrewards"


class TestKeepEarningFlow:
    def test_cards_are_opened_remembered_and_the_counter_says_how_many_counted(self, stub):
        cards = [{"url": "https://www.bing.com/search?q=a&form=ML23ZW", "title": "A"},
                 {"url": "https://www.bing.com/search?q=b&form=ML23ZW", "title": "B"},
                 {"url": "https://rewards.bing.com/referandearn/", "title": "Refer"}]
        stub.page = _Page({"progressbar": "", "moreactivities a[href]": json.dumps(cards), "moreactivities": ["0/29", "1/29"]})

        async def recent():
            return {"earn:b"}

        async def opened(finder, label):
            return True

        async def remember(card):
            stub.remembered.append(card["title"])

        stub._recent_cards, stub._open_card, stub._remember_card, stub._open = recent, opened, remember, _nothing
        assert asyncio.run(stub._do_keep_earning()) == (1, (0, 29), (1, 29))
        assert stub.remembered == ["A"]

    def test_cards_the_page_did_not_count_are_not_remembered_so_they_come_back(self, stub):
        cards = [{"url": "https://www.bing.com/search?q=a&form=ML23ZW", "title": "A"}]
        stub.page = _Page({"progressbar": "", "moreactivities a[href]": json.dumps(cards), "moreactivities": ["3/29", "3/29"]})

        async def recent():
            return set()

        async def opened(finder, label):
            return True

        async def remember(card):
            stub.remembered.append(card["title"])

        stub._recent_cards, stub._open_card, stub._remember_card, stub._open = recent, opened, remember, _nothing
        assert asyncio.run(stub._do_keep_earning()) == (1, (3, 29), (3, 29)) and stub.remembered == []

    def test_cards_that_would_not_open_are_a_failure_and_keep_the_earn_page_for_later(self, stub):
        cards = [{"url": "https://www.bing.com/search?q=a&form=ML23ZW", "title": "A"}]
        stub.page = _Page({"progressbar": "", "moreactivities a[href]": json.dumps(cards), "moreactivities": "3/29"})
        rows = []

        async def recent():
            return set()

        async def not_opened(finder, label):
            return False

        async def today_row(change):
            rows.append(change({}))

        stub._recent_cards, stub._open_card, stub._today_row = recent, not_opened, today_row
        earn = asyncio.run(stub._do_keep_earning())
        assert earn == (0, (3, 29), (3, 29)) and stub._earn_todo == 1
        state = dict(read_dashboard(DASHBOARD), ready=0)
        asyncio.run(stub._note_earn_idle({"counter_full": True}, earn, state))
        assert rows == []
        stub._report(state, state, earn, 0, 0, 0, 0)
        assert status(stub, "Keep earning") == "failed: 0 of 1 card opened"

    def test_an_earn_page_with_nothing_left_is_skipped_by_later_runs_today(self, stub):
        rows = []

        async def today_row(change):
            rows.append(change({}))

        stub._today_row = today_row
        stub._counter_after = {"earned": 15, "most": 15}
        state = dict(read_dashboard(DASHBOARD), ready=0)
        asyncio.run(stub._note_earn_idle({"counter_full": False}, NO_EARN, state))
        assert rows == [{"earn_idle": True}]

    def test_protection_that_did_not_switch_on_keeps_the_earn_page_for_later(self, stub):
        rows = []

        async def today_row(change):
            rows.append(change({}))

        stub._today_row, stub._protection_tried = today_row, True
        state = dict(read_dashboard(DASHBOARD), ready=0, protection={"on": False, "days": 14})
        asyncio.run(stub._note_earn_idle({"counter_full": True}, NO_EARN, state))
        assert rows == []

    def test_cards_opened_without_the_counter_moving_are_a_failure(self, stub):
        stub._report(read_dashboard(page(MEMBERSHIP)), dict(read_dashboard(page(MEMBERSHIP)), ready=0),
                     (2, (5, 29), (5, 29)), 0, 0, 0, 0)
        assert status(stub, "Keep earning") == "failed: 2 cards opened, not counted"

    def test_cards_the_page_counted_only_in_part_all_come_back_next_run(self, stub):
        cards = [{"url": "https://www.bing.com/search?q=a&form=ML23ZW", "title": "A"},
                 {"url": "https://www.bing.com/search?q=b&form=ML23ZW", "title": "B"}]
        stub.page = _Page({"progressbar": "", "moreactivities a[href]": json.dumps(cards), "moreactivities": ["20/29", "21/29"]})

        async def recent():
            return set()

        async def opened(finder, label):
            return True

        async def remember(card):
            stub.remembered.append(card["title"])

        stub._recent_cards, stub._open_card, stub._remember_card, stub._open = recent, opened, remember, _nothing
        earn = asyncio.run(stub._do_keep_earning())
        # Which of the two did not count cannot be told, so neither is kept away for a week.
        assert earn == (2, (20, 29), (21, 29)) and stub.remembered == []
        state = dict(read_dashboard(page(MEMBERSHIP)), ready=0)
        stub._report(state, state, earn, 0, 0, 0, 0)
        assert status(stub, "Keep earning") == "failed: 2 cards opened, 1 counted"


class TestDismissDialogs:
    def test_a_dialog_its_button_closed_needs_no_escape(self, stub):
        answers = [True, False]
        clicks = []

        async def click(finder, label):
            clicks.append(label)
            return answers.pop(0)

        stub._click_like_a_hand = click
        asyncio.run(stub._dismiss_dialogs())
        assert clicks == ["Rewards dialog", "Rewards dialog"] and stub.page.keys == []

    def test_escape_is_pressed_only_for_a_window_its_button_did_not_close(self, stub):
        async def click(finder, label):
            return False

        stub._click_like_a_hand = click
        stub.page = _Page({"!![...document": True})
        asyncio.run(stub._dismiss_dialogs())
        assert stub.page.keys == [("keyDown", "Escape"), ("keyUp", "Escape")]


class TestSearchesStoppedForTheDay:
    def test_a_stop_earlier_today_means_no_searches_this_run(self, stub):
        state = dict(read_dashboard(DASHBOARD), ready=0, warning="")
        searched = []

        async def dashboard(load=True, shot=""):
            return state

        async def log():
            return {"made": 2, "used": set(), "stopped": "Bing check", "quizzes": set(), "quests_checked": set(), "counter_full": False, "earn_idle": False}

        async def url():
            return "https://rewards.bing.com/dashboard"

        async def searches(*args):
            searched.append(args)
            return 0

        async def earn():
            return NO_EARN

        async def claim(st):
            return 0

        stub._read_dashboard, stub._search_log, stub._current_url, stub._do_searches = dashboard, log, url, searches
        stub._close_dialogs, stub._do_daily_set, stub._do_keep_earning, stub._claim = _nothing, _nothing, earn, claim
        stub._read_breakdown, stub._protect_streak, stub._today_row = _nothing, _nothing, _nothing
        stub._do_earn_page = lambda *args: earn()
        asyncio.run(stub._collect())
        assert searched == []

    def test_the_counter_decides_even_when_the_level_says_the_searches_are_made(self, stub):
        state = dict(read_dashboard(DASHBOARD), ready=0, warning="")
        searched = []

        async def dashboard(load=True, shot=""):
            return state

        async def log():
            # Level 2 gives 10 searches a day and 10 were made, but Microsoft may have counted fewer.
            return {"made": 10, "used": set(), "stopped": "", "quizzes": set(), "quests_checked": set(), "counter_full": False, "earn_idle": False}

        async def url():
            return "https://rewards.bing.com/dashboard"

        async def searches(*args):
            searched.append(args)
            return 0

        async def earn(*args):
            return NO_EARN

        async def claim(st):
            return 0

        stub._read_dashboard, stub._search_log, stub._current_url, stub._do_searches = dashboard, log, url, searches
        stub._close_dialogs, stub._do_daily_set, stub._do_dashboard_tasks, stub._claim = _nothing, _nothing, _nothing, claim
        stub._do_earn_page, stub._today_row = earn, _nothing
        asyncio.run(stub._collect())
        assert searched == [(0, 10, 3)]


class TestQuizzesOncePerDay:
    def _run(self, stub, opened_today):
        state = dict(read_dashboard(DASHBOARD), ready=0, warning="")
        daily, rows = [], []

        async def dashboard(load=True, shot=""):
            return state

        async def log():
            return {"made": 5, "used": set(), "stopped": "", "quizzes": opened_today, "quests_checked": set(), "counter_full": False, "earn_idle": False}

        async def url():
            return "https://rewards.bing.com/dashboard"

        async def do_daily(items):
            daily.extend(i["title"] for i in items)

        async def today_row(change):
            rows.append(change({}))

        async def earn():
            return NO_EARN

        async def claim(st):
            return 0

        stub._read_dashboard, stub._search_log, stub._current_url, stub._do_daily_set = dashboard, log, url, do_daily
        stub._close_dialogs, stub._do_keep_earning, stub._claim, stub._today_row = _nothing, earn, claim, today_row
        async def no_searches(*args):
            return 0

        stub._read_breakdown, stub._protect_streak, stub._do_searches = _nothing, _nothing, no_searches
        stub._do_earn_page = lambda *args: earn()
        asyncio.run(stub._collect())
        return daily, rows

    def test_a_quiz_is_opened(self, stub):
        daily, _ = self._run(stub, set())
        assert sorted(daily) == ["Card 2", "Card 3"]

    def test_a_quiz_opened_earlier_today_is_not_opened_again(self, stub):
        daily, _ = self._run(stub, {"Global_DailySet_20261025_Child3"})
        assert daily == ["Card 2"]

    def test_opening_the_set_notes_its_quiz_for_the_day(self, stub):
        rows = []

        async def opened(finder, label):
            return True

        async def today_row(change):
            rows.append(change({}))

        stub._open_card, stub._today_row = opened, today_row
        asyncio.run(stub._do_daily_set([i for i in read_dashboard(DASHBOARD)["daily_set"] if not i["done"]]))
        assert rows == [{"quizzes": ["Global_DailySet_20261025_Child3"]}]

    def test_a_quiz_that_did_not_open_is_not_noted_and_the_set_is_reported_failed(self, stub):
        rows = []

        async def not_opened(finder, label):
            return False

        async def today_row(change):
            rows.append(change({}))

        stub._open_card, stub._today_row = not_opened, today_row
        first = dict(read_dashboard(DASHBOARD), ready=0)
        asyncio.run(stub._do_daily_set([i for i in first["daily_set"] if not i["done"]]))
        assert rows == [] and stub._quizzes_today == set()
        stub._report(first, first, NO_EARN, 0, 0, 0, 0)
        # Not "the quiz counts later": it was never opened, and the next run opens it.
        assert status(stub, "Daily set") == "failed: 1 of 3 done"


def step(n: int, label: str, href: str, done: bool = False, locked: bool = False) -> dict:
    return {"ariaLabel": label, "hash": "ef", "href": href, "isCompleted": done, "isLocked": locked, "linkText": label.split(", ")[0],
            "offerId": f"WW_pcchild{n}_HalloweenQuest_punchcard"}


# The shape a punch card's own page served on 2026-10-26: steps as links, done ones without a button.
QUEST_PAGE = page(BALANCE, ["$", "$L81", None, {"children": [
    step(1, "Set a Goal, Set a Rewards goal", "/redeem/?section=shop"),
    step(2, "Earn now, Browse the Earn page", "/earn"),
    step(3, "Search now, Spooky snacks", "https://www.bing.com/search?q=halloween+snacks&form=ML2HW1"),
    step(4, "Explore now, Explore the dashboard", "/dashboard/"),
    step(5, "Search now, Search for 7 days", "https://www.bing.com/"),
    step(6, "Listen, Spotify playlist", "https://open.spotify.com/playlist/abc", locked=True),
    step(7, "Done, Already done", "/about", done=True),
    {"href": "https://www.bing.com/search?q=x", "offerId": "Global_DailySet_20261026_Child1", "isCompleted": False}]}])


class TestPunchCards:
    def test_reads_the_steps_and_leaves_other_offers_out(self):
        steps = rewards.quest_page_steps(QUEST_PAGE)
        assert [s["offer_id"][:10] for s in steps] == ["WW_pcchild"] * 7
        assert steps[1] == {"offer_id": "WW_pcchild2_HalloweenQuest_punchcard", "title": "Browse the Earn page",
                            "label": "Earn now, Browse the Earn page", "url": "/earn", "done": False, "locked": False}

    def test_only_steps_one_visit_completes(self):
        steps = rewards.quest_page_steps(QUEST_PAGE)
        assert [s["title"] for s in rewards.quest_steps_to_open(steps)] == ["Browse the Earn page", "Spooky snacks"]

    def test_a_step_is_found_by_its_label_not_by_the_footer_link(self):
        finder = rewards.step_finder("/earn", "Earn now, Browse the Earn page")
        assert '"/earn"' in finder and '"Earn now, Browse the Earn page"' in finder and "aria-label" in finder

    def test_the_flow_opens_each_step_and_counts_those_gone_from_the_page(self, stub):
        after = page(BALANCE, ["$", "$L81", None, {"children": [step(1, "Set a Goal, Set a Rewards goal", "/redeem/?section=shop"),
                                                               step(3, "Search now, Spooky snacks", "https://www.bing.com/search?q=halloween+snacks&form=ML2HW1")]}])
        stub.page = _Page({"outerHTML": [QUEST_PAGE, after]})
        opened, pages, rows = [], [], []

        async def open_card(finder, label):
            opened.append(label)
            return True

        async def go(url, timeout=60):
            pages.append(url)

        async def here():
            return "https://rewards.bing.com/earn/quest/WW_pcparent_HalloweenQuest_punchcard"

        async def today_row(change):
            rows.append(change({}))

        stub._open_card, stub._open, stub._current_url, stub._today_row = open_card, go, here, today_row
        asyncio.run(stub._do_quests(["/earn/quest/WW_pcparent_HalloweenQuest_punchcard"]))
        # The steps are taken in a fresh order each run.
        assert sorted(opened) == ["Browse the Earn page", "Spooky snacks"]
        assert stub._quest_done == ["WW_pcchild2_HalloweenQuest_punchcard"]
        # The card is visited once a day; its steps need no list of their own.
        assert rows == [{"quests_checked": ["/earn/quest/WW_pcparent_HalloweenQuest_punchcard"]}]

    def test_a_step_that_would_not_open_is_reported_not_left_out(self, stub):
        stub.page = _Page({"outerHTML": [QUEST_PAGE, QUEST_PAGE]})

        async def not_opened(finder, label):
            return False

        async def here():
            return "https://rewards.bing.com/earn/quest/WW_pcparent_HalloweenQuest_punchcard"

        stub._open_card, stub._open, stub._current_url, stub._today_row = not_opened, _nothing, here, _nothing
        asyncio.run(stub._do_quests(["/earn/quest/WW_pcparent_HalloweenQuest_punchcard"]))
        state = dict(read_dashboard(page(MEMBERSHIP)), ready=0)
        stub._report(state, state, NO_EARN, 0, 0, 0, 0)
        assert status(stub, "Punch cards") == "failed: 0 of 2 steps done"

    # Seen on 2026-10-28: "Search on your desktop for 5 out of 10 days" and Spotify's "search for three days" (in Edge).
    @pytest.mark.parametrize("url,opened", [
        ("https://www.bing.com?form=ML2X8X&OCID=ML2X8X&PUBL=RewardsDO&CREA=ML2X8X", False),
        ("https://www.bing.com/search?form=ML2YBF&OCID=ML2YBF&PUBL=RewardsDO&CREA=ML2YBF", False),
        ("https://www.bing.com/", False), ("https://bing.com/?form=ML2QST", False),
        ("https://www.bing.com/search?q=halloween+costumes&form=ML2HW1", True), ("https://www.bing.com/apps/wallpaper", True),
        ("https://www.bing.com.evil.example/?form=x", True)])
    def test_steps_that_need_searches_on_several_days_are_left_to_the_daily_searches(self, url, opened):
        steps = [{"offer_id": "WW_pcchild1_x", "title": "A step", "label": "Go, A step", "url": url, "done": False, "locked": False}]
        assert bool(rewards.quest_steps_to_open(steps)) is opened

    def test_report_names_the_steps_that_did_not_count(self, stub, caplog):
        state = dict(read_dashboard(page(MEMBERSHIP)), ready=0)
        stub._quest_tried = [{"offer_id": "a", "title": "Browse the Earn page"}, {"offer_id": "b", "title": "Skip the line"}]
        stub._quest_done = ["a"]
        with caplog.at_level(logging.INFO, logger="fgc.msrewards"):
            stub._report(state, state, NO_EARN, 0, 0, 0, 0)
        assert status(stub, "Punch cards") == "failed: 1 of 2 steps done"
        assert "Punch cards: failed: 1 of 2 steps done, not counted: Skip the line" in caplog.text


class TestPageScripts:
    """Stubs return canned answers, so a script the browser cannot even parse would pass them (a stray "}}" did on 2026-10-26)."""

    SCRIPTS = {name: value for name, value in vars(rewards).items()
               if name.isupper() and isinstance(value, str) and "document" in value}

    @pytest.mark.parametrize("name", sorted(SCRIPTS))
    def test_brackets_are_balanced(self, name):
        import re
        code = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", self.SCRIPTS[name])
        for opening, closing in ("{}", "()", "[]"):
            assert code.count(opening) == code.count(closing), f"{name}: {opening}{closing} do not pair up"

    def test_every_finder_and_reader_is_checked(self):
        assert {"PANEL_COUNTER_JS", "PROTECTION_JS", "PROTECTION_TOGGLE", "BREAKDOWN_BUTTON", "READY_JS"} <= set(self.SCRIPTS)


class TestEarnPagePanels:
    def test_points_breakdown_is_opened_read_and_closed(self, stub, monkeypatch):
        monkeypatch.setattr(rewards.random, "random", lambda: 0.9)
        clicks = []

        async def click(finder, label):
            clicks.append(label)
            return True

        stub._click_like_a_hand = click
        stub.page = _Page({"children.length === 2": "12/15"})
        assert asyncio.run(stub._read_breakdown()) == {"earned": 12, "most": 15}
        assert clicks == ["Points breakdown"] and ("keyDown", "Escape") in stub.page.keys

    def test_streak_protection_is_switched_on_only_when_off_with_days_left(self, stub, monkeypatch):
        monkeypatch.setattr(rewards.random, "random", lambda: 0.9)
        clicks = []

        async def click(finder, label):
            clicks.append(label)
            return True

        stub._click_like_a_hand = click
        stub.page = _Page({"input[role=switch]": ["off", "on"]})
        asyncio.run(stub._protect_streak({"protection": {"on": False, "days": 14}}))
        assert clicks == ["Daily streak", "Streak protection"] and stub._protection_tried

    @pytest.mark.parametrize("guard", [None, {"on": True, "days": 14}, {"on": False, "days": 0}])
    def test_streak_protection_is_left_alone_otherwise(self, stub, guard):
        asyncio.run(stub._protect_streak({"protection": guard}))
        assert stub.clicks == [] and not stub._protection_tried

    def test_report_confirms_protection_from_the_dashboard(self, stub):
        first = dict(read_dashboard(page(MEMBERSHIP)), ready=0)
        stub._protection_tried = True
        stub._report(first, dict(first, protection={"on": True, "days": 14}), NO_EARN, 0, 0, 0, 0)
        assert status(stub, "Streak protection") == "switched on, 14 days left"
        stub.notify_games.clear()
        stub._report(first, dict(first, protection={"on": False, "days": 14}), NO_EARN, 0, 0, 0, 0)
        assert status(stub, "Streak protection") == "failed: not switched on"


class TestDailySetHint:
    def test_the_hint_that_opens_with_the_section_is_closed_before_the_cards(self, stub):
        clicks = []

        async def click(finder, label):
            clicks.append(label)
            return label == "Daily set section"

        async def opened(finder, label):
            clicks.append(f"open {label}")
            return True

        stub._click_like_a_hand, stub._open_card = click, opened
        asyncio.run(stub._do_daily_set([read_dashboard(DASHBOARD)["daily_set"][1]]))
        assert clicks == ["Daily set section", "Rewards dialog", "open Card 2"]
        assert stub.page.keys == []


class TestClaimAndReport:
    def test_a_claim_counts_only_when_the_balance_grew(self, stub):
        clicks = []

        async def click(finder, label):
            clicks.append(label)
            return True

        async def unchanged(load=True, shot=""):
            return dict(read_dashboard(DASHBOARD), ready=40)

        stub._click_like_a_hand, stub._read_dashboard = click, unchanged
        assert asyncio.run(stub._claim(dict(read_dashboard(DASHBOARD), ready=40))) is None
        assert clicks == ["Ready to claim", "Odbierz punkty"]

    def test_a_claim_that_moved_the_points_returns_them(self, stub):
        async def click(finder, label):
            return True

        async def grown(load=True, shot=""):
            return dict(read_dashboard(DASHBOARD), ready=0, balance=1290)

        stub._click_like_a_hand, stub._read_dashboard = click, grown
        state = dict(read_dashboard(DASHBOARD), ready=40)
        assert asyncio.run(stub._claim(state)) == 40 and state["balance"] == 1290

    def test_a_balance_that_could_not_be_read_confirms_no_claim(self, stub):
        async def click(finder, label):
            return True

        async def unreadable(load=True, shot=""):
            return dict(read_dashboard(page(MEMBERSHIP)), ready=0)

        stub._click_like_a_hand, stub._read_dashboard = click, unreadable
        assert asyncio.run(stub._claim(dict(read_dashboard(DASHBOARD), ready=40))) is None

    def test_nothing_ready_is_nothing_to_claim(self, stub):
        assert asyncio.run(stub._claim(dict(read_dashboard(DASHBOARD), ready=0))) == 0 and stub.clicks == []

    def _after(self, *items, searched=1, **changes):
        return dict(read_dashboard(page(BALANCE, MEMBERSHIP, bing_streak(searched), daily(*items))), ready=0, **changes)

    def test_a_good_day(self, stub):
        first = read_dashboard(DASHBOARD)
        after = self._after(item(1, True), item(2, True), item(3, form="dsetqu"), balance=1300)
        stub._daily_tried = ["Global_DailySet_20261025_Child2", "Global_DailySet_20261025_Child3"]
        stub._quizzes_today = {"Global_DailySet_20261025_Child3"}
        stub._report(first, after, (2, (0, 29), (10, 29)), 5, 7, 10, 50)
        # The Points breakdown was not read, so the line says what this run gained, not what the day did.
        assert [(g["title"], g["status"]) for g in stub.notify_games] == [
            ("This run", "+50 points, balance 1300"), ("Daily set", "2 of 3 done, the quiz counts later"),
            ("Keep earning", "2 cards opened"), ("Bing searches", "7 of 10 searches today")]

    def test_today_comes_from_microsoft_s_own_number_when_read(self, stub):
        first = read_dashboard(DASHBOARD)
        stub._daily_tried, stub._today_points = ["Global_DailySet_20261025_Child2"], 135
        stub._report(first, self._after(item(1, True), item(2, True), balance=1300), NO_EARN, 0, 0, 0, 0)
        assert stub.notify_games[0] == {"title": "Today", "url": rewards.URL_HOME, "status": "+135 points, balance 1300"}

    def test_a_card_that_did_not_count_is_a_failure_not_a_job_for_you(self, stub, caplog):
        stub._daily_tried = ["Global_DailySet_20261025_Child2", "Global_DailySet_20261025_Child3"]
        stub._quizzes_today = {"Global_DailySet_20261025_Child3"}
        with caplog.at_level(logging.INFO, logger="fgc.msrewards"):
            stub._report(read_dashboard(DASHBOARD), self._after(item(1, True), item(2), item(3, form="dsetqu")), NO_EARN, 0, 0, 0, 0)
        assert status(stub, "Daily set") == "failed: 1 of 3 done"
        assert "not counted: Card 2" in caplog.text and "Card 3" not in caplog.text

    def test_microsoft_s_counter_reports_the_search_points(self, stub):
        stub._counter_before, stub._counter_after = {"earned": 3, "most": 15}, {"earned": 15, "most": 15}
        stub._report(read_dashboard(page(MEMBERSHIP)), self._after(), NO_EARN, 4, 4, 5, 0)
        assert status(stub, "Bing searches") == "4 searches, 15 of 15 points"

    def test_the_day_s_target_comes_from_the_counter_when_only_the_first_read_worked(self, stub):
        # 10 searches earlier today, the counter then showed 21/30, so 3 more: never "13 of 10".
        stub._counter_before, stub._counter_after = {"earned": 21, "most": 30}, None
        stub._report(read_dashboard(page(MEMBERSHIP)), self._after(), NO_EARN, 3, 13, 10, 0)
        assert status(stub, "Bing searches") == "13 of 13 searches today"

    def test_a_counter_that_did_not_move_is_a_failure_even_with_the_streak_marked(self, stub):
        stub._counter_before, stub._counter_after = {"earned": 3, "most": 15}, {"earned": 3, "most": 15}
        stub._report(read_dashboard(page(MEMBERSHIP)), self._after(searched=1), NO_EARN, 4, 4, 5, 0)
        assert status(stub, "Bing searches") == "failed: 4 searches, not counted by Microsoft"

    def test_searches_microsoft_did_not_count_are_a_failure(self, stub):
        first = read_dashboard(page(MEMBERSHIP))
        stub._report(first, self._after(searched=0), NO_EARN, 5, 5, 10, 0)
        assert status(stub, "Bing searches") == "failed: 5 searches, not counted by Microsoft"

    def test_a_bing_check_reaches_the_summary_even_before_the_first_search(self, stub):
        stub._bing_check = True
        first = read_dashboard(page(MEMBERSHIP))
        stub._report(first, self._after(searched=0), NO_EARN, 0, 0, 10, 0)
        assert status(stub, "Bing searches") == "failed: Bing asked for a check after 0 searches"

    def test_a_failed_claim_is_reported_as_a_failure(self, stub):
        state = dict(read_dashboard(page(MEMBERSHIP)), ready=40)
        stub._report(state, state, NO_EARN, 0, 0, 0, None)
        assert status(stub, "Points") == "failed: 40 points waiting, not claimed"

    def test_a_quiz_waiting_all_day_is_not_named_again_by_later_runs(self, stub):
        only_quiz = page(MEMBERSHIP, bing_streak(1), daily(item(1, True), item(2, True), item(3, form="dsetqu")))
        state = dict(read_dashboard(only_quiz), ready=0)
        stub._report(state, state, NO_EARN, 0, 5, 5, 0)
        assert stub.notify_games == [{"title": "Microsoft Rewards", "url": rewards.URL_HOME, "status": "already done today"}]

    def test_a_second_run_with_the_daily_set_already_done_says_nothing_new(self, stub, caplog):
        done = page(MEMBERSHIP, bing_streak(1), daily(item(1, True), item(2, True), item(3, True)))
        state = dict(read_dashboard(done), ready=0)
        with caplog.at_level(logging.INFO, logger="fgc.msrewards"):
            stub._report(state, state, NO_EARN, 0, 5, 5, 0)
        assert stub.notify_games == [{"title": "Microsoft Rewards", "url": rewards.URL_HOME, "status": "already done today"}]


class TestHeadlineWithoutSpaces:
    def test_a_headline_without_spaces_gives_a_short_piece_of_it(self):
        headline = "東京で新しい電車が走り始めた今日のニュースをお伝えします"
        for _ in range(20):
            query = headline_query(headline)
            assert 1 <= len(query) <= 12 and query in headline


class TestTodayRow:
    """The day's row in a real database: what two runs on one day read and write."""

    def test_searches_add_up_in_the_row_of_the_day_the_run_began(self, stub, monkeypatch, tmp_path):
        async def run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fgc.db'}")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            monkeypatch.setattr(rewards, "async_session", async_sessionmaker(engine, expire_on_commit=False))
            # A run that began before midnight keeps writing that day's row.
            stub._day = "2026-10-27"
            await stub._today_row(lambda extra: {**extra, "quizzes": ["quiz"]})
            await stub._count_search(1, "First query")
            await stub._count_search(2, "second query")
            log = await stub._search_log()
            async with rewards.async_session() as session:
                rows = (await session.execute(select(ClaimedGame))).scalars().all()
            await engine.dispose()
            return log, rows

        log, rows = asyncio.run(run())
        assert [r.game_id for r in rows] == ["searches:2026-10-27"]
        assert json.loads(rows[0].extra) == {"quizzes": ["quiz"], "made": 2, "queries": ["first query", "second query"]}
        assert log["made"] == 2 and log["quizzes"] == {"quiz"} and log["used"] == {"first query", "second query"}


class TestSuggestionsLastPastAPageWithout:
    """Live 7.11: after the one page with Related searches the next page had none, and the bot went back to headlines."""

    def _read(self, stub, pages):
        async def nothing(*_a, **_kw):
            return None
        stub._look_around = nothing
        found = iter(pages)

        class Page:
            async def evaluate(self, _js):
                return json.dumps(next(found))
        stub.page = Page()
        for _ in pages:
            asyncio.run(stub._read_results())
        return stub._suggestions

    def test_earlier_ones_stay_when_a_page_has_none(self, stub):
        stub._suggestions = []
        assert self._read(stub, [["roosevelt national park", "badlands"], []]) == ["roosevelt national park", "badlands"]

    def test_one_already_searched_is_not_kept(self, stub):
        stub._suggestions, stub._used = [], {"badlands"}
        assert self._read(stub, [["roosevelt national park", "Badlands"], []]) == ["roosevelt national park"]

    def test_one_already_searched_on_this_very_page_is_not_kept_either(self, stub):
        # Review 7.11: ten used ones from the page took half the places and pushed fresh ones out.
        stub._suggestions, stub._used = ["fresh one"], {"wroclaw pogoda"}
        assert self._read(stub, [["Wrocław pogoda", "zakopane pogoda"]]) == ["zakopane pogoda", "fresh one"]

    def test_new_ones_come_first_and_the_list_stays_short(self, stub):
        stub._suggestions = []
        kept = self._read(stub, [[f"old {i}" for i in range(15)], [f"new {i}" for i in range(10)]])
        assert kept[:10] == [f"new {i}" for i in range(10)] and len(kept) == 20
