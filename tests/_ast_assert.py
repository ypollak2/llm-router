"""AST assertions for call sites — R13.

WHY THIS EXISTS
---------------
Tests added on 2026-09-22 carried this in their own docstrings:

    THIS FILE ASSERTS THE CALL SITE, NOT THE DEFINITION (Rule B).

They did not. They checked whether a string appeared anywhere in a 5,300-line
module. The audit reproduced the evasion: break the real call site, leave the
pinned string alive as a comment, and **23 tests passed** on code where the
bandit was once again rewarded for answers the router had rejected.

The earlier red-check reverted whole files, which removes the string too — so
it went red and the test looked sound. A whole-file revert cannot distinguish
a call-site assertion from a substring scan.

WHAT THIS PROVIDES
------------------
Assertions over the parsed AST. Comments and strings are not in the AST, so a
pinned phrase in a comment cannot satisfy them. Prefer a behavioural test where
one exists; use these where the property is genuinely structural.
"""

from __future__ import annotations

import ast
import inspect
import textwrap


def _tree(obj) -> ast.AST:
    src = textwrap.dedent(inspect.getsource(obj))
    return ast.parse(src)


def _norm(node: ast.AST) -> str:
    """Source-like text for a node, with formatting and comments removed."""
    return ast.unparse(node)


def calls_in(obj) -> list[str]:
    """Every call expression in *obj*, normalised. Comments cannot appear here."""
    return [_norm(n) for n in ast.walk(_tree(obj)) if isinstance(n, ast.Call)]


def assert_calls(obj, needle: str, *, msg: str = "") -> None:
    """Fail unless *obj* really contains a call matching *needle*.

    `needle` is matched against `ast.unparse` output, so whitespace and comment
    style are irrelevant and a commented-out call cannot satisfy it.
    """
    found = calls_in(obj)
    assert any(needle in c for c in found), (
        msg or f"no call matching {needle!r} in {getattr(obj, '__name__', obj)}"
    ) + f"\n  calls present: {found[:12]}"


def assert_not_calls(obj, needle: str, *, msg: str = "") -> None:
    found = calls_in(obj)
    bad = [c for c in found if needle in c]
    assert not bad, (msg or f"unexpected call matching {needle!r}") + f"\n  {bad}"


def keyword_args(obj, func_needle: str) -> list[set[str]]:
    """The keyword names passed at each call matching *func_needle*."""
    out = []
    for n in ast.walk(_tree(obj)):
        if isinstance(n, ast.Call) and func_needle in _norm(n.func):
            out.append({k.arg for k in n.keywords if k.arg})
    return out


def assert_passes_kwarg(obj, func_needle: str, kwarg: str, *, msg: str = "") -> None:
    """Fail unless every call to *func_needle* inside *obj* passes *kwarg*."""
    sites = keyword_args(obj, func_needle)
    assert sites, f"no call to {func_needle!r} found in {getattr(obj, '__name__', obj)}"
    missing = [s for s in sites if kwarg not in s]
    assert not missing, (
        msg or f"a call to {func_needle!r} does not pass {kwarg!r}"
    ) + f"\n  call-site kwargs: {sites}"


def assert_guarded_by(obj, attr: str, *, msg: str = "") -> None:
    """Fail unless *obj* branches on *attr* somewhere in real code.

    Matches an actual attribute access or a `getattr(..., "attr", ...)` in the
    AST — not the identifier appearing in prose.
    """
    tree = _tree(obj)
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == attr:
            return
        if isinstance(n, ast.Call) and _norm(n.func) == "getattr":
            if len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value == attr:
                return
    raise AssertionError(
        (msg or f"{getattr(obj, '__name__', obj)} never reads {attr!r}")
        + " (searched the AST, so a mention in a comment does not count)"
    )
