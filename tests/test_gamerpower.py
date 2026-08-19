"""GamerPower routing and type filtering.

The instructions fallback used to be dead code because `run()` never carried the
field, so every giveaway whose URL hid its destination landed in "unknown".
"""

import re
from pathlib import Path

import pytest

from src.stores.gamerpower import (COVERED_ELSEWHERE, GamerPowerClaimer, classify_target,
                                   fanatical_game_id, is_wanted, itch_game_id,
                                   login_help_message, needs_otp, wanted_types)


class TestRoutingByHost:
    @pytest.mark.parametrize("url,expected", [
        ("https://store.steampowered.com/app/123/Some_Game/", "steam"),
        ("https://store.epicgames.com/en-US/p/some-game", "epic"),
        ("https://www.gog.com/en/game/some_game", "gog"),
        ("https://www.fanatical.com/en/game/some-game", "fanatical"),
        ("https://www.alienwarearena.com/ucf/show/123", "alienware"),
        ("https://some-dev.itch.io/some-game", "itchio"),
        ("https://www.indiegala.com/giveaway/some-game", "indiegala"),
        ("https://www.ubisoft.com/en-us/games/some-game", "ubisoft"),
    ])
    def test_known_stores(self, url, expected):
        assert classify_target(url) == expected

    @pytest.mark.parametrize("url", [
        "https://gog.com.evil.tld/free-game",
        "https://evil-fanatical.com/giveaway",
        "https://itch.io.attacker.tld/game",
        "https://notindiegala.com/giveaway",
        "https://ubisoft.com.evil.tld/free",
        "https://store.steampowered.com.phish.tld/app/1",
    ])
    def test_lookalike_hosts_are_never_a_store(self, url):
        # The old code did `"gog.com" in url`, which every one of these satisfies.
        assert classify_target(url) == "unknown"

    @pytest.mark.parametrize("url", ["", "not a url", "https://example.com/x"])
    def test_unrelated_input(self, url):
        assert classify_target(url) == "unknown"


class TestRoutingByInstructions:
    """The fallback for giveaways whose URL says nothing, which is 33 of 105 live entries."""

    GENERIC = "https://www.gamerpower.com/open/some-giveaway"

    @pytest.mark.parametrize("text,expected", [
        ("1. Log in to your IndieGala account. 2. Click claim.", "indiegala"),
        ("Visit Alienware Arena and redeem your key.", "alienware"),
        ("Register on Fanatical to receive the game.", "fanatical"),
        ("Head to itch.io and download it.", "itchio"),
    ])
    def test_instructions_decide_when_the_url_does_not(self, text, expected):
        assert classify_target(self.GENERIC, text) == expected

    def test_host_wins_over_instructions(self):
        # A Steam link stays Steam even if the text mentions another shop.
        assert classify_target("https://store.steampowered.com/app/1/X/",
                               "Also available on Fanatical") == "steam"

    @pytest.mark.parametrize("text", ["", None, "Just click the button."])
    def test_useless_instructions(self, text):
        assert classify_target(self.GENERIC, text) == "unknown"


class TestTypeFilter:
    def test_dlc_is_skipped_by_default(self):
        assert wanted_types(claim_dlc=False) == {"game", "early access"}
        assert not is_wanted({"type": "DLC"}, claim_dlc=False)

    def test_dlc_can_be_switched_on(self):
        assert "dlc" in wanted_types(claim_dlc=True)
        assert is_wanted({"type": "DLC"}, claim_dlc=True)

    @pytest.mark.parametrize("kind", ["Game", "game", "Early Access", "EARLY ACCESS"])
    def test_full_games_always_pass(self, kind):
        assert is_wanted({"type": kind}, claim_dlc=False)

    @pytest.mark.parametrize("entry", [{}, {"type": None}, {"type": ""}, {"type": "Other"}])
    def test_unknown_types_are_dropped(self, entry):
        assert not is_wanted(entry, claim_dlc=False)
        assert not is_wanted(entry, claim_dlc=True)


class TestCoveredElsewhere:
    """Ubisoft giveaways reach us through the ubisoft store, not through GamerPower."""

    def test_ubisoft_is_marked_as_covered(self):
        assert COVERED_ELSEWHERE.get("ubisoft") == "ubisoft"

    def test_stores_we_delegate_to_are_not_marked_covered(self):
        for store in ("steam", "epic", "gog", "fanatical", "itchio", "indiegala", "alienware"):
            assert store not in COVERED_ELSEWHERE


