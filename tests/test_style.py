"""Source-level checks for mistakes that only show up at runtime.

A function annotated `-> bool` that hits a bare `return` hands the caller `None`,
which is falsy. That is how a successful Epic login was reported as a failure
(issue #29): the caller does `if not await self._ensure_logged_in():`.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "src").rglob("*.py")) + [ROOT / "main.py"]


def _bool_functions():
    """Every function in the project annotated as returning bool."""
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if getattr(node.returns, "id", None) == "bool":
                yield path.relative_to(ROOT), node


def test_the_project_has_bool_functions_to_check():
    # Guards against the scan silently matching nothing after a refactor.
    assert len(list(_bool_functions())) > 5


@pytest.mark.parametrize("path,node", list(_bool_functions()), ids=lambda v: getattr(v, "name", str(v)))
def test_bool_function_never_returns_none(path, node):
    bare = [n.lineno for n in ast.walk(node) if isinstance(n, ast.Return) and n.value is None]
    assert not bare, (
        f"{path}:{node.lineno} {node.name}() is annotated -> bool but has a bare return "
        f"at line(s) {bare}, which yields None and reads as False"
    )
