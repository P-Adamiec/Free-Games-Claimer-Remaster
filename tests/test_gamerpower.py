"""GamerPower routing and type filtering.

The instructions fallback used to be dead code because `run()` never carried the
field, so every giveaway whose URL hid its destination landed in "unknown".
"""

import asyncio
import re
from pathlib import Path

import pytest

from src.stores.gamerpower import (COVERED_ELSEWHERE, FAN_GIVEAWAY_JS, FAN_MARK_MAIN_JS, FAN_MARK_PAY_JS,
                                   FAN_NEWSLETTER_JS, FAN_ORDER_JS, FAN_ORDERS_JS, FAN_REVEAL_JS, FAN_UNSUBSCRIBE_JS,
                                   GamerPowerClaimer, classify_target, download_only_status, fanatical_game_id,
                                   fanatical_item_is_steam, fanatical_page, fanatical_price, fanatical_receipt_authorised,
                                   fanatical_receipt_order,
                                   counts_as_done, fanatical_next_step, fanatical_should_unsubscribe,
                                   find_fanatical_item, is_product_page,
                                   is_wanted, itch_game_id, login_help_message, needs_otp,
                                   steam_key_in, wanted_types)


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

    @pytest.mark.parametrize("url", [
        "https://store.steampowered.com/app/1/a/",
        "https://store.steampowered.com/sub/2/",
    ])
    def test_steam_app_and_sub_pages_pass(self, url):
        assert is_product_page("steam", url)

    def test_steam_landing_pages_are_dropped(self):
        assert not is_product_page("steam", "https://store.steampowered.com/")

    @pytest.mark.parametrize("url", [
        "https://store.epicgames.com/en-US/p/game",
        "https://store.epicgames.com/en-US/bundles/pack",
    ])
    def test_epic_product_and_bundle_pages_pass(self, url):
        assert is_product_page("epic", url)

    @pytest.mark.parametrize("url", [
        "https://store.epicgames.com/en-US/browse",
        "https://store.epicgames.com/en-US/mobile",
        "",
    ])
    def test_epic_non_product_pages_are_dropped(self, url):
        assert not is_product_page("epic", url)

    def test_a_site_with_no_rule_is_left_alone(self):
        assert is_product_page("itchio", "https://itch.io/s/anything")


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
        # Itch.io is not among them any more: it always reloads the game page after signing in.
        assert self.SOURCE.count("current_url.startswith(url)") == 3

    def test_a_dry_run_cannot_record_ownership(self):
        # The existed branch used to write to the database before the dry-run guard.
        body = self.SOURCE.split("async def _claim_itchio_game", 1)[1]
        owned_branch = body.split("_itch_owns_this()", 1)[1].split("_itch_run_claim(", 1)[0]
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

    def test_the_claim_is_confirmed_against_the_account(self):
        # A COMPLETE order decides; a game sitting in the cart once counted as claimed (#71).
        assert "_fanatical_complete_order(" in self.BLOCK
        assert "_fanatical_claim_left_page" not in self.SOURCE and "stillOffered" not in self.SOURCE

    def test_sign_in_is_judged_by_the_header_only(self):
        # Every giveaway lists "Create or Sign in to a Fanatical account" as a step, signed in or not (#71).
        assert "Create or Sign in to a Fanatical account" not in self.BLOCK
        assert "if not await self._fanatical_signed_in():" in self.BLOCK

    def test_the_claim_goes_through_the_checkout(self):
        claim = self.BLOCK.index('FAN_MARK_MAIN_JS, "Claim button"')
        assert self.BLOCK.index("_fanatical_finish_steps(") < claim < self.BLOCK.index("_fanatical_checkout(")

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
        assert "self._next_unused_code(codes, used_name)" in self.BLOCK

    def test_it_records_the_code_it_spent(self):
        assert "self._mark_code_used(code, used_name, codes)" in self.BLOCK

    def test_it_keeps_no_second_copy_of_the_bookkeeping(self):
        # One implementation lives in BaseClaimer; this file used to hold a second.
        assert "used_file" not in self.BLOCK

    def test_exhausted_codes_do_not_crash_the_run(self):
        assert "Every recovery code has been used already" in self.BLOCK

    def test_itch_passes_its_codes_only_when_switched_on(self):
        call = self.SOURCE.split('"Itch.io", self._itch_logged_in', 1)[1][:300]
        assert "backup_codes=cfg.itchio_otp_codes" in call

    def test_itch_hands_over_its_authenticator_secret_too(self):
        call = self.SOURCE.split('"Itch.io", self._itch_logged_in', 1)[1][:300]
        assert "otp_key=cfg.itchio_otp_key" in call

    def test_fanatical_hands_over_its_authenticator_secret(self):
        call = self.SOURCE.split('"Fanatical", self._fanatical_signed_in', 1)[1][:300]
        assert "otp_key=cfg.fanatical_otp_key" in call and "backup_codes" not in call

    @pytest.mark.parametrize("label,pressed", [
        ("Authenticate", True), ("Verify", True), ("Sign in", True),
        ("Sign in with Apple", False), ("Change account", False),
    ])
    def test_the_code_is_sent_with_the_button_the_site_shows(self, label, pressed):
        # Fanatical's code screen only has "Authenticate"; without it the code was typed and never sent (#75).
        block = self.SOURCE.split("async def _type_otp", 1)[1].split("async def _code_screen_still_up", 1)[0]
        button = re.search(r"const words = /(.+?)/i;", block).group(1)
        assert bool(re.search(button, label, re.I)) is pressed

    def test_the_code_box_s_own_form_is_searched_first(self):
        # Searched page-wide, Fanatical's header "Sign in" came first and closed the code screen (#75).
        block = self.SOURCE.split("async def _type_otp", 1)[1].split("async def _code_screen_still_up", 1)[0]
        assert block.index("own.find(named)") < block.index("buttons(document).find(named)")
        assert ".closest('form')" in block

    def test_the_secret_is_tried_before_a_code_is_spent(self):
        block = self.SOURCE.split("async def _confirm_side_login", 1)[1].split("async def _type_otp", 1)[0]
        assert block.index("_fill_totp(") < block.index("_fill_backup_code(")


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


