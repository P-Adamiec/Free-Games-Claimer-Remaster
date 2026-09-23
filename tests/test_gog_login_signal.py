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
