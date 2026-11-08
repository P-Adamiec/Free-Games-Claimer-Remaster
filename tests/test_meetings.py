"""A puzzle nobody solved waits for you to open noVNC, then the bot opens the checkout again (owner, 2026-11-02).

The puzzle expires about two minutes after it shows, and noVNC is closed while the bot works in the background,
so a prompt alone rarely reached anyone in time.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.core import run_state
from src.core.display import viewer_sockets

NOW = datetime(2026, 11, 5, 10, 40, tzinfo=timezone.utc)

# The owner's container on 2.11: Xvnc (5900) and noVNC (7080) listening, health checks of 7080 in TIME_WAIT.
QUIET = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1BA8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 41001 1 0
   1: 00000000:170C 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 41002 1 0
   2: 0100007F:96A0 0100007F:1BA8 06 00000000:00000000 03:00000F9E 00000000     0        0 0 3 0
"""
# noVNC open: websockify's own end (to 5900) and Xvnc's end (on 5900); only Xvnc's end is the viewer.
WATCHED = QUIET + """   3: 0100007F:D2F0 0100007F:170C 01 00000000:00000000 00:00000000 00000000  1000        0 52001 1 0
   4: 0100007F:170C 0100007F:D2F0 01 00000000:00000000 00:00000000 00000000  1000        0 52002 1 0
"""
WATCHED_V6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:170C 00000000000000000000000001000000:D2F2 01 00000000:00000000 00:00000000 00000000  1000        0 52010 1 0
"""


class TestWhoWatches:
    def test_listening_and_health_checks_are_nobody(self):
        assert viewer_sockets(QUIET, 5900) == set()

    def test_an_open_novnc_is_xvnc_s_end_of_the_connection(self):
        assert viewer_sockets(WATCHED, 5900) == {"52002"}

    def test_ipv6_counts_too(self):
        assert viewer_sockets(WATCHED_V6, "5900") == {"52010"}

    def test_outside_the_container_nobody_can_be_told(self, monkeypatch):
        from src.core import display
        monkeypatch.setattr(display, "Path", lambda _p: type("Missing", (), {"exists": lambda self: False})())
        assert display.vnc_viewers() is None


class TestHowLongAPuzzleWaitsForYou:
    @pytest.fixture
    def next_run_at(self, monkeypatch):
        def set_next(when):
            monkeypatch.setattr(run_state, "_next_run_source", (lambda: when) if when else None)
        return set_next

    def test_a_last_chance_waits_until_half_an_hour_before_the_end(self, next_run_at):
        next_run_at(NOW + timedelta(hours=12))
        assert run_state.booking_until(datetime(2026, 11, 5, 16, tzinfo=timezone.utc), True, NOW) == \
            datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc)

    def test_any_other_puzzle_waits_until_a_quarter_and_one_prompt_before_the_next_run(self, next_run_at, monkeypatch):
        # A meeting started at the deadline can still wait VNC_LOGIN_TIMEOUT for you, and must end before the next run.
        from src.core.config import cfg
        monkeypatch.setattr(cfg, "vnc_login_timeout", 900)
        next_run_at(NOW + timedelta(hours=12))
        assert run_state.booking_until(None, False, NOW) == NOW + timedelta(hours=11, minutes=30)

    def test_a_single_run_keeps_no_meeting(self, next_run_at):
        # Owner, 2.11: RUN_ONCE, Ofelia, cron and --once end at once; their next start tries again.
        next_run_at(None)
        assert run_state.booking_until(NOW + timedelta(days=6), True, NOW) is None

    def test_a_next_run_close_by_leaves_no_meeting(self, next_run_at):
        # SCHEDULER_FIXED_TIMES 18:00, a startup pass at 17:50: an hour's wait would skip the 18:00 pass for every store.
        next_run_at(NOW + timedelta(minutes=18))
        assert run_state.booking_until(NOW + timedelta(hours=5), True, NOW) is None

    def test_no_meeting_when_too_little_time_is_left(self, next_run_at):
        next_run_at(NOW + timedelta(hours=12))
        assert run_state.booking_until(NOW + timedelta(minutes=35), True, NOW) is None


class TestBookings:
    ITEM = {"title": "Sorry We're Closed", "data": "https://store.epicgames.com/p/sorry", "ends": None,
            "last_chance": True, "until": NOW, "seen": set()}

    def test_a_game_is_kept_once_and_dropped_after_its_meeting(self):
        assert run_state.book("Epic", dict(self.ITEM)) is True
        assert run_state.book("epic", dict(self.ITEM)) is False
        assert run_state.is_booked("epic", "Sorry We're Closed")
        run_state.unbook("epic", run_state.booked()["epic"])
        assert run_state.booked() == {}

    def test_every_run_starts_without_meetings(self):
        run_state.book("epic", dict(self.ITEM))
        run_state.reset_run_state()
        assert run_state.booked() == {}

    def test_a_game_that_ends_before_the_next_run_is_not_said_to_wait_for_it(self):
        text = run_state.describe_wait(run_state.CAPTCHA_ENDS, 1, "game")
        assert "could not be claimed" in text and "next run" not in text


def _epic_stub(monkeypatch, ask_setting, ends=None, meeting=False, with_you=False, clears=False):
    from src.stores import epic as epic_module
    from src.stores.epic import EpicGamesClaimer
    monkeypatch.setattr(epic_module.cfg, "eg_captcha_ask", ask_setting)
    monkeypatch.setattr(epic_module, "vnc_viewers", lambda: {"41"})
    asked = []

    async def reason():
        return "checkout frame, frame: https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html"

    async def wait_out(*args, **kwargs):
        asked.append(kwargs["ask"])
        stub._handed_over = False
        return clears

    stub = type("Stub", (), {"store_name": "epic", "_book": EpicGamesClaimer._book,
                             "_checkout_challenge_reason": staticmethod(reason),
                             "_checkout_challenge_present": staticmethod(reason),
                             "_wait_out_challenge": staticmethod(wait_out)})()
    stub._checkout_asked, stub._checkout_puzzles, stub._checkout_stuck, stub._checkout_handed = set(), set(), set(), {}
    stub._ends_now, stub._claiming_url, stub._seen_at_prompt = ends, "https://store.epicgames.com/p/galaxy-kart", None
    stub._meeting, stub._with_you, stub._from_list, stub._platform_labels = meeting, with_you, True, {}
    asyncio.run(EpicGamesClaimer._clear_checkout_challenge(stub, "Galaxy Kart"))
    return asked, run_state.booked()


class TestAPuzzleNobodySolvedIsKept:
    def test_with_eg_captcha_ask_you_are_asked_first_then_it_is_kept(self, monkeypatch):
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        # Owner, 2.11: the full VNC_LOGIN_TIMEOUT first, the meeting only after that.
        asked, kept = _epic_stub(monkeypatch, True)
        assert asked == [True]
        item = kept["epic"][0]
        assert item["title"] == "Galaxy Kart" and item["data"].endswith("galaxy-kart") and item["seen"] == {"41"}
        # The meeting opens a game from the free games list by its tile again, never by a cold address load.
        assert item["from_list"] is True

    def test_by_default_a_game_with_a_next_run_is_not_kept(self, monkeypatch):
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        asked, kept = _epic_stub(monkeypatch, False, ends=datetime.now(timezone.utc) + timedelta(days=3))
        assert asked == [False] and kept == {}

    def test_by_default_a_game_on_its_last_run_is_kept(self, monkeypatch):
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        asked, kept = _epic_stub(monkeypatch, False, ends=datetime.now(timezone.utc) + timedelta(hours=3))
        assert asked == [True] and kept["epic"][0]["last_chance"] is True

    def test_a_solved_puzzle_is_not_kept(self, monkeypatch):
        asked, kept = _epic_stub(monkeypatch, True, clears=True)
        assert kept == {}

    def test_a_single_run_asks_and_keeps_nothing(self, monkeypatch):
        # No next run known: the game is on its last chance, you are asked, and the run then ends without waiting.
        asked, kept = _epic_stub(monkeypatch, False, ends=datetime.now(timezone.utc) + timedelta(days=3))
        assert asked == [True] and kept == {}

    @pytest.mark.parametrize("with_you,ask", [(True, True), (False, False)])
    def test_at_a_meeting_you_are_asked_at_once_and_nothing_is_kept_again(self, monkeypatch, with_you, ask):
        asked, kept = _epic_stub(monkeypatch, False, meeting=True, with_you=with_you)
        assert asked == [ask] and kept == {}


def test_fab_keeps_the_whole_listing_for_its_meeting(monkeypatch):
    monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
    from src.stores import epic_fab
    from src.stores.epic_fab import FabClaimer
    monkeypatch.setattr(epic_fab.cfg, "eg_captcha_ask", True)
    monkeypatch.setattr(epic_fab, "vnc_viewers", lambda: set())

    async def reason():
        return "checkout frame, frame: https://newassets.hcaptcha.com/x"

    async def wait_out(*args, **kwargs):
        stub._handed_over = False
        return False

    listing = {"title": "Dragon Cave", "uid": "abc", "url": "https://www.fab.com/listings/abc", "ends": ""}
    stub = type("Stub", (), {"store_name": "fab", "_book": FabClaimer._book, "_meeting": False, "_with_you": False,
                             "_checkout_puzzle": False, "_ends_now": None, "_seen_at_prompt": None, "_claiming": listing})()
    stub._checkout_challenge_reason, stub._wait_out_challenge = reason, wait_out
    assert asyncio.run(FabClaimer._clear_checkout_challenge(stub, "Dragon Cave")) is False
    assert stub._checkout_puzzle is True and run_state.booked()["fab"][0]["data"] is listing


class _Meetings:
    def __init__(self, monkeypatch, viewers):
        import main
        self.main, self.calls, self.sent = main, [], []
        self.viewers = list(viewers)

        async def meet(items, with_you):
            self.calls.append(([item["title"] for item in items], with_you))
            return {"store": "Epic Games", "games": [{"title": items[0]["title"], "url": "", "status": "claimed"}]}

        async def notify(message, **_kw):
            self.sent.append(message)

        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr(main, "MEETINGS", {"epic": ("Epic Games", meet)})
        monkeypatch.setattr(main, "notify", notify)
        monkeypatch.setattr(main, "vnc_viewers", lambda: self.viewers.pop(0) if len(self.viewers) > 1 else self.viewers[0])
        monkeypatch.setattr(main.asyncio, "sleep", no_sleep)

    @staticmethod
    def keep(title, until, last_chance=True, seen=frozenset({"41"})):
        run_state.book("epic", {"title": title, "data": "https://store.epicgames.com/p/x", "ends": None,
                                "last_chance": last_chance, "until": until, "seen": set(seen)})


class TestTheMeeting:
    LATER = datetime.now(timezone.utc) + timedelta(hours=3)
    GONE = datetime.now(timezone.utc) - timedelta(minutes=1)

    def test_a_new_connection_opens_the_checkout_with_you(self, monkeypatch):
        m = _Meetings(monkeypatch, [{"41", "77"}])
        m.keep("Galaxy Kart", self.LATER)
        asyncio.run(m.main.keep_appointments(final=False))
        assert m.calls == [(["Galaxy Kart"], True)] and run_state.booked() == {}
        assert "with you in noVNC" in m.sent[0]

    def test_a_tab_left_open_does_not_count_as_you_coming(self, monkeypatch):
        m = _Meetings(monkeypatch, [{"41"}])
        m.keep("Galaxy Kart", self.LATER)
        asyncio.run(m.main.keep_appointments(final=False))
        assert m.calls == [] and run_state.is_booked("epic", "Galaxy Kart")

    def test_without_you_a_last_chance_gets_one_try_of_the_bot_s_own(self, monkeypatch):
        m = _Meetings(monkeypatch, [set()])
        m.keep("Sorry We're Closed", self.GONE)
        m.keep("Galaxy Kart", self.GONE, last_chance=False)
        asyncio.run(m.main.keep_appointments(final=False))
        # The other one is tried by the next run anyway.
        assert m.calls == [(["Sorry We're Closed"], False)] and run_state.booked() == {}

    def test_the_last_wait_ends_when_you_come(self, monkeypatch):
        m = _Meetings(monkeypatch, [set(), set(), {"77"}])
        m.keep("Galaxy Kart", self.LATER)
        asyncio.run(m.main.keep_appointments(final=True))
        assert m.calls == [(["Galaxy Kart"], True)]

    def test_nothing_kept_means_no_wait(self, monkeypatch):
        m = _Meetings(monkeypatch, [set()])
        asyncio.run(m.main.keep_appointments(final=True))
        assert m.calls == [] and m.sent == []

    def test_outside_the_container_only_the_deadline_counts(self, monkeypatch):
        m = _Meetings(monkeypatch, [None])
        m.keep("Sorry We're Closed", self.GONE)
        asyncio.run(m.main.keep_appointments(final=False))
        assert m.calls == [(["Sorry We're Closed"], False)]


class TestYouAreToldOnce:
    def test_one_notification_per_store_for_what_was_kept_since_the_last(self, monkeypatch):
        m = _Meetings(monkeypatch, [set()])
        monkeypatch.setattr(m.main.cfg, "notify_login_request", True)
        m.keep("Galaxy Kart", datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc))
        asyncio.run(m.main.tell_bookings())
        asyncio.run(m.main.tell_bookings())
        assert len(m.sent) == 1
        assert "Galaxy Kart: until 2026-11-05 15:30 UTC" in m.sent[0] and "two minutes" in m.sent[0]

    def test_the_summary_lists_what_waits_for_you(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        assert "Waiting for you in noVNC" in source and "await keep_appointments(final=True)" in source
        run = source.split("async def run_claimers(", 1)[1]
        # Between stores, never inside one.
        assert run.index("await keep_appointments(final=False)") < run.index("await tell_bookings()")


class TestAnEpicMeeting:
    """The meeting itself: signed in first, every game the way it opened today, and no more checkouts once nobody answers."""

    def _run(self, monkeypatch, items, with_you, unanswered_after=None):
        from src.stores.epic import EpicGamesClaimer
        run_state.mark_unanswered("epic")
        claimer, seen = EpicGamesClaimer(), {"sign_in_held_back": None, "claims": []}

        async def nothing(*_a, **_kw):
            return None

        async def signed_in():
            seen["sign_in_held_back"] = run_state.waits_for_nobody("epic")
            return True

        async def claim(url, from_list=False):
            seen["claims"].append((url, from_list, dict(claimer._platform_labels)))
            if unanswered_after == url:
                run_state.mark_unanswered("epic")

        claimer.page = type("Page", (), {"get": staticmethod(nothing)})()
        for name, stub in (("start_browser", nothing), ("_set_cookies", nothing), ("sleep", nothing),
                           ("_ensure_logged_in", signed_in), ("_claim_game", claim), ("close_browser", nothing)):
            monkeypatch.setattr(claimer, name, stub)
        asyncio.run(claimer.run(booked_items=items, with_you=with_you))
        return claimer, seen

    @staticmethod
    def _item(slug, last_chance=False, from_list=True, platform=None):
        return {"title": slug, "data": f"https://store.epicgames.com/p/{slug}", "last_chance": last_chance,
                "ends": (datetime.now(timezone.utc) + timedelta(hours=2)) if last_chance else None,
                "from_list": from_list, "platform": platform}

    def test_with_you_the_sign_in_is_not_held_back_and_games_open_as_before(self, monkeypatch):
        claimer, seen = self._run(monkeypatch, [self._item("sorry"), self._item("galaxy-kart-android", from_list=False,
                                                                                 platform="Android")], True)
        assert seen["sign_in_held_back"] is False and claimer._meeting is True
        assert seen["claims"][0][:2] == ("https://store.epicgames.com/p/sorry", True)
        assert seen["claims"][1][2] == {"https://store.epicgames.com/p/galaxy-kart-android": "Android"}

    def test_once_nobody_answers_at_the_meeting_only_a_last_chance_is_opened(self, monkeypatch):
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        first = self._item("a")
        _, seen = self._run(monkeypatch, [first, self._item("b"), self._item("c", last_chance=True)], True,
                            unanswered_after=first["data"])
        assert [url.rsplit("/", 1)[-1] for url, _, _ in seen["claims"]] == ["a", "c"]
        assert run_state.waiting_for_you() == {("epic", run_state.CAPTCHA_UNSOLVED, "game"): 1}


class TestJoiningTheMeetingUnopened:
    """With EG_CAPTCHA_ASK, after a prompt nobody answered, the next game is yours anyway: kept, not opened."""

    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from src.core.database import Base
        from src.stores import epic as epic_module

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fgc.db'}")

        async def create():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        asyncio.run(create())
        monkeypatch.setattr(epic_module, "async_session", async_sessionmaker(engine, expire_on_commit=False))
        monkeypatch.setattr(epic_module.cfg, "eg_captcha_ask", True)
        monkeypatch.setattr(epic_module.cfg, "eg_email", "player@example.com")
        monkeypatch.setattr(epic_module, "vnc_viewers", lambda: set())
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        yield epic_module
        asyncio.run(engine.dispose())

    def _claim(self, epic_module, status=None):
        from src.core.database import ClaimedGame
        if status:
            async def add():
                async with epic_module.async_session() as session:
                    session.add(ClaimedGame(store="epic", user="player@example.com", game_id="sorry", title="Sorry We're Closed",
                                            url="https://store.epicgames.com/p/sorry", status=status, extra="{}"))
                    await session.commit()
            asyncio.run(add())
        claimer, loads = epic_module.EpicGamesClaimer(), []

        class Page:
            async def get(self, url):
                loads.append(url)
        claimer.page = Page()
        run_state.mark_unanswered("epic")
        asyncio.run(claimer._claim_game("https://store.epicgames.com/p/sorry", from_list=True))
        return claimer, loads

    def test_a_game_you_lack_joins_without_its_page_being_opened(self, db):
        _, loads = self._claim(db)
        kept = run_state.booked()["epic"][0]
        assert loads == [] and kept["title"] == "Sorry" and kept["from_list"] is True

    def test_a_game_you_own_is_not_kept(self, db):
        claimer, loads = self._claim(db, status="existed")
        assert loads == [] and run_state.booked() == {} and claimer.notify_games[0]["status"] == "existed"

    def test_by_default_only_a_game_you_lack_is_said_to_wait(self, db, monkeypatch):
        # Review 4.11: "Needed you" counted games you already had as waiting for the next run.
        monkeypatch.setattr(db.cfg, "eg_captcha_ask", False)
        _, loads = self._claim(db)
        assert loads == [] and run_state.booked() == {}
        assert run_state.waiting_for_you() == {("epic", run_state.CAPTCHA_UNSOLVED, "game"): 1}

    @pytest.mark.parametrize("status", ["failed:missing_base", "skipped:not-free"])
    def test_a_game_you_cannot_get_anyway_is_not_counted(self, db, monkeypatch, status):
        # Review 7.11: a DLC without its base game counted as "1 game waits for the next run".
        monkeypatch.setattr(db.cfg, "eg_captcha_ask", False)
        _, loads = self._claim(db, status=status)
        assert loads == [] and run_state.waiting_for_you() == {}

    def test_by_default_a_game_you_own_is_not_counted(self, db, monkeypatch):
        monkeypatch.setattr(db.cfg, "eg_captcha_ask", False)
        claimer, loads = self._claim(db, status="existed")
        assert loads == [] and run_state.waiting_for_you() == {} and claimer.notify_games[0]["status"] == "existed"

    @staticmethod
    def _claim_ending(epic_module, monkeypatch):
        """The same game after a prompt nobody answered, with its end known; returns what was opened."""
        monkeypatch.setattr(epic_module.cfg, "eg_captcha_ask", False)
        claimer, opened = epic_module.EpicGamesClaimer(), []
        claimer._game_ends = {"sorry": datetime.now(timezone.utc) + timedelta(hours=1)}

        async def open_page(url, ask=True, from_list=False):
            opened.append(url)
            return False
        monkeypatch.setattr(claimer, "_open_game_page", open_page)
        run_state.mark_unanswered("epic")
        asyncio.run(claimer._claim_game("https://store.epicgames.com/p/sorry", from_list=True))
        return opened

    def test_by_default_a_game_on_its_last_run_is_still_opened(self, db, monkeypatch):
        # With nobody there a puzzle ends it at once, without one it is claimed, so trying costs nothing.
        assert self._claim_ending(db, monkeypatch) == ["https://store.epicgames.com/p/sorry"]

    def test_a_single_run_waits_even_with_the_end_known(self, db, monkeypatch):
        # Without a next run nobody can say this is its last chance, and the next start tries again.
        monkeypatch.setattr(run_state, "_next_run_source", lambda: None)
        assert self._claim_ending(db, monkeypatch) == []
        assert run_state.waiting_for_you() == {("epic", run_state.CAPTCHA_UNSOLVED, "game"): 1}


class TestTheSummaryLines:
    def test_titles_are_made_safe_and_silenced_stores_left_out(self, monkeypatch):
        import main
        monkeypatch.setattr(main.cfg, "store_notify_enabled", lambda store: store != "fab")
        until = datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc)
        for store, title in (("epic", "<Remastered> Pack"), ("fab", "Dragon Cave")):
            run_state.book(store, {"title": title, "data": "x", "ends": None, "last_chance": True, "until": until, "seen": set()})
        lines = main.kept_lines()
        assert len(lines) == 1 and "<Remastered>" not in lines[0] and "until 2026-11-05 15:30 UTC" in lines[0]


def test_the_log_says_it_waits_again_after_each_meeting(monkeypatch, caplog):
    # live.py reads this line to tell an idle wait (Chrome closed) from a meeting or a pass.
    import logging

    import main
    calls = []

    async def meet(items, with_you):
        calls.append(items[0]["title"])
        return {"games": []}

    async def no_sleep(_seconds):
        return None

    viewers = [{"41", "77"}, {"41", "77", "99"}]
    monkeypatch.setattr(main, "MEETINGS", {"epic": ("Epic Games", meet), "fab": ("Fab", meet)})
    monkeypatch.setattr(main, "vnc_viewers", lambda: viewers.pop(0) if len(viewers) > 1 else viewers[0])
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    until = datetime.now(timezone.utc) + timedelta(hours=3)
    run_state.book("epic", {"title": "Galaxy Kart", "data": "x", "ends": None, "last_chance": False, "until": until, "seen": {"41"}})
    run_state.book("fab", {"title": "Dragon Cave", "data": {}, "ends": None, "last_chance": False, "until": until,
                           "seen": {"41", "77"}})
    with caplog.at_level(logging.INFO):
        asyncio.run(main.keep_appointments(final=True))
    assert calls == ["Galaxy Kart", "Dragon Cave"]
    assert caplog.text.count("Waiting for you in noVNC until") == 2


class TestASingleRunClaimsNoLoss:
    """Review, 2.11: without a next run every known end counted as lost, though tomorrow's start could claim it."""

    def test_a_single_run_never_says_a_giveaway_is_lost(self, monkeypatch):
        monkeypatch.setattr(run_state, "_next_run_source", None)
        assert run_state.lost_before_next_run(datetime.now(timezone.utc) + timedelta(days=6)) is False

    def test_with_a_schedule_an_end_before_the_next_run_is_lost(self, monkeypatch):
        monkeypatch.setattr(run_state, "_next_run_source", lambda: datetime.now(timezone.utc) + timedelta(hours=12))
        monkeypatch.setattr(run_state, "run_started", lambda: None)
        assert run_state.lost_before_next_run(datetime.now(timezone.utc) + timedelta(hours=3)) is True