class TestProductPageFilter:
    """A giveaway that resolves to a storefront banner is not a claimable page."""

    FILTER = staticmethod(GamerPowerClaimer._product_pages)

    def test_steam_app_and_sub_pages_pass(self):
        games = [
            {"title": "A", "final_url": "https://store.steampowered.com/app/1/a/"},
            {"title": "B", "final_url": "https://store.steampowered.com/sub/2/"},
        ]
        assert self.FILTER(games, ("/app/", "/sub/"), "Steam") == games

    def test_steam_landing_pages_are_dropped(self):
        games = [{"title": "A", "final_url": "https://store.steampowered.com/"}]
        assert self.FILTER(games, ("/app/", "/sub/"), "Steam") == []

    def test_epic_product_and_bundle_pages_pass(self):
        games = [
            {"title": "A", "final_url": "https://store.epicgames.com/en-US/p/game"},
            {"title": "B", "final_url": "https://store.epicgames.com/en-US/bundles/pack"},
        ]
        assert self.FILTER(games, ("/p/", "/bundles/"), "Epic") == games

    @pytest.mark.parametrize("url", [
        "https://store.epicgames.com/en-US/browse",
        "https://store.epicgames.com/en-US/mobile",
        "",
    ])
    def test_epic_non_product_pages_are_dropped(self, url):
        assert self.FILTER([{"title": "A", "final_url": url}], ("/p/", "/bundles/"), "Epic") == []

    def test_the_good_ones_survive_a_mixed_batch(self):
        games = [
            {"title": "keep", "final_url": "https://store.epicgames.com/en-US/p/game"},
            {"title": "drop", "final_url": "https://store.epicgames.com/en-US/browse"},
        ]
        kept = self.FILTER(games, ("/p/", "/bundles/"), "Epic")
        assert [g["title"] for g in kept] == ["keep"]


class TestTwoFactorDetection:
    """Issue #32: a code screen used to pass as a successful login and fail in silence."""

    LOGIN_PAGE = {"labelled": 0, "visibleTextFields": 1, "hasPassword": True, "talksAboutIt": False}
    SIGNED_IN = {"labelled": 0, "visibleTextFields": 1, "hasPassword": False, "talksAboutIt": False}

    def test_a_named_code_field_is_enough(self):
        assert needs_otp({"labelled": 1, "visibleTextFields": 2, "hasPassword": False,
                          "talksAboutIt": False})

    def test_an_unnamed_field_needs_the_page_to_say_so(self):
        assert needs_otp({"labelled": 0, "visibleTextFields": 1, "hasPassword": False,
                          "talksAboutIt": True})

    def test_the_real_itch_login_page_is_not_a_code_screen(self):
        # Measured live on itch.io/login: one text field, a password field, no 2FA wording.
        assert not needs_otp(self.LOGIN_PAGE)

    def test_a_signed_in_page_is_not_a_code_screen(self):
        # Measured live after signing in: the search box is the only text field.
        assert not needs_otp(self.SIGNED_IN)

    def test_a_password_screen_is_never_a_code_screen(self):
        # Typing an authenticator code into a password box would lock the account out.
        assert not needs_otp({"labelled": 0, "visibleTextFields": 1, "hasPassword": True,
                              "talksAboutIt": True})

    def test_wording_alone_with_several_fields_is_not_enough(self):
        assert not needs_otp({"labelled": 0, "visibleTextFields": 3, "hasPassword": False,
                              "talksAboutIt": True})

    @pytest.mark.parametrize("state", [None, {}, {"labelled": 0}])
    def test_unusable_input_is_never_a_code_screen(self, state):
        assert not needs_otp(state)


