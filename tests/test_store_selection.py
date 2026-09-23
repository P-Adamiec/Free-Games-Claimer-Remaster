"""Which stores run when nothing is configured.

Read out of main.py's source, so the test cannot drift into checking a copy.
Fab and Ubisoft were both added to the registry but missed here, which left them
off by default; that is the mistake these tests exist to catch.
"""

import ast
import re
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
SOURCE = MAIN_PY.read_text(encoding="utf-8")

EXPECTED_DEFAULT = ["steam", "epic", "fab", "prime", "gog", "microsoft", "ubisoft", "aliexpress"]


def _default_stores() -> list[str]:
    match = re.search(r"^DEFAULT_STORES: list\[str\] = (\[[^\]]*\])", SOURCE, re.M)
    assert match, "DEFAULT_STORES not found in main.py"
    return ast.literal_eval(match.group(1))


def _registry_keys() -> list[str]:
    match = re.search(r"^ALL_CLAIMERS.*?=\s*\{(.*?)^\}", SOURCE, re.S | re.M)
    assert match, "ALL_CLAIMERS not found in main.py"
    return re.findall(r'"([a-z]+)":\s*\(', match.group(1))


class TestDefaultSelection:
    def test_default_list_is_exactly_this(self):
        assert _default_stores() == EXPECTED_DEFAULT

    def test_every_default_is_a_real_store(self):
        unknown = sorted(set(_default_stores()) - set(_registry_keys()))
        assert not unknown, f"DEFAULT_STORES names stores that do not exist: {unknown}"

    def test_new_stores_are_not_silently_left_out(self):
        # Anything in the registry is either a default or a deliberate opt-in.
        # Unity is opt-in until its checkout works, it can find an asset but not buy it.
        opt_in = {"unity"}
        missing = sorted(set(_registry_keys()) - set(_default_stores()) - opt_in)
        assert not missing, (
            f"{missing} exist in ALL_CLAIMERS but run neither by default nor as a known opt-in. "
            "Add them to DEFAULT_STORES or to this test's opt-in set."
        )

    @pytest.mark.parametrize("store", EXPECTED_DEFAULT)
    def test_each_expected_store_is_present(self, store):
        assert store in _default_stores()

    def test_epic_runs_before_fab(self):
        # Fab reuses Epic's session, so Epic signing in first saves it a login.
        order = _default_stores()
        assert order.index("epic") < order.index("fab")

    def test_gamerpower_is_not_a_store(self):
        # It finds giveaways and hands them to the store they belong to, it claims nothing itself.
        assert "gamerpower" not in _registry_keys()
        assert "gamerpower" not in _default_stores()

    def test_the_side_stores_are_selectable_but_never_default(self):
        # Each one needs an account on that site, so naming it is the opt-in.
        match = re.search(r'^SIDE_STORES.*?=\s*\((.*?)\)', SOURCE, re.S | re.M)
        assert match, "SIDE_STORES not found in main.py"
        sides = re.findall(r'"([a-z.]+)"', match.group(1))
        assert sides == ["itchio", "fanatical", "indiegala", "alienware"]
        assert not set(sides) & set(_default_stores())

    def test_every_side_store_has_a_name_you_can_type(self):
        aliases = re.search(r'^_ALIASES.*?=\s*\{(.*?)^\}', SOURCE, re.S | re.M)
        assert aliases
        for side in ("itchio", "fanatical", "indiegala", "alienware"):
            assert f'"{side}"' in aliases.group(1)

    def test_the_hardcoded_list_is_gone(self):
        # The old literal lived inside _get_active_claimers and drifted from the registry.
        assert '["steam", "epic", "prime", "gog", "aliexpress"]' not in SOURCE


class TestRunOrder:
    """GamerPower finds, the stores claim: the order that makes that work."""

    RUN = SOURCE.split("async def run_claimers", 1)[1].split("\nasync def ", 1)[0]

    def test_gamerpower_is_asked_before_any_store_runs(self):
        assert self.RUN.index("discover_giveaways()") < self.RUN.index("for key, name, func in claimers")

    def test_it_is_asked_only_when_this_run_can_use_the_answer(self):
        # STORES=prime has nothing GamerPower feeds, so it must not cost a single request.
        guard = self.RUN.split("routed: dict = {}", 1)[1][:200]
        assert "if sides or any(key in GP_TARGETS for key in selected)" in guard

    def test_the_big_stores_are_handed_their_own_finds(self):
        assert "await func(routed.get(key)) if key in GP_TARGETS else await func()" in self.RUN

    def test_the_side_stores_come_after_the_gog_codes(self):
        assert self.RUN.index("redeem_pending_codes") < self.RUN.index("claim_side_stores(routed)")

    def test_only_the_mapped_stores_take_the_finds(self):
        match = re.search(r'^GP_TARGETS.*?=\s*\((.*?)\)', SOURCE, re.S | re.M)
        assert match and re.findall(r'"([a-z]+)"', match.group(1)) == ["steam", "epic", "gog", "microsoft"]


class TestUnknownSitesStayOff:
    """Opening a site nobody mapped is not built yet, so the setting cannot switch it on."""

    def test_the_switch_does_not_reach_the_run(self, monkeypatch):
        from src.core.config import cfg
        from src.stores.gamerpower import GamerPowerClaimer

        monkeypatch.setattr(cfg, "gp_unknown_stores", True)
        assert GamerPowerClaimer._side_store_selected("unknown") is False

    def test_a_named_side_store_still_runs(self, monkeypatch):
        from src.core import selection
        from src.stores.gamerpower import GamerPowerClaimer

        selection.set_active_stores(["itchio"])
        try:
            assert GamerPowerClaimer._side_store_selected("itchio") is True
            assert GamerPowerClaimer._side_store_selected("fanatical") is False
        finally:
            selection.reset_active_stores()