class TestDownloadOnlyGiveaways:
    """Itch.io hands some giveaways out as a file: that is not a failed claim."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_the_first_run_says_it_plainly(self):
        status = download_only_status(True)
        assert "download only" in status
        # Must dodge every word the summary filter hides.
        for hidden in ("skip", "fail", "exist", "already"):
            assert hidden not in status.lower()

    def test_later_runs_stay_quiet(self):
        assert download_only_status(False) == "skipped:download-only"

    def test_the_walk_reports_why_it_stopped(self):
        block = self.SOURCE.split("async def _itch_run_claim", 1)[1].split("async def ", 1)[0]
        assert '"download-only"' in block and '"blocked"' in block and '"clicked"' in block
        assert "return False" not in block

    def test_a_download_only_giveaway_is_not_a_failed_claim(self):
        block = self.SOURCE.split("async def _claim_itchio_game", 1)[1].split("async def ", 1)[0]
        assert 'elif walked == "download-only"' in block
        assert "skipped:download-only" in block


class TestASaleThatIsNoLongerFree:
    """Express No. 6: GamerPower kept listing it for days after itch.io put it back at $0.39."""

    def test_it_never_reaches_a_notification(self, monkeypatch):
        import asyncio

        from src.stores import gamerpower as gp

        async def _yes(*_a, **_kw):
            return True

        async def _no(*_a, **_kw):
            return False

        async def _nothing(*_a, **_kw):
            return None

        async def _not_free(_title):
            return "not-free"

        monkeypatch.setattr(gp.cfg, "dryrun", False)
        claimer = gp.GamerPowerClaimer.__new__(gp.GamerPowerClaimer)
        claimer.notify_games = []
        claimer.page = type("Page", (), {"get": staticmethod(_nothing)})()
        monkeypatch.setattr(claimer, "_itch_session_ready", _yes)
        monkeypatch.setattr(claimer, "_itch_owns_this", _no)
        monkeypatch.setattr(claimer, "_itch_run_claim", _not_free)
        monkeypatch.setattr(claimer, "sleep", _nothing)

        asyncio.run(claimer._claim_itchio_game({"title": "Express No. 6", "final_url": "https://askgames.itch.io/express-no6"}))
        assert claimer.notify_games == []

    def test_the_walk_tells_a_price_apart_from_a_blocked_page(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
        block = source.split("async def _itch_run_claim", 1)[1].split("async def ", 1)[0]
        assert 'return "not-free"' in block


class TestIndieGalaSignInCheck:
    """It asked for a manual login even when signed in, because it guessed CSS classes (issue #47)."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_the_guessed_classes_are_gone(self):
        # Checked live: IndieGala uses none of these, so the old check could never say "signed in".
        for guess in (".user-menu", ".user-avatar", ".profile-link"):
            assert guess not in self.SOURCE

    def test_one_method_decides(self):
        block = self.SOURCE.split("async def _claim_indiegala_game", 1)[1].split("\n    async def ", 1)[0]
        assert "_ig_logged_in()" in block
        assert "needs_login" not in block

    def test_the_check_reads_links_not_page_text(self):
        block = self.SOURCE.split("async def _ig_logged_in", 1)[1].split("\n    def ", 1)[0]
        assert "logout" in block
        assert "add to library" not in block


class TestItchSessionAfterCloudflare:
    """#59: right after Cloudflare's page clears itch.io is still loading, so one look read a signed-in session as out."""

    class _Stub:
        def __init__(self, answers):
            self.answers, self.calls, self.slept = list(answers), 0, 0

        async def _itch_logged_in(self):
            self.calls += 1
            return self.answers.pop(0) if self.answers else False

        async def sleep(self, seconds):
            self.slept += seconds

    def test_a_session_that_shows_up_while_the_page_loads_counts(self):
        stub = self._Stub([False, False, True])
        assert asyncio.run(GamerPowerClaimer._itch_logged_in_after_load(stub)) is True
        assert stub.calls == 3 and stub.slept == 4

    def test_a_signed_out_page_gives_up_after_the_limit(self):
        stub = self._Stub([])
        assert asyncio.run(GamerPowerClaimer._itch_logged_in_after_load(stub, seconds=12)) is False
        assert stub.calls == 7 and stub.slept == 12

    def test_the_session_check_waits_for_the_page_before_asking_you(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
        ready = source.split("async def _itch_session_ready", 1)[1].split("\n    async def ", 1)[0]
        assert ready.index("_itch_logged_in_after_load()") < ready.index("_wait_for_vnc_login(")


class TestItchOwnershipIsCheckedSignedIn:
    """A signed-out itch.io page shows no ownership banner, so the session comes first."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py")         .read_text(encoding="utf-8")
    BLOCK = SOURCE.split("async def _claim_itchio_game", 1)[1]         .split("async def _claim_indiegala_game", 1)[0]

    def test_the_session_is_ready_before_anything_is_judged(self):
        # The first giveaway of a run used to be walked through a claim it already owned.
        assert self.BLOCK.index("_itch_session_ready()") < self.BLOCK.index("_itch_owns_this()")

    def test_ownership_is_still_checked_before_claiming(self):
        assert self.BLOCK.index("_itch_owns_this()") < self.BLOCK.index("_itch_run_claim(")

    def test_an_owned_game_is_reported_as_owned(self):
        owned = self.BLOCK.split("_itch_owns_this()", 1)[1][:400]
        assert "already owned" in owned and '"existed"' in owned

    def test_being_signed_in_is_judged_on_itchio_itself(self):
        # A creator's subdomain carries no sign-in link, so the old page-text guess said "no login
        # needed" while signed out, and every check after it read a signed-out page.
        session = self.SOURCE.split("async def _itch_session_ready", 1)[1].split('\n    async def ', 1)[0]
        assert 'page.get("https://itch.io/")' in session
        assert "_itch_logged_in_after_load()" in session

    def test_the_session_is_only_established_once(self):
        session = self.SOURCE.split("async def _itch_session_ready", 1)[1].split('\n    async def ', 1)[0]
        assert "if self._itch_session_ok:" in session
        assert "self._itch_session_ok = True" in session

    def test_the_old_guess_is_gone(self):
        # Fanatical keeps its own check, it reads a prompt that names the site.
        assert "needs_login" not in self.BLOCK


class TestSideStorePrompts:
    """Issue #59: itch.io called you while signed in, and the prompt said "gamerpower"."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")

    def test_every_prompt_names_the_site(self):
        calls = re.findall(r"_wait_for_vnc_login\((.*?)\)\s*:", self.SOURCE, re.S)
        assert calls, "no VNC waits found, the scan stopped matching"
        assert all("custom_msg" in c for c in calls), calls

    def test_cloudflare_is_let_through_before_the_session_is_judged(self):
        ready = self.SOURCE.split("async def _itch_session_ready", 1)[1].split("\n    async def ", 1)[0]
        assert ready.index("_human_challenge_present()") < ready.index("if await self._itch_logged_in_after_load()")
        assert "_wait_out_challenge(" in ready


class TestIndieGalaSignIn:
    """Seen live on 28.09: the e-mail field was never found, LOGIN was never pressed, and a captcha stood in the way."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
    LOGIN = SOURCE.split("async def _indiegala_login", 1)[1].split("\n    async def ", 1)[0]

    def test_fields_without_a_name_are_found_beside_the_password(self):
        from src.stores.gamerpower import IG_MARK_FIELDS_JS
        assert 'input[type="password"]' in IG_MARK_FIELDS_JS and "data-fgc-mail" in IG_MARK_FIELDS_JS

    def test_it_is_typed_like_a_person_would(self):
        assert "send_keys(value)" in self.LOGIN
        assert "setter.call" not in self.LOGIN

    def test_login_is_pressed_beside_the_password_not_the_first_submit_on_the_page(self):
        from src.stores.gamerpower import IG_SUBMIT_JS
        assert "data-fgc-pass" in IG_SUBMIT_JS and "log ?in" in IG_SUBMIT_JS

    def test_the_captcha_goes_to_you_before_login_is_pressed(self):
        assert self.LOGIN.index("_ig_captcha_unsolved()") < self.LOGIN.index("IG_SUBMIT_JS")
        assert "present_fn=self._ig_captcha_unsolved" in self.LOGIN

    @pytest.mark.parametrize("url,expected", [
        ("https://freebies.indiegala.com/best-plumber", "best-plumber"),
        ("https://freebies.indiegala.com/Best-Plumber/", "best-plumber"),
        ("https://www.gamerpower.com/open/best-plumber-pc-giveaway", ""),
        ("https://freebies.indiegala.com.evil.tld/best-plumber", ""),
        ("", ""),
    ])
    def test_the_database_key_is_indiegalas_own_slug(self, url, expected):
        from src.stores.gamerpower import indiegala_game_id
        assert indiegala_game_id(url) == expected

    def test_a_ticked_box_counts_as_solved(self):
        from src.stores.gamerpower import IG_CAPTCHA_UNSOLVED_JS
        assert "g-recaptcha-response" in IG_CAPTCHA_UNSOLVED_JS and "answer.value" in IG_CAPTCHA_UNSOLVED_JS
        assert chr(8) not in IG_CAPTCHA_UNSOLVED_JS


class TestIndieGalaClaim:
    """Seen live on 28.09: every signed-in page says "Search in your library", so every giveaway passed as owned."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "gamerpower.py").read_text(encoding="utf-8")
    CLAIM = SOURCE.split("async def _claim_indiegala_game", 1)[1].split("\nasync def ", 1)[0]

    def test_ownership_is_not_read_from_a_hint_every_page_carries(self):
        from src.stores.gamerpower import IG_OWNED_JS
        assert "just go to your library" in IG_OWNED_JS
        assert '"in your library" in body_text' not in self.CLAIM

    def test_ownership_is_judged_after_signing_in(self):
        assert self.CLAIM.index("_ig_logged_in()") < self.CLAIM.index("_ig_owns_this()")

    def test_a_dry_run_writes_nothing(self):
        owned = self.CLAIM.split("if await self._ig_owns_this():", 1)[1].split("return", 1)[0]
        assert "if not cfg.dryrun:" in owned
        assert self.CLAIM.index("if cfg.dryrun:") < self.CLAIM.index("IG_MARK_CLAIM_JS")

    def test_a_click_alone_is_not_a_claim(self):
        assert "claimed = True" not in self.CLAIM
        after = self.CLAIM.split("await button.click()", 1)[1]
        assert "await self.page.get(url)" in after and "_ig_owns_this()" in after
        assert '"failed:unconfirmed"' in after

    def test_a_captcha_during_the_claim_goes_to_you(self):
        assert '_clear_challenge("IndieGala")' in self.CLAIM


class TestFanaticalAccount:
    """Read live 4.10: the orders API answers with the site's own token, the key comes from /user/orders/redeem."""


    ORDERS = [
        {"_id": "o1", "status": "COMPLETE",
         "items": [{"_id": "i1", "name": "Some Paid Game", "slug": "some-paid-game", "iid": 1}]},
        {"_id": "o2", "status": "COMPLETE",
         "items": [{"_id": "i2", "name": "Free Game: The Giveaway", "slug": "free-game-the-giveaway",
                    "serialId": 7, "iid": 2, "drm": {"steam": True, "epic": False}}]},
    ]

    def test_the_giveaway_item_is_found_by_its_slug(self):
        found = find_fanatical_item(self.ORDERS, "free-game-the-giveaway", "anything")
        assert found["oid"] == "o2" and found["item"]["_id"] == "i2" and found["status"] == "COMPLETE"

    def test_a_top_level_item_reveals_without_a_bundle_id(self):
        assert find_fanatical_item(self.ORDERS, "free-game-the-giveaway", "x")["bid"] is None

    def test_an_unfinished_checkout_is_found_but_not_complete(self):
        # /user/orders also lists INITIALISED orders, the cart that never got checked out (#71).
        orders = [{"_id": "o9", "status": "INITIALISED", "items": [dict(self.ORDERS[1]["items"][0])]}]
        assert find_fanatical_item(orders, "free-game-the-giveaway", "x")["status"] == "INITIALISED"

    def test_a_complete_order_wins_over_an_earlier_unfinished_one(self):
        orders = [{"_id": "o9", "status": "INITIALISED", "items": [dict(self.ORDERS[1]["items"][0])]}] + self.ORDERS
        assert find_fanatical_item(orders, "free-game-the-giveaway", "x")["oid"] == "o2"

    def test_or_by_its_name_when_the_slug_differs(self):
        found = find_fanatical_item(self.ORDERS, "other-slug", "Free Game - The Giveaway!")
        assert found["oid"] == "o2"

    @pytest.mark.parametrize("orders", [[], None, "nope", [{"items": None}], [{"_id": "x", "items": ["bad"]}]])
    def test_nothing_is_found_in_an_empty_or_odd_answer(self, orders):
        assert find_fanatical_item(orders, "free-game-the-giveaway", "Free Game") is None

    def test_another_order_is_never_taken_for_the_giveaway(self):
        assert find_fanatical_item(self.ORDERS, "missing-slug", "Missing Game") is None

    @pytest.mark.parametrize("value,key", [
        ({"key": "abcde-fghij-klmno"}, "ABCDE-FGHIJ-KLMNO"),
        ([{"x": {"key": "AAAAA-BBBBB-CCCCC"}}], "AAAAA-BBBBB-CCCCC"),
        ({"key": "error"}, ""),
        ({"key": "AAAAA-BBBBB-CCCCC-DDDDD"}, ""),
        ({"key": "ABCDEFGHIJKLMNOPQR"}, ""),
        (None, ""),
    ])
    def test_only_a_steam_shaped_key_is_taken(self, value, key):
        assert steam_key_in(value) == key

    @pytest.mark.parametrize("item,steam", [
        ({"drm": ["steam"]}, True),
        ({"name": "A game"}, True),
        ({"drm": ["epicgames"]}, False),
        ({"drm": ["gog"]}, False),
        # Order items name every platform with true or false; "steam": false once counted as Steam (#71).
        ({"drm": {"steam": False, "epic": True}}, False),
        ({"drm": {"steam": True, "gog": False}}, True),
    ])
    def test_a_key_goes_to_steam_only_when_nothing_says_otherwise(self, item, steam):
        assert fanatical_item_is_steam(item) is steam

    def test_every_call_uses_the_sites_own_token_and_headers(self):
        # The site's api client sends these on every request (api/index.js generateHeaders).
        for js in (FAN_ORDERS_JS, FAN_ORDER_JS, FAN_REVEAL_JS, FAN_UNSUBSCRIBE_JS, FAN_NEWSLETTER_JS):
            assert "localStorage.getItem('bsauth')" in js and "authorization: auth.token" in js
            for header in ("h.anonid", "'x-fan-fp'", "'X-Fan-Client-Version'"):
                assert header in js
        assert "/api/user/orders/redeem" in FAN_REVEAL_JS and "atok" in FAN_REVEAL_JS

    def test_a_key_is_revealed_only_after_the_bots_own_claim(self):
        block = TestFanaticalClaimHonesty.BLOCK
        owned = block.split("if owned:", 1)[1].split("if cfg.dryrun:", 1)[0]
        assert "_fanatical_reveal_key" not in owned
        assert block.index("_fanatical_reveal_key") > block.index("_fanatical_complete_order(")

    def test_the_order_list_alone_is_found_by_name(self):
        # Read live 8.10: /user/orders lists items as {name, bundles} only, no slug, ids or platform.
        orders = [{"_id": "o3", "status": "COMPLETE", "items": [{"name": "Spooky Cats", "bundles": []}]}]
        assert find_fanatical_item(orders, "spooky-cats", "Spooky Cats")["oid"] == "o3"

    def test_the_reveal_and_the_platform_come_from_the_order_itself(self):
        # Revealing with the list item sent no ids and got a 400 (#71); /user/orders/<id> has them.
        source = TestFanaticalClaimHonesty.SOURCE
        complete = source.split("async def _fanatical_complete_order", 1)[1].split("\n    async def ", 1)[0]
        assert "_fanatical_order_item(" in complete
        assert "FAN_ORDER_JS" in source.split("async def _fanatical_order_item", 1)[1].split("\n    async def ", 1)[0]

    def test_the_bots_own_order_takes_its_only_item_whatever_the_name(self):
        order = {"_id": "o4", "status": "COMPLETE", "items": [{"_id": "i4", "name": "Spooky Cats: Deluxe"}]}
        assert find_fanatical_item([order], "other-slug", "Spooky Cats") is None
        assert find_fanatical_item([order], "other-slug", "Spooky Cats", sole_item=True)["oid"] == "o4"
        order["items"].append({"_id": "i5", "name": "Something Else"})
        assert find_fanatical_item([order], "other-slug", "Spooky Cats", sole_item=True) is None

    def test_the_reveal_sends_atok_the_way_the_site_does(self):
        # The site's store holds the raw "bsatok" string, so the whole string goes, empty when missing.
        assert "const atok = localStorage.getItem('bsatok') || '';" in FAN_REVEAL_JS

    def test_fanaticals_error_text_is_masked_in_the_log(self):
        # Release review: the reveal's error body may quote the account e-mail.
        source = TestFanaticalClaimHonesty.SOURCE
        reveal = source.split("async def _fanatical_reveal_key", 1)[1].split("\n    async def ", 1)[0]
        assert r'r"\1***@", detail)' in reveal and "detail[:120]" in reveal

    def test_existed_never_overwrites_a_row(self):
        # Release review: a later "existed" wiped Steam's outcome for the key (claimed and activated, failed:key-*).
        source = TestFanaticalClaimHonesty.SOURCE
        remember = source.split("async def _remember_fanatical", 1)[1].split("\n    async def ", 1)[0]
        assert 'if created or status != "existed":' in remember

    @pytest.mark.parametrize("store,status,done", [
        ("epic", "claimed", True),
        ("epic", "existed", True),
        ("fanatical", "claimed and activated", True),
        ("fanatical", "failed:key-region", True),
        ("fanatical", "failed:missing_base", True),
        ("epic", "failed:unconfirmed", False),
        ("itchio", "not-free", False),
        ("steam", "", False),
    ])
    def test_a_game_steam_took_the_key_for_is_not_chased_again(self, store, status, done):
        # Release review: "claimed and activated" was not counted, so the giveaway was routed again every run.
        assert counts_as_done(store, status) is done


class TestFanaticalCheckout:
    """A giveaway is a free order: cart, checkout, receipt. The bot never pays and never takes the upsell."""

    SOURCE = TestFanaticalClaimHonesty.SOURCE
    BLOCK = TestFanaticalClaimHonesty.BLOCK
    CHECKOUT = SOURCE.split("async def _fanatical_checkout", 1)[1].split("\n    async def ", 1)[0]
    STEPS = SOURCE.split("async def _fanatical_finish_steps", 1)[1].split("\n    async def ", 1)[0]

    @pytest.mark.parametrize("text,price", [
        ("€0.00", 0.0), ("$0.00", 0.0), ("0,00 zł", 0.0), ("£4.00", 4.0),
        ("$1,299.99", 1299.99), ("1.299,99 €", 1299.99), ("¥1,299", 1299.0),
        ("", None), (None, None), ("Free", None),
    ])
    def test_prices_are_read_in_every_format(self, text, price):
        assert fanatical_price(text) == price

    @pytest.mark.parametrize("path,page,expected", [
        ("/en/cart", "cart", True),
        ("/en/cart?upsell=true", "cart", True),
        ("/en/game/cartel-tycoon", "cart", False),
        ("/en/receipt?authResult=AUTHORISED", "receipt", True),
        ("/en/game/receipt-of-doom", "receipt", False),
        ("", "cart", False),
        ("https://www.fanatical.com/en/receipt?authResult=AUTHORISED", "receipt", True),
        ("https://www.fanatical.com/en/cart?upsell=true", "cart", True),
        # A receipt on any other host proves nothing.
        ("https://evil.example/en/receipt?authResult=AUTHORISED", "receipt", False),
        ("https://www.fanatical.com.evil.example/en/cart", "cart", False),
    ])
    def test_the_cart_is_told_apart_from_a_game_named_like_it(self, path, page, expected):
        assert fanatical_page(path, page) is expected

    @pytest.mark.parametrize("path,order", [
        ("/en/receipt?utm_nooverride=1&authResult=AUTHORISED&merchantReference=6a0b1c2d3e4f5a6b7c8d9e0f", "6a0b1c2d3e4f5a6b7c8d9e0f"),
        ("/en/receipt?authResult=AUTHORISED&merchantReference=", ""),
        ("/en/receipt?merchantReference=../user", ""),
        ("/en/cart", ""),
    ])
    def test_the_receipt_names_the_order(self, path, order):
        assert fanatical_receipt_order(path) == order

    def test_nothing_is_clicked_before_a_free_total_is_seen(self):
        gate = self.CHECKOUT.index("if not free_seen or paid:")
        assert gate < self.CHECKOUT.index("FAN_MARK_PAY_JS") and gate < self.CHECKOUT.index("FAN_MARK_PROCEED_JS")
        assert 'return "not-free", ""' in self.CHECKOUT
        # A total that cannot be read is not free either.
        assert "if total is None:" in self.CHECKOUT

    def test_only_the_checkout_button_is_pressed_on_the_upsell(self):
        # The upsell page offers "4 Mystery Games" with an ADD button; only #api-button goes on.
        assert "button#api-button" in FAN_MARK_PAY_JS and "ADD" not in FAN_MARK_PAY_JS

    def test_only_the_giveaways_own_button_is_pressed(self):
        # ProductAddToCartButton also sits on every paid add-to-cart on the page.
        assert ".GiveawaySteps__steps__action button" in FAN_MARK_MAIN_JS
        assert "ProductAddToCartButton" not in FAN_MARK_MAIN_JS

    def test_a_claim_that_never_reaches_the_cart_does_not_ask_you(self):
        # A refused claim used to wait minutes and then tell you the game was in your cart.
        assert 'if not await self._fanatical_click(FAN_MARK_MAIN_JS, "Claim button"):' in self.BLOCK
        no_cart = self.BLOCK.split('if outcome in ("not-free", "no-cart"):', 1)[1].split("return", 1)[0]
        assert "_wait_for_vnc_login" not in no_cart
        assert self.BLOCK.index('if outcome in ("not-free", "no-cart"):') < self.BLOCK.index('if outcome == "stuck":')

    def test_steps_are_read_from_the_markup(self):
        assert "iconContainer--" in FAN_GIVEAWAY_JS and ".done" in FAN_GIVEAWAY_JS and "sold-out" in FAN_GIVEAWAY_JS

    def test_the_newsletter_is_ticked_once_and_the_rest_is_left_to_you(self):
        # The site's button toggles the consent, a second click would take it back.
        assert 'if action == "newsletter":' in self.STEPS and "if not ticked:" in self.STEPS
        assert "_wait_for_vnc_login(" in self.STEPS

    def test_steam_is_connected_beforehand_not_over_vnc(self):
        # The README asks for Steam to be connected on Fanatical first, so the bot reports it instead of waiting.
        steam = self.STEPS.split('if action == "steam":', 1)[1].split('return "steam"', 1)[0]
        assert "_wait_for_vnc_login" not in steam and "FAN_LINKED_ACCOUNTS_URL" in steam
        assert self.STEPS.index('return "steam"') < self.STEPS.index("_wait_for_vnc_login(")

    @pytest.mark.parametrize("steps,expected", [
        ([], ("wait", -1)),
        ([{"type": "signin", "done": True}, {"type": "newsletter", "done": True}], ("ready", -1)),
        ([{"type": "signin", "done": True}, {"type": "newsletter", "done": False}], ("newsletter", 1)),
        ([{"type": "signin", "done": True}, {"type": "wishlistOnSteam", "done": False}], ("human", 1)),
        # Release review: Steam unlinked behind another step still blocks at once, before anything is ticked or asked.
        ([{"type": "signin", "done": True}, {"type": "wishlistOnSteam", "done": False},
          {"type": "steamConnect", "done": False}], ("steam", 2)),
        ([{"type": "newsletter", "done": False}, {"type": "steamConnect", "done": False}], ("steam", 1)),
        ([{"type": "steamConnect", "done": True}, {"type": "newsletter", "done": False}], ("newsletter", 1)),
    ])
    def test_the_next_step_is_decided_in_one_place(self, steps, expected):
        assert fanatical_next_step(steps) == expected

    def test_over_vnc_the_bot_waits_for_the_step_it_asked_for(self):
        # Waiting for every step would never end while a step the bot cannot do is still open.
        assert "asked_step_done" in self.STEPS and "_wait_for_vnc_login(asked_step_done" in self.STEPS
        assert '"failed:steam-not-linked"' in self.BLOCK.split('if steps == "steam":', 1)[1].split("return", 1)[0]

    def test_a_sold_out_giveaway_is_no_news(self):
        sold = self.BLOCK.split('if steps == "sold-out":', 1)[1].split("return", 1)[0]
        assert "self.notify_games.remove(notify_game)" in sold


class TestFanaticalNewsletter:
    """Unless FANATICAL_NEWSLETTER=true, only a subscription the bot's own claim made is taken back."""

    BLOCK = TestFanaticalClaimHonesty.BLOCK

    def test_the_calls_are_the_sites_own(self):
        assert "/api/crm/frontunsubscribe" in FAN_UNSUBSCRIBE_JS and "method: 'POST'" in FAN_UNSUBSCRIBE_JS
        # The account read is the one the site makes on every page load.
        assert "/api/user/refresh-auth" in FAN_NEWSLETTER_JS and "email_newsletter_pending" in FAN_NEWSLETTER_JS

    @pytest.mark.parametrize("before,after,expected", [
        (False, True, True),
        (True, True, False),     # you had it before the claim
        (False, False, False),   # the claim signed you up for nothing
        (None, True, False),     # unknown before: leave it alone
        (False, None, False),    # unknown after: leave it alone
    ])
    def test_it_unsubscribes_only_what_the_claim_added(self, before, after, expected):
        assert fanatical_should_unsubscribe(before, after) is expected

    def test_the_account_is_read_before_any_step_and_after_the_claim(self):
        # Read live 8.10: an old consent stays in the browser, so a step is no proof; the account before and after is.
        assert self.BLOCK.index("had_newsletter = None if cfg.fanatical_newsletter else") < self.BLOCK.index(
            "_fanatical_finish_steps(")
        # Release review: a claim that reached the checkout signs you up even when it is not confirmed after.
        after = self.BLOCK.split("was not confirmed as claimed", 1)[1]
        assert "fanatical_should_unsubscribe(" in after and "_fanatical_unsubscribe(" in after
        assert self.BLOCK.index("_fanatical_reveal_key(") < self.BLOCK.index("_fanatical_unsubscribe(")

    def test_the_decision_is_in_the_debug_log_and_a_blind_spot_is_a_warning(self):
        assert "Newsletter for '%s': before %s, after %s" in self.BLOCK
        assert "Could not check your newsletter after" in self.BLOCK

    def test_no_call_goes_out_without_the_sites_token(self):
        # The site's own client sends no Authorization header at all without a token.
        for js in (FAN_ORDERS_JS, FAN_ORDER_JS, FAN_REVEAL_JS, FAN_UNSUBSCRIBE_JS, FAN_NEWSLETTER_JS):
            assert js.index("if (!auth.token) return") < js.index("fetch(")

    def test_it_works_like_gog_newsletter(self):
        # Release review: false (the default) unsubscribes, true keeps it, the same way as GOG_NEWSLETTER.
        source = (Path(__file__).resolve().parent.parent / "src" / "core" / "config.py").read_text(encoding="utf-8")
        assert 'fanatical_newsletter: bool = _bool("FANATICAL_NEWSLETTER")' in source
        assert "had_newsletter = None if cfg.fanatical_newsletter else" in self.BLOCK
        assert "if not cfg.fanatical_newsletter:" in self.BLOCK


class TestFanaticalOutcomes:
    """How each end of the flow is reported."""

    BLOCK = TestFanaticalClaimHonesty.BLOCK

    def test_a_receipt_counts_when_the_account_cannot_confirm_it_but_only_an_authorised_one(self):
        assert 'if found or (outcome == "receipt" and orders is None and authorised):' in self.BLOCK

    @pytest.mark.parametrize("path,ok", [
        ("/en/receipt?utm_nooverride=1&authResult=AUTHORISED&merchantReference=6a0b1c2d3e4f5a6b7c8d9e0f", True),
        ("/en/receipt?authResult=authorised", True),
        ("/en/receipt?authResult=REFUSED&merchantReference=6a0b1c2d3e4f5a6b7c8d9e0f", False),
        ("/en/receipt?authResult=CANCELLED", False),
        ("/en/receipt?merchantReference=6a0b1c2d3e4f5a6b7c8d9e0f", False),
        ("", False),
    ])
    def test_only_a_receipt_that_says_authorised_counts_alone(self, path, ok):
        # Fanatical shows a receipt page for refused and cancelled orders too (redux/ducks/checkout.js).
        assert fanatical_receipt_authorised(path) is ok

    def test_a_complete_order_from_the_list_is_kept_when_its_own_page_will_not_load(self):
        source = TestFanaticalClaimHonesty.SOURCE
        complete = source.split("async def _fanatical_complete_order", 1)[1].split("\n    async def ", 1)[0]
        assert "if listed_complete:" in complete and "if misses >= 2:" in complete

    def test_only_an_item_with_its_ids_is_revealed(self):
        assert 'bool(found["item"].get("_id"))' in self.BLOCK

    def test_a_page_without_steps_is_its_own_failure(self):
        assert 'if steps == "no-steps":' in self.BLOCK and '"failed:no-steps"' in self.BLOCK

    def test_a_dry_run_knows_a_sold_out_giveaway(self):
        dry = self.BLOCK.split("if cfg.dryrun:", 1)[1].split("return", 2)
        assert 'giveaway["soldOut"]' in dry[0] and "self.notify_games.remove(notify_game)" in dry[0]

    def test_a_checkout_you_finish_is_checked_against_the_account(self):
        assert 'elif outcome == "stuck" and orders is not None:' in self.BLOCK
