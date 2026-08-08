"""Ubisoft giveaway detection.

Real entries served on 2026-08-07, including the trial that shares the giveaway's
"gametrial" type and the one whose expiry is set to the year 2222.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from src.stores.ubisoft import (
    UbisoftClaimer,
    _extract_state,
    _news_entries,
    _slug,
    parse_free_games,
)

NOW = datetime(2026, 8, 7, 12, 0, 0)

GIVEAWAY = {
    "newsId": "ignt.62354",
    "type": "gametrial",
    "placement": "freeevents",
    "publicationDate": "2026-08-06T15:00:00",
    "expirationDate": "2026-08-13T08:00:00",
    "title": "Ghost Recon Future Soldier Giveaway",
    "links": [{"type": "External", "param": "https://register.ubisoft.com/ghost-recon-future-soldier"}],
}
FREE_WEEKEND = {
    "newsId": "ignt.62164",
    "type": "freeweek",
    "placement": "freeevents",
    "publicationDate": "2026-08-06T15:00:00",
    "expirationDate": "2026-08-10T12:00:00",
    "title": "Ghost Recon Wildlands Free Weekend",
    "links": [{"type": "External", "param": "https://freeweekend.ubisoft.com/ghost-recon-wildlands"}],
}
DEMO = {
    "newsId": "ignt.51478",
    "type": "demo",
    "placement": "freeevents",
    "publicationDate": "2025-04-18T06:00:00",
    "expirationDate": None,
    "title": "Star Wars Outlaws Demo",
    "links": [{"type": "External", "param": "https://register.ubisoft.com/starwarsoutlaws-demo"}],
}
TRIAL = {
    "newsId": "ignt.45793",
    "type": "gametrial",
    "placement": "freeevents",
    "publicationDate": "2024-07-10T13:00:00",
    "expirationDate": None,
    "title": "Skull and Bones Trial",
    "links": [{"type": "External", "param": "https://register.ubisoft.com/skullandbones-trial"}],
}
TRIAL_FAR_FUTURE_EXPIRY = {
    "newsId": "ignt.30111",
    "type": "gametrial",
    "placement": "freeevents",
    "publicationDate": "2022-01-27T18:01:00",
    "expirationDate": "2222-01-27T00:00:00",
    "title": "Tom Clancy's Rainbow Six Extraction Trial",
    "links": [{"type": "External", "param": "https://register.ubisoft.com/r6e-trial"}],
}
FREE_TO_PLAY = {
    "newsId": "ignt.39096",
    "type": "free2play",
    "placement": "freeevents",
    "publicationDate": "2023-08-07T11:00:00",
    "expirationDate": None,
    "title": "Growtopia",
    "links": [{"type": "External", "param": "https://register.ubisoft.com/Growtopia-free"}],
}

REAL_FEED = [GIVEAWAY, FREE_WEEKEND, DEMO, TRIAL, TRIAL_FAR_FUTURE_EXPIRY, FREE_TO_PLAY]


def _page(entries, blocks=1):
    """Wrap feed entries the way the real page embeds them."""
    state = {
        "NewsPersonalization": {
            f"newsPersonalization-{i}": {"status": "SUCCESS", "data": {"news": entries}}
            for i in range(blocks)
        }
    }
    return "<html><body><script>window.__PRELOADED_STATE__ = " + json.dumps(state) + ";</script></body></html>"


class TestRealFeed:
    def test_finds_only_the_giveaway(self):
        games = parse_free_games(_page(REAL_FEED), now=NOW)
        assert games == [{
            "title": "Ghost Recon Future Soldier Giveaway",
            "url": "https://register.ubisoft.com/ghost-recon-future-soldier",
            "slug": "ghost-recon-future-soldier",
            "ends": "2026-08-13T08:00:00",
        }]

    @pytest.mark.parametrize("entry,why", [
        (FREE_WEEKEND, "free weekend"),
        (DEMO, "demo"),
        (TRIAL, "trial without an end date"),
        (TRIAL_FAR_FUTURE_EXPIRY, "trial with a year 2222 end date"),
        (FREE_TO_PLAY, "free to play game"),
    ])
    def test_everything_else_is_rejected(self, entry, why):
        assert parse_free_games(_page([entry]), now=NOW) == [], why

    @pytest.mark.parametrize("type_name", ["freeweek", "freeweekend", "somethingnew"])
    def test_unknown_types_are_rejected(self, type_name):
        # One free weekend was served as both "freeweek" and "freeweekend", so allowlist only.
        assert parse_free_games(_page([dict(FREE_WEEKEND, type=type_name)]), now=NOW) == []

    def test_the_same_feed_in_several_blocks_yields_one_entry(self):
        assert len(parse_free_games(_page(REAL_FEED, blocks=3), now=NOW)) == 1


class TestPromoWindow:
    def test_expired_giveaway_is_ignored(self):
        assert parse_free_games(_page([GIVEAWAY]), now=datetime(2026, 8, 14, 0, 0)) == []

    def test_giveaway_is_taken_up_to_its_last_minute(self):
        assert len(parse_free_games(_page([GIVEAWAY]), now=datetime(2026, 8, 13, 7, 59))) == 1

    def test_announced_but_not_started_is_ignored(self):
        assert parse_free_games(_page([GIVEAWAY]), now=datetime(2026, 8, 5, 0, 0)) == []


class TestLinkHandling:
    def test_lookalike_claim_host_is_rejected(self):
        entry = dict(GIVEAWAY, links=[{"param": "https://register.ubisoft.com.evil.tld/free-game"}])
        assert parse_free_games(_page([entry]), now=NOW) == []

    def test_entry_without_any_link_is_rejected(self):
        assert parse_free_games(_page([dict(GIVEAWAY, links=[])]), now=NOW) == []

    def test_slug_reading(self):
        assert _slug("https://register.ubisoft.com/ghost-recon-future-soldier") == "ghost-recon-future-soldier"
        assert _slug("https://register.ubisoft.com/some-game/en-US") == "some-game"
        assert _slug("https://register.ubisoft.com/") == ""


class TestBrokenInput:
    @pytest.mark.parametrize("html", [
        "",
        "<html><body>no state here</body></html>",
        "<html><script>window.__PRELOADED_STATE__ = {not json};</script></html>",
        "<html><script>window.__PRELOADED_STATE__ = {\"a\": 1}</script>",
    ])
    def test_unusable_page_gives_an_empty_list(self, html):
        assert parse_free_games(html, now=NOW) == []

    def test_state_without_a_feed(self):
        assert _news_entries(_extract_state(_page([]))) == []
        assert _extract_state("<script>window.__PRELOADED_STATE__ = {\"language\": {}};</script>") == {"language": {}}


class TestClaimPageWording:
    """Titles and headings as the real pages served them on 2026-08-07."""

    GIVEAWAY_PAGE = {"title": "Giveaway", "headings": ["GET YOUR FREE GAME!"]}
    TRIAL_PAGE = {"title": "Skull and Bones Free trial", "headings": ["TRY NOW FOR FREE!"]}
    DEMO_PAGE = {"title": "Star Wars Outlaws® Demo", "headings": ["Play the Free Demo"]}

    @pytest.fixture
    def claimer(self):
        return UbisoftClaimer()

    def test_giveaway_page_is_recognised(self, claimer):
        assert claimer._looks_like_giveaway(self.GIVEAWAY_PAGE)

    @pytest.mark.parametrize("page", [TRIAL_PAGE, DEMO_PAGE, {}, {"title": "", "headings": []}])
    def test_other_pages_are_not_claimed(self, claimer, page):
        assert not claimer._looks_like_giveaway(page)

    # Exactly what the page showed after a real claim on 2026-08-07.
    CLAIMED_PAGE = {
        "title": "Giveaway",
        "headings": ["GET YOUR FREE GAME!", "Have fun!"],
        "body": ("GET YOUR FREE GAME! Have fun! The game has been successfully added to your "
                 "Ubisoft Connect PC library. LAUNCH UBISOFT CONNECT PC DOWNLOAD UBISOFT CONNECT PC"),
    }
    UNCLAIMED_PAGE = {
        "title": "Giveaway",
        "headings": ["GET YOUR FREE GAME!"],
        "body": "GET YOUR FREE GAME! Get Ghost Recon Future Soldier free for a limited time! SELECT YOUR GAMING PLATFORM:",
    }

    def test_real_success_page_is_recognised(self, claimer):
        assert claimer._claim_confirmed(self.CLAIMED_PAGE)

    def test_page_before_claiming_is_not_a_confirmation(self, claimer):
        assert not claimer._claim_confirmed(self.UNCLAIMED_PAGE)
        assert not claimer._claim_confirmed({"body": "SELECT YOUR GAMING PLATFORM:"})
        assert not claimer._claim_confirmed({})

    def test_ownership_wording(self, claimer):
        assert claimer._already_owned({"body": "You already own this game."})
        assert not claimer._already_owned({"body": "GET YOUR FREE GAME!"})

    def test_claim_url_is_pinned_to_english(self, claimer):
        assert claimer._claim_url("https://register.ubisoft.com/x") == "https://register.ubisoft.com/x/en-US"
        assert claimer._claim_url("https://register.ubisoft.com/x/") == "https://register.ubisoft.com/x/en-US"


class TestNotificationVocabulary:
    """Statuses drive the summary filter in main.py, so they may not drift into free text."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "ubisoft.py").read_text(encoding="utf-8")
    ALLOWED = re.compile(r"^(claimed|existed|notified|available \(dry run\)|(failed|skipped)(:[a-z-]+)?)$")

    def test_every_status_follows_the_shared_vocabulary(self):
        statuses = set(re.findall(r'"status": "([^"]+)"', self.SOURCE))
        statuses |= set(re.findall(r'status = "([^"]+)"', self.SOURCE))
        assert statuses, "no statuses found, the scan stopped matching"
        unexpected = sorted(s for s in statuses if not self.ALLOWED.match(s))
        assert not unexpected, f"statuses outside the shared vocabulary: {unexpected}"

    def test_vnc_prompts_are_prefixed_with_the_store_name(self):
        titles = re.findall(r'self\._vnc_notice\(\s*"([^"]+)"', self.SOURCE)
        assert titles, "no VNC prompts found, the scan stopped matching"
        assert all(t.startswith("Ubisoft: ") for t in titles), titles