class TestItchGameId:
    """The database key must be itch.io's own identity, not the GamerPower link."""

    @pytest.mark.parametrize("url,expected", [
        ("https://truegamesstudio.itch.io/nightbell", "truegamesstudio.itch.io/nightbell"),
        ("https://truegamesstudio.itch.io/nightbell/", "truegamesstudio.itch.io/nightbell"),
        ("https://truegamesstudio.itch.io/nightbell/purchase", "truegamesstudio.itch.io/nightbell"),
        ("https://TrueGamesStudio.itch.io/NightBell", "truegamesstudio.itch.io/nightbell"),
        ("https://dev.itch.io/game?utm_source=gamerpower", "dev.itch.io/game"),
    ])
    def test_it_reads_creator_and_slug(self, url, expected):
        assert itch_game_id(url) == expected

    def test_two_games_by_one_creator_never_collide(self):
        assert itch_game_id("https://truegamesstudio.itch.io/nightbell") != \
               itch_game_id("https://truegamesstudio.itch.io/dire-echo")

    @pytest.mark.parametrize("url", ["", None, "not a url", "https://itch.io/"])
    def test_unusable_input_yields_nothing(self, url):
        # The caller falls back to the giveaway URL when this is empty.
        assert itch_game_id(url) == ""


class TestSideStoreNavigation:
    """Every giveaway must be judged on its own page.

    Checking only the host let the second game inherit the first one's "you own this"
    banner, which recorded five games as owned that the account never had.
    """

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_the_host_only_shortcut_is_gone(self):
        for host in ('"itch.io" not in current_url', '"fanatical.com" not in current_url',
                     '"indiegala.com" not in current_url'):
            assert host not in self.SOURCE, f"the host-only navigation guard is back: {host}"

    def test_every_side_store_compares_the_whole_url(self):
        assert self.SOURCE.count("current_url.startswith(url)") == 4

    def test_a_dry_run_cannot_record_ownership(self):
        # The existed branch used to write to the database before the dry-run guard.
        owned_branch = self.SOURCE.split("async def _claim_itchio_game", 1)[1].split("needs_login", 1)[0]
        assert "if cfg.dryrun:" in owned_branch
        assert owned_branch.index("if cfg.dryrun:") < owned_branch.index("async_session()")


class TestFanaticalGameId:
    """The database key follows Fanatical's own slug, not the GamerPower link."""

    @pytest.mark.parametrize("url,expected", [
        ("https://www.fanatical.com/en/game/some-game", "some-game"),
        ("https://www.fanatical.com/en/giveaway/free-weekend-thing", "free-weekend-thing"),
        ("https://www.fanatical.com/de/game/some-game?ref=gamerpower", "some-game"),
        ("https://www.fanatical.com/en/bundle/indie-pack", "indie-pack"),
    ])
    def test_it_reads_the_slug(self, url, expected):
        assert fanatical_game_id(url) == expected

    @pytest.mark.parametrize("url", ["", None, "https://www.fanatical.com/en/", "not a url"])
    def test_unusable_input_yields_nothing(self, url):
        # The caller falls back to the giveaway URL when this is empty.
        assert fanatical_game_id(url) == ""


class TestFanaticalClaimHonesty:
    """The claim used to be reported as a win whether or not the page agreed."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
    BLOCK = SOURCE.split("async def _claim_fanatical_game", 1)[1].split("async def _claim_alienware_game", 1)[0]

    def test_the_unconditional_success_is_gone(self):
        # Both arms of the old check ended with `claimed = True; break`.
        assert self.BLOCK.count("claimed = True") == 0

    def test_the_claim_is_confirmed_against_the_page(self):
        assert "stillOffered" in self.BLOCK

    def test_an_unconfirmed_claim_is_reported_as_such(self):
        assert 'failed:unconfirmed' in self.BLOCK

    def test_credentials_are_not_pasted_into_javascript(self):
        # `f'("{password}")'` broke on any quote in the password and injected into the page.
        assert '("{email}")' not in self.BLOCK and '("{password}")' not in self.BLOCK


class TestCaptchaAndNotifications:
    """A captcha must reach you, and NOTIFY_SKIP_STORES must be able to silence it."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_the_store_names_itself(self):
        # Without this it inherited "base", so NOTIFY_SKIP_STORES=gamerpower silenced nothing.
        assert 'store_name = "gamerpower"' in self.SOURCE

    def test_the_browser_profile_keeps_its_old_folder(self):
        # Renaming it would throw away every side-store session already logged in.
        assert 'profile_name = "base"' in self.SOURCE

    def test_the_login_path_checks_for_a_human_check(self):
        finisher = self.SOURCE.split("async def _confirm_side_login", 1)[1].split("async def _fill_otp", 1)[0]
        assert "_human_challenge_present()" in finisher and "_wait_out_challenge" in finisher

    @pytest.mark.parametrize("store,marker", [
        ("itch", '_clear_challenge("Itch.io")'),
        ("fanatical", '_clear_challenge("Fanatical")'),
    ])
    def test_the_claim_path_checks_too(self, store, marker):
        # A captcha during the claim used to end as a plain "could not click".
        assert marker in self.SOURCE