def test_the_meeting_result_follows_the_summary_switches(monkeypatch):
    import main
    sent = []

    async def meet(items, with_you):
        return {"games": [{"title": "A", "url": "", "status": "claimed"}, {"title": "B", "url": "", "status": "failed"}]}

    async def notify(message, **_kw):
        sent.append(message)

    monkeypatch.setattr(main, "MEETINGS", {"epic": ("Epic Games", meet)})
    monkeypatch.setattr(main, "notify", notify)
    monkeypatch.setattr(main.cfg, "notify_claim_fails", False)
    asyncio.run(main._meet("epic", [{"title": "A"}], False))
    assert len(sent) == 1 and "A" in sent[0] and "B" not in sent[0]


def test_a_fab_meeting_decides_by_the_kept_flag(monkeypatch):
    # Hours of waiting made every end look like a last chance; the flag kept at the booking does not drift.
    from src.stores.epic_fab import FabClaimer
    claimer, claimed = FabClaimer(), []

    async def nothing(*_a, **_kw):
        return None

    async def yes(*_a, **_kw):
        return True

    async def claim(item):
        claimed.append(item["title"])
        run_state.mark_unanswered("fab")

    claimer.page = type("Page", (), {"get": staticmethod(nothing)})()
    for name, stub in (("start_browser", nothing), ("sleep", nothing), ("_ensure_logged_in", yes),
                       ("_claim_listing", claim), ("close_browser", nothing)):
        monkeypatch.setattr(claimer, name, stub)
    kept = [{"title": t, "data": {"title": t, "uid": t, "url": f"https://www.fab.com/listings/{t}"}, "last_chance": last}
            for t, last in (("first", False), ("second", False), ("last", True))]
    asyncio.run(claimer.run(booked_items=kept, with_you=True))
    assert claimed == ["first", "last"]
    assert claimer.notify_games == [{"title": "second", "url": "https://www.fab.com/listings/second", "status": "failed:captcha"}]


