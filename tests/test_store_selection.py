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

EXPECTED_DEFAULT = ["steam", "epic", "fab", "prime", "gog", "ubisoft", "aliexpress", "gamerpower"]


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

    def test_gamerpower_runs_last(self):
        # Its dedup reads the database, so the stores with their own module claim first.
        assert _default_stores()[-1] == "gamerpower"

    def test_the_hardcoded_list_is_gone(self):
        # The old literal lived inside _get_active_claimers and drifted from the registry.
        assert '["steam", "epic", "prime", "gog", "aliexpress"]' not in SOURCE