class TestRecoveryCodeHandling:
    """Spending a recovery code must mirror gog.py, including never reusing one."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
    BLOCK = SOURCE.split("async def _fill_backup_code", 1)[1].split("async def _clear_challenge", 1)[0]

    def test_it_picks_the_first_unused_code(self):
        assert "next((c for c in codes if c not in used)" in self.BLOCK

    def test_it_records_the_code_it_spent(self):
        assert "used_file.open(" in self.BLOCK and "print(raw_code, file=fh)" in self.BLOCK

    def test_it_uses_the_same_data_folder_as_gog(self):
        assert "cfg._data_dir / used_name" in self.BLOCK

    def test_exhausted_codes_do_not_crash_the_run(self):
        assert "Every recovery code has been used already" in self.BLOCK

    def test_itch_passes_its_codes_only_when_switched_on(self):
        call = self.SOURCE.split('"Itch.io", self._itch_logged_in', 1)[1][:300]
        assert "cfg.itchio_otp_codes if cfg.itchio_otp_enable else None" in call


class TestLoginHelpMessage:
    """The VNC ping should say what the page is actually asking for."""

    def test_a_code_screen_asks_you_for_the_code(self):
        # No store keeps an authenticator secret, so the message never advertises one.
        msg = login_help_message("Itch.io", True)
        assert msg == "Itch.io is asking for your authenticator code. Open the browser and type it."
        assert "OTPKEY" not in msg

    def test_a_plain_login_failure_never_mentions_codes(self):
        msg = login_help_message("Fanatical", False)
        assert "did not accept the automated sign-in" in msg
        assert "code" not in msg.lower()

    def test_a_spent_recovery_code_is_reported(self):
        msg = login_help_message("Itch.io", True, tried_backup=True)
        assert "recovery code was spent" in msg

    def test_every_message_names_the_store(self):
        for code_screen in (True, False):
            assert login_help_message("Itch.io", code_screen).startswith("Itch.io")


class TestNotificationVocabulary:
    """Statuses drive the summary filter in main.py, so they may not drift into free text.

    Scoped to the slug family (Ubisoft, Fab, GamerPower, Unity). GOG, Prime and AliExpress
    write sentences instead and are a separate, older convention.
    """

    STORES = ("gamerpower", "epic_fab", "ubisoft")
    ALLOWED = re.compile(r"^(claimed|existed|notified|available \(dry run\)|(failed|skipped)(:[a-z-]+)?)")

    @pytest.mark.parametrize("store", STORES)
    def test_every_status_follows_the_shared_vocabulary(self, store):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / f"{store}.py").read_text(encoding="utf-8")
        statuses = set(re.findall(r'"status": "([^"]+)"', source))
        statuses |= set(re.findall(r'status = "([^"]+)"', source))
        statuses |= set(re.findall(r'notify_game\["status"\] = "([^"]+)"', source))
        statuses |= set(re.findall(r'status="([^"]+)"', source))
        assert statuses, f"no statuses found in {store}.py, the scan stopped matching"
        unexpected = sorted(s for s in statuses if not self.ALLOWED.match(s))
        assert not unexpected, f"{store}.py uses statuses outside the shared vocabulary: {unexpected}"

    def test_vnc_prompts_use_the_project_titles(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
        assert "2FA code needed" in source, "a code screen should use the same title as Epic, Fab and Ubisoft"
        assert "sign-in needs you" not in source, "the project says 'login needs you'"

    def test_side_stores_report_who_signed_in(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
        # Not BaseClaimer.log_signed_in: that also rewrites self.user, which this claimer
        # keeps as the database key for every side store.
        assert source.count("_log_side_signed_in(") >= 4, "each side store should log the account it uses"
        assert "Signed in as:" in source
