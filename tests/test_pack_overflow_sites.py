"""PLAN v16 P1.3-a: every `check_overflow(` call outside local_context_guard.py is
preceded by `pack.trim_to(...)` in the same function; exactly 6 sites exist.

Separate file because it scans source text with `ast` (the G-F mutation campaign
excludes such files by rule, see scripts/gf_excluded_tests.py).
"""
from __future__ import annotations

import ast
from pathlib import Path

from llm_router import pack_overflow

SRC = Path(pack_overflow.__file__).resolve().parent
EXPECTED_SITES = {
    "providers.py": 2, "proxy/backends.py": 1, "hooks/agent_loop.py": 1,
    "hooks/direct_executor.py": 1, "toolkit/adapters/ollama.py": 1,
}


def _sites(tree: ast.AST) -> list[tuple[ast.AST, ast.Call]]:
    """(enclosing function, call) for every `check_overflow(` call."""
    out = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "check_overflow":
                    out.append((fn, node))
    # a call nested in an inner function is reported against both; keep the innermost
    inner: dict[int, tuple[ast.AST, ast.Call]] = {}
    for fn, call in out:
        prev = inner.get(id(call))
        if prev is None or fn.lineno > prev[0].lineno:
            inner[id(call)] = (fn, call)
    return list(inner.values())


def _untrimmed(fn: ast.AST, call: ast.Call) -> bool:
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "trim_to" and getattr(node.func.value, "id", "") == "pack"
                and node.lineno < call.lineno):
            return False
    return True


def _scan() -> dict[str, list[bool]]:
    found: dict[str, list[bool]] = {}
    for path in SRC.rglob("*.py"):
        if path.name == "local_context_guard.py":
            continue
        text = path.read_text()
        if "check_overflow(" not in text:
            continue
        for fn, call in _sites(ast.parse(text)):
            found.setdefault(path.relative_to(SRC).as_posix(), []).append(_untrimmed(fn, call))
    return found


def test_ast_finds_exactly_the_six_sites_and_none_untrimmed():
    found = _scan()
    assert {k: len(v) for k, v in found.items()} == EXPECTED_SITES  # a new site fails here
    assert sum(len(v) for v in found.values()) == 6
    assert [k for k, v in found.items() if any(v)] == []


def test_ast_scanner_detects_an_untrimmed_site():
    bad = "def f(p):\n    check_overflow(p)\n"
    ok = "def f(p):\n    pack = P(p)\n    p = pack.trim_to()\n    check_overflow(p)\n"
    late = "def f(p):\n    check_overflow(p)\n    pack.trim_to()\n"
    assert [_untrimmed(*s) for s in _sites(ast.parse(bad))] == [True]
    assert [_untrimmed(*s) for s in _sites(ast.parse(ok))] == [False]
    assert [_untrimmed(*s) for s in _sites(ast.parse(late))] == [True]
