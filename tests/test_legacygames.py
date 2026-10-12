"""Legacy Games codes from Prime: which promo page, what the answer means, and the order of the steps."""

from pathlib import Path

import json

import pytest

from src.stores.gog import is_gog_code
from src.stores.legacygames import legacy_listed, legacy_promo_candidates, legacy_redeem_link, legacy_redeem_outcome

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "src" / "stores" / "legacygames.py").read_text(encoding="utf-8")
PRIME_URL = "https://luna.amazon.com/claims/the-da-vinci-cryptex-legacy/dp/amzn1.pg.item.6cd3cd34"


class TestPromoCandidates:
    def test_the_page_prime_linked_comes_first(self):
        stored = "https://promo.legacygames.com/the-da-vinci-cryptex-luna/"
        assert legacy_promo_candidates(PRIME_URL, stored)[0] == stored

    def test_an_old_code_without_a_link_gets_the_pages_its_name_points_to(self):
        # Checked live on 2026-10-09: -prime-deal answered 404 for this game, -luna is its page.
        assert legacy_promo_candidates(PRIME_URL) == [
            "https://promo.legacygames.com/the-da-vinci-cryptex-luna/",
            "https://promo.legacygames.com/the-da-vinci-cryptex-prime-deal/",
        ]

    def test_a_stored_link_on_another_host_is_ignored(self):
        assert "https://evil.example/x" not in legacy_promo_candidates(PRIME_URL, "https://evil.example/x")

    def test_a_url_without_a_claim_slug_gives_nothing_to_guess(self):
        assert legacy_promo_candidates("https://luna.amazon.com/claims/home") == []


class TestRedeemLink:
    @pytest.mark.parametrize("hrefs,link", [
        (["https://luna.amazon.com/", "https://promo.legacygames.com/big-adventure-luna/"],
         "https://promo.legacygames.com/big-adventure-luna/"),
        (["https://legacygames.com/", "https://www.amazon.com/legacy"], ""),
        (["https://promo.legacygames.com.evil.net/x"], ""),
        (["http://promo.legacygames.com/x"], ""),
        ([], ""),
    ])
    def test_only_the_promo_host_counts(self, hrefs, link):
        assert legacy_redeem_link(hrefs) == link


class TestListedInFreeGames:
    # Rows as My Free Games showed them on the owner's account on 2026-10-09.
    ROWS = [
        ["", "Facility 47", "November 6, 2022", "2983209"],
        ["Prime Gaming Giveaway – Rose Riddle 2: Werewolf Shadow", "Rose Riddle 2: Werewolf Shadow", "December 1, 2022", "3212900"],
        ["Prime Gaming Giveaway – City Legends: Trapped In Mirror CE", "City Legends: Trapped In Mirror CE", "April 20, 2023", "3622038"],
        ["", "Lost Lands: Sand Captivity CE", "January 21, 2024", "4772917"],
        ["", "Mystery Box 2", "May 1, 2024", "5000001"],
        ["Amazon Luna Giveaway – The Da Vinci Cryptex", "The Da Vinci Cryptex 1", "October 9, 2026", "9254609"],
    ]

    @pytest.mark.parametrize("title,listed", [
        ("The Da Vinci Cryptex", True),
        ("Rose Riddle 2: Werewolf Shadow", True),
        ("Lost Lands: Sand Captivity", True),
        ("Facility 47", True),
        ("Rose Riddle", False),
        ("Facility", False),
        ("Mystery Box", False),
        ("Mystery Box 2", True),
        ("City Legends", False),
        ("The Da Vinci", False),
        ("", False),
    ])
    def test_a_row_must_be_this_game(self, title, listed):
        assert legacy_listed(self.ROWS, title) is listed

    def test_an_empty_list_lists_nothing(self):
        assert legacy_listed([], "The Da Vinci Cryptex") is False


