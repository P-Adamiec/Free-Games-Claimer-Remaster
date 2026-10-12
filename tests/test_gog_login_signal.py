"""What GOG's login check is allowed to believe.

gog.com serves the same markup signed in or out, so the old check read the account
menu button and reported people as signed in under the name "account" (issue #38).
The only honest answer comes from GOG's own menu service, and these guards keep it
that way; the live browser proved both states, this pins the source.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "src" / "stores" / "gog.py").read_text(encoding="utf-8")
CHECK = SOURCE.split("async def _is_logged_in", 1)[1].split("\n        # First check", 1)[0]


class TestTheCheckAsksGOGItself:

    def test_it_reads_the_account_menu_service(self):
        assert "menu.gog.com/v1/account/basic" in CHECK
        assert "isLoggedIn" in CHECK

    def test_it_sends_the_session_along(self):
        # Without the cookies the service answers "nobody", which would look like a logout.
        assert "credentials: 'include'" in CHECK

    def test_it_waits_for_the_answer(self):
        # An unawaited promise comes back as an object and every run would read as signed out.
        assert "await_promise=True" in CHECK


class TestThePageIsNotEvidence:

    def test_the_menu_button_no_longer_decides(self):
        assert "menuAccountButton" not in CHECK
        assert "hasSignIn" not in CHECK

    def test_a_name_is_never_taken_from_page_text(self):
        # The one place a username is set, and it comes from the service.
        assert CHECK.count("self.user =") == 1
        assert "self.user = str(result.get(\"user\")" in CHECK

    def test_an_unknown_name_falls_back_instead_of_guessing(self):
        assert '"GOG User"' in CHECK


class TestACodeGOGSaysIsBeingRedeemed:
    """Issue #66: "This code is currently being redeemed by someone" came back every run as "check manually"."""

    REDEEM = SOURCE.split("async def _redeem_gog_code", 1)[1].split("\nasync def ", 1)[0]
    LOCKED = REDEEM.split("elif result_state == 'locked':", 1)[1].split("\n            else:", 1)[0]

    def test_the_message_is_its_own_state(self):
        assert "currently being redeemed" in self.REDEEM and "return 'locked'" in self.REDEEM

    def test_it_is_read_before_success_is_guessed(self):
        # "redeemed" in that sentence would otherwise pass for a successful redemption.
        assert self.REDEEM.index("return 'locked'") < self.REDEEM.index("return 'success'")

    def test_the_sign_in_is_checked_and_the_code_tried_once_more(self):
        assert "self._ensure_logged_in()" in self.LOCKED
        assert "retried=True" in self.LOCKED and "if not retried:" in self.LOCKED

    def test_it_stays_out_of_the_summary_and_the_database(self):
        assert "notify_games" not in self.LOCKED and "obj.status" not in self.LOCKED


class TestADryRunRedeemsNothing:
    """TODO 49: a dry run redeemed Prime's GOG codes left over from an earlier run."""

    REDEEM = TestACodeGOGSaysIsBeingRedeemed.REDEEM

    def test_it_stops_before_the_first_button(self):
        stop = self.REDEEM.index("if cfg.dryrun:")
        assert stop < self.REDEEM.index("return", stop) < self.REDEEM.index("t === 'continue'")
        assert self.REDEEM.index("t === 'continue'") < self.REDEEM.index("'activate'")

    def test_it_is_listed_as_a_dry_run(self):
        block = self.REDEEM.split("if cfg.dryrun:", 1)[1].split("return", 1)[0]
        assert '"available (dry run)"' in block
