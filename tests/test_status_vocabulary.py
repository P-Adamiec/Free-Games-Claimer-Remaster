"""One vocabulary for what a store reports, so the summary reads every store the same way.

main.py shows or hides an entry by the words in its status, so a store that writes free text
either slips past that filter or vanishes from it. The stores below follow the list; the older
ones (Epic, GOG, Prime, Steam, GamerPower) predate it and are not held to it yet.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = re.compile(r"^(claimed|existed|notified|available \(dry run\)|(failed|skipped)(:[a-z-]+)?)$")
# Store module and the name its VNC prompts start with.
STORES = {"ubisoft": "Ubisoft", "unity": "Unity", "epic_fab": "Fab", "microsoft": "Microsoft"}


def _strings(expr) -> set:
    """The strings an expression can produce, not the ones it only compares against."""
    if isinstance(expr, ast.IfExp):
        return _strings(expr.body) | _strings(expr.orelse)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.JoinedStr):
        return {"".join(v.value if isinstance(v, ast.Constant) else "{}" for v in expr.values)}
    return set()


def _statuses(module: str) -> set:
    """Every string a store can put into a notification status, read from its source."""
    tree = ast.parse((ROOT / "src" / "stores" / f"{module}.py").read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "status":
                    found |= _strings(value)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "status" for t in node.targets):
            found |= _strings(node.value)
    return found


@pytest.mark.parametrize("module", sorted(STORES))
def test_every_status_follows_the_shared_vocabulary(module):
    statuses = _statuses(module)
    assert statuses, "no statuses found, the scan stopped matching"
    unexpected = sorted(s for s in statuses if not ALLOWED.match(s))
    assert not unexpected, f"{module} reports outside the shared vocabulary: {unexpected}"


@pytest.mark.parametrize("module,label", sorted(STORES.items()))
def test_vnc_prompts_name_the_store(module, label):
    source = (ROOT / "src" / "stores" / f"{module}.py").read_text(encoding="utf-8")
    titles = re.findall(r'self\._vnc_notice\(\s*"([^"]+)"', source)
    assert all(t.startswith(f"{label}: ") for t in titles), titles


def test_notified_reaches_the_summary_by_default():
    # The one status meant for "found it, take it yourself" must survive the default filter.
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    hidden = re.findall(r'"([a-z_]+)" not in g\["status"\]\.lower\(\)', main)
    assert hidden, "the summary filter changed shape, re-read main.py"
    assert not any(word in "notified" for word in hidden), hidden