class TestRedeemOutcome:
    @pytest.mark.parametrize("text,form,outcome", [
        ("Thanks for redeeming your Amazon Luna code! Here is how you can access your free game.", False, "redeemed"),
        ("Please check your code or email and try again", True, "rejected"),
        ("Enter your redemption code\nType your code and email address below to redeem", True, "unknown"),
        ("", False, "unknown"),
    ])
    def test_the_answer_page_decides(self, text, form, outcome):
        assert legacy_redeem_outcome({"text": text, "form": form}) == outcome

    def test_thanks_while_the_form_is_still_there_is_no_answer_yet(self):
        # Help text on the form page must not count as the server's answer.
        assert legacy_redeem_outcome({"text": "Thanks for redeeming with us", "form": True}) == "unknown"


class TestTheFlow:
    REDEEM = SOURCE.split("async def _redeem", 1)[1].split("\n    async def ", 1)[0]

    def test_the_dry_run_stops_before_the_form_is_sent(self):
        assert self.REDEEM.index("cfg.dryrun") < self.REDEEM.index("_send_code(")

    def test_the_free_games_list_is_asked_before_a_code_is_sent_again(self):
        # A retry after an unclear answer would otherwise send a code that already went through and read as rejected.
        assert self.REDEEM.index("_in_free_games(") < self.REDEEM.index("_send_code(")

    def test_the_newsletter_is_unticked_before_sending(self):
        send = SOURCE.split("async def _send_code", 1)[1].split("\n    async def ", 1)[0]
        assert send.index("NEWSLETTER_OFF_JS") < send.index("button.click()")

    def test_a_rejected_code_is_checked_against_the_list_first(self):
        # A code that went through on an earlier run is answered with "check your code" too.
        after = self.REDEEM.split("_send_code(", 1)[1]
        assert after.index('outcome == "rejected" and await self._in_free_games(') < after.index('elif outcome == "rejected"')

    def test_the_form_is_not_sent_with_the_newsletter_ticked(self):
        send = SOURCE.split("async def _send_code", 1)[1].split("\n    async def ", 1)[0]
        assert "if not await self.page.evaluate(NEWSLETTER_OFF_JS):" in send

    def test_the_typed_fields_are_read_back_before_sending(self):
        send = SOURCE.split("async def _send_code", 1)[1].split("\n    async def ", 1)[0]
        assert send.index("typed != [") < send.index("button.click()")

    def test_only_recent_codes_are_sent(self):
        pending = SOURCE.split("async def _pending_codes", 1)[1].split("\n    async def ", 1)[0]
        assert "ClaimedGame.created_at >= since" in pending and "PENDING_DAYS = 60" in SOURCE

    def test_a_code_without_a_page_stops_being_retried(self):
        assert "failed:no-redeem-page" in self.REDEEM and '"redeem failed"' in self.REDEEM

    def test_it_is_a_store_of_its_own(self):
        assert 'store_name = "legacygames"' in SOURCE

    def test_a_failed_sign_in_redeems_nothing(self):
        run = SOURCE.split("async def redeem_pending_codes", 1)[1]
        assert run.index("_ensure_logged_in()") < run.index("self._redeem(")
        assert "needs_you(self.store_name" in run

    def test_main_runs_it_when_the_run_includes_it(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        block = main.split("# Legacy Games codes from Prime Gaming", 1)[1].split("\n    # ", 1)[0]
        assert '"legacygames" not in redeemers' in block and "redeem_pending_codes()" in block
        # On by default: the settings are judged where the waiting codes are known, not here.
        assert "cfg.lg_email" not in block

    def test_the_settings_are_asked_about_only_when_a_code_waits(self):
        run = SOURCE.split("async def redeem_pending_codes", 1)[1].split("\n    async def ", 1)[0]
        assert run.index("if not pending:") < run.index("cfg.lg_email and cfg.lg_password") < run.index("start_browser()")

    @pytest.mark.parametrize("typed", ["legacy", "lg", "LG", "legacygames", "legacy-games"])
    def test_every_name_you_can_type_picks_it(self, typed):
        import main
        assert main._resolve_stores([typed]) == ["legacygames"]

    def test_it_runs_by_default(self):
        # Prime runs by default and its Legacy Games codes expire within days.
        import main
        assert "legacygames" in main.DEFAULT_STORES and "legacygames" in main._selectable()

    def test_the_summary_and_the_prompts_use_one_key(self):
        import main
        assert main._store_key("Legacy Games") == "legacygames"

    def test_prime_keeps_the_link_it_was_given(self):
        prime = (ROOT / "src" / "stores" / "prime.py").read_text(encoding="utf-8")
        assert 'extra["redeem_url"] = link' in prime and "legacy_redeem_link(" in prime


class TestGOGLeavesLegacyCodesAlone:
    """GOG runs first and took any 18-character uppercase code, a Legacy Games one too."""

    @pytest.mark.parametrize("code,store,is_gog", [
        ("ABCDEFGHIJKLMNOPQR", "gog", True),
        ("ABCDEFGHIJKLMNOPQR", None, True),
        ("ABCDEFGHIJKLMNOPQR", "unknown", True),
        ("ABCDEFGHIJKLMNOPQR", "legacy games", False),
        ("ABCDEFGHIJKLMNOPQR", "microsoft store", False),
        ("ABCDEFGHIJKLMNOPQ", None, False),
        ("abcdefghijklmnopqr", None, False),
        ("ABCDE-FGHIJ-KLMNOP", None, False),
    ])
    def test_another_stores_code_is_never_gog(self, code, store, is_gog):
        extra = json.dumps({"external_store": store}) if store else None
        assert is_gog_code(code, extra) is is_gog

    def test_broken_extra_falls_back_to_the_shape(self):
        assert is_gog_code("ABCDEFGHIJKLMNOPQR", "{not json") is True


class TestOnByDefaultStaysQuiet:
    """Legacy Games runs by default, so someone without codes or without an account must not be nagged."""

    class _Stub:
        def __init__(self, pending):
            self.pending, self.started, self.notify_games = pending, False, []
            self.store_name = "legacygames"

        async def _pending_codes(self):
            return self.pending

        async def start_browser(self):
            self.started = True

        async def _ensure_logged_in(self):
            return False

        async def close_browser(self):
            pass

    @pytest.fixture()
    def settings(self, monkeypatch):
        import src.stores.legacygames as lg

        def use(email, password):
            monkeypatch.setattr(lg.cfg, "lg_email", email)
            monkeypatch.setattr(lg.cfg, "lg_password", password)
        return use

    def _run(self, stub, caplog):
        import asyncio
        import logging

        from src.core.run_state import reset_run_state
        from src.stores.legacygames import LegacyGamesClaimer

        with caplog.at_level(logging.DEBUG, logger="fgc.legacygames"):
            asyncio.run(LegacyGamesClaimer.redeem_pending_codes(stub))
        reset_run_state()
        return [r for r in caplog.records if r.levelno >= logging.INFO]

    def test_no_codes_says_nothing_and_opens_nothing(self, settings, caplog):
        settings(None, None)
        stub = self._Stub([])
        assert self._run(stub, caplog) == [] and not stub.started

    def test_codes_without_an_account_get_one_line(self, settings, caplog):
        from types import SimpleNamespace
        settings(None, None)
        stub = self._Stub([SimpleNamespace(title="Game", url="u", code="ABCDE-12345")])
        lines = self._run(stub, caplog)
        assert len(lines) == 1 and "LG_EMAIL" in lines[0].getMessage() and not stub.started

    def test_codes_with_an_account_open_the_browser(self, settings, caplog):
        from types import SimpleNamespace
        settings("me@example.com", "secret")
        stub = self._Stub([SimpleNamespace(title="Game", url="u", code="ABCDE-12345")])
        self._run(stub, caplog)
        assert stub.started