class TestAnOpenNoVNCIsTold:
    """A VPS with port 7080 open and no VNC_PASSWORD gives strangers a browser signed in to your stores."""

    def _sent(self, monkeypatch, url, password=False):
        import main
        sent = []

        async def notify(message, **_kw):
            sent.append(message)

        monkeypatch.setattr(main, "notify", notify)
        monkeypatch.setattr(main.cfg, "vnc_password_set", password)
        monkeypatch.setattr(main.cfg, "vnc_url_base", url)
        asyncio.run(main.warn_open_novnc())
        return sent

    def test_a_public_address_without_a_password_is_told(self, monkeypatch):
        sent = self._sent(monkeypatch, "https://fgc.example.com")
        assert len(sent) == 1 and "VNC_PASSWORD" in sent[0]

    @pytest.mark.parametrize("url", ["http://localhost:7080", "http://192.168.1.10:7080", "http://nas.local:7080"])
    def test_a_home_address_is_not(self, monkeypatch, url):
        assert self._sent(monkeypatch, url) == []

    def test_a_proxy_that_asks_for_a_login_is_not_an_open_door(self, monkeypatch):
        assert self._sent(monkeypatch, "https://me:secret@fgc.example.com") == []

    def test_a_password_ends_it(self, monkeypatch):
        assert self._sent(monkeypatch, "https://fgc.example.com", password=True) == []


class TestAnAssetYouHaveIsLeftAlone:
    """Log 5.11: Fab opened three pages of assets you own at every run, as Unity did its one; Epic, Steam and itch.io did not."""

    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from src.core.database import Base
        from src.stores import epic_fab, unity

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fgc.db'}")

        async def create():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        asyncio.run(create())
        session = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(epic_fab, "async_session", session)
        monkeypatch.setattr(unity, "async_session", session)
        monkeypatch.setattr(epic_fab.cfg, "eg_email", "player@example.com")
        yield session
        asyncio.run(engine.dispose())

    @staticmethod
    def _row(session, store, user, game_id, status="existed", days_ago=1):
        from src.core.database import ClaimedGame

        extra = {} if days_ago is None else {"page_checked": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()}

        async def add():
            async with session() as s:
                s.add(ClaimedGame(store=store, user=user, game_id=game_id, title=game_id, url="", status=status, extra=json.dumps(extra)))
                await s.commit()
        asyncio.run(add())

    def _fab(self, monkeypatch, listings):
        from src.stores.epic_fab import FabClaimer
        claimer, seen = FabClaimer(), {"signed_in": False, "opened": []}

        async def nothing(*_a, **_kw):
            return None

        async def detect():
            return listings

        async def sign_in():
            seen["signed_in"] = True
            return True

        async def claim(item):
            seen["opened"].append(item["uid"])

        claimer.page = type("Page", (), {"get": staticmethod(nothing)})()
        for name, stub in (("start_browser", nothing), ("sleep", nothing), ("_detect_free_listings", detect),
                           ("_ensure_logged_in", sign_in), ("_claim_listing", claim), ("close_browser", nothing)):
            monkeypatch.setattr(claimer, name, stub)
        asyncio.run(claimer.run())
        return claimer, seen

    LISTING = {"title": "Underground Tunnel", "uid": "tunnel", "url": "https://www.fab.com/listings/tunnel"}

    def test_fab_opens_nothing_and_signs_in_to_nothing_when_all_are_read_lately(self, db, monkeypatch):
        self._row(db, "fab", "player@example.com", "tunnel")
        claimer, seen = self._fab(monkeypatch, [self.LISTING])
        assert seen == {"signed_in": False, "opened": []}
        assert claimer.notify_games == [{"title": "Underground Tunnel", "url": self.LISTING["url"], "status": "existed"}]

    @pytest.mark.parametrize("days_ago,status", [(4, "existed"), (None, "existed"), (1, "failed:unconfirmed")])
    def test_fab_opens_an_asset_read_long_ago_or_never_confirmed(self, db, monkeypatch, days_ago, status):
        self._row(db, "fab", "player@example.com", "tunnel", status=status, days_ago=days_ago)
        _, seen = self._fab(monkeypatch, [self.LISTING])
        assert seen == {"signed_in": True, "opened": ["tunnel"]}

    def test_fab_opens_a_new_asset_beside_one_it_knows(self, db, monkeypatch):
        self._row(db, "fab", "player@example.com", "tunnel")
        new = {"title": "SciFi Beast", "uid": "beast", "url": "https://www.fab.com/listings/beast"}
        claimer, seen = self._fab(monkeypatch, [self.LISTING, new])
        assert seen == {"signed_in": True, "opened": ["beast"]} and claimer.notify_games[0]["status"] == "existed"

    def test_unity_does_not_open_the_page_of_an_asset_it_read_lately(self, db):
        from src.stores.unity import UnityClaimer
        self._row(db, "unity", "player", "vfavorites-2-263643")
        claimer, loads = UnityClaimer(), []
        claimer.user = "player"

        class Page:
            async def get(self, url):
                loads.append(url)
        claimer.page = Page()
        asyncio.run(claimer._claim_asset({"title": "vFavorites 2", "slug": "vfavorites-2-263643", "url": "https://assetstore.unity.com/x"}))
        assert loads == [] and claimer.notify_games[0]["status"] == "existed"

    def test_unity_notes_the_read_when_only_its_record_says_you_have_it(self, db):
        # No package number on the page: the record is all there is, and it should not bring the page back at every run.
        from sqlalchemy import select

        from src.core.database import ClaimedGame
        from src.stores.unity import UnityClaimer
        self._row(db, "unity", "player", "vfavorites-2-263643", days_ago=None)
        claimer = UnityClaimer()
        claimer.user = "player"

        class Page:
            async def get(self, _url):
                return None

        async def nothing(*_a, **_kw):
            return None
        claimer.page, claimer.sleep = Page(), nothing
        asyncio.run(claimer._claim_asset({"title": "vFavorites 2", "slug": "vfavorites-2-263643", "url": "https://assetstore.unity.com/x"}))

        async def read():
            async with db() as s:
                return (await s.execute(select(ClaimedGame))).scalars().first()
        row = asyncio.run(read())
        assert claimer.notify_games[-1]["status"] == "existed", claimer.notify_games
        assert "page_checked" in json.loads(row.extra)

    def test_fab_signed_in_by_hand_finds_its_record_at_the_asset(self, db, monkeypatch):
        # Review 7.11: without EG_EMAIL the rows sit under the account's name, known only after sign-in.
        from src.stores import epic_fab
        monkeypatch.setattr(epic_fab.cfg, "eg_email", "")
        self._row(db, "fab", "Nick", "tunnel")
        claimer, loads = epic_fab.FabClaimer(), []
        claimer.user = "Nick"

        class Page:
            async def get(self, url):
                loads.append(url)
        claimer.page = Page()
        asyncio.run(claimer._claim_listing(self.LISTING))
        assert loads == [] and claimer.notify_games == [{"title": "Underground Tunnel", "url": self.LISTING["url"], "status": "existed"}]

    @pytest.mark.parametrize("store", ["fab", "unity"])
    def test_a_claim_stays_a_claim_when_a_later_run_finds_it_owned(self, db, store):
        from sqlalchemy import select

        from src.core.database import ClaimedGame
        from src.stores.epic_fab import FabClaimer
        from src.stores.unity import UnityClaimer
        claimer = FabClaimer() if store == "fab" else UnityClaimer()
        claimer.user = "player"
        asyncio.run(claimer._remember("tunnel", "Underground Tunnel", "", "claimed"))
        asyncio.run(claimer._remember("tunnel", "Underground Tunnel", "", "existed"))

        async def read():
            async with db() as s:
                return (await s.execute(select(ClaimedGame))).scalars().first()
        assert asyncio.run(read()).status == "claimed"

    def test_remembering_an_asset_notes_when_its_page_was_read(self, db):
        from sqlalchemy import select

        from src.core.database import ClaimedGame
        from src.stores.epic_fab import FabClaimer
        asyncio.run(FabClaimer()._remember("tunnel", "Underground Tunnel", "", "claimed"))

        async def read():
            async with db() as s:
                return (await s.execute(select(ClaimedGame))).scalars().first()
        assert "page_checked" in json.loads(asyncio.run(read()).extra)


class TestBingsOwnSuggestions:
    """Log 5.11: 0 of 10 queries came from Related searches, so all were odd word salads from headlines."""

    def test_they_are_read_without_the_section_being_drawn(self):
        # Bing draws the section only on screen: innerText was empty there, textContent is not.
        from src.stores.ms_rewards import SUGGESTIONS_JS
        assert "textContent" in SUGGESTIONS_JS and "innerText" not in SUGGESTIONS_JS

    def test_they_are_preferred_when_there_are_any(self):
        from src.stores.ms_rewards import next_query

        class Roll:
            @staticmethod
            def random():
                return 0.1

            @staticmethod
            def choice(items):
                return items[0]
        assert next_query(set(), ["pogoda warszawa na jutro"], ["rosja osób"], rng=Roll) == "pogoda warszawa na jutro"
