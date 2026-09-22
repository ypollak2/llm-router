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
import pathlib
import textwrap


def _tree(obj) -> ast.AST:
    """Accepts a module/function/class, an already-parsed AST, or source text.

    The AST case matters for files that are not importable as modules — hook
    scripts, and any file read by path rather than imported.
    """
    if isinstance(obj, ast.AST):
        return obj
    if isinstance(obj, str):
        return ast.parse(textwrap.dedent(obj))
    if isinstance(obj, pathlib.Path):
        return ast.parse(obj.read_text(encoding="utf-8"))
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


# ── String literals (R13, the largest conversion category) ──────────────────
#
# Many source-text assertions are about the content of a STRING in the code —
# an SQL fragment, a log event name, an env var. `"date(timestamp,'localtime')"
# in getsource(mod)` is satisfied by that phrase appearing in a comment, a
# docstring, or a test's own explanation pasted into the module. That is the
# A-10 evasion exactly.
#
# Pulling the string CONSTANTS out of the AST is strictly stronger: comments
# are not in the AST at all, and docstrings are excluded explicitly below. A
# match therefore means the text is in a value the program actually uses.


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings, so they can be excluded.

    A docstring is real source text, but it is DOCUMENTATION — asserting on it
    is the same mistake one layer in, and a module docstring quoting the SQL it
    used to contain would satisfy an assertion about the SQL.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def string_constants(obj) -> list[str]:
    """Every string literal the code USES, docstrings excluded.

    Includes the pieces of f-strings, so an f-string that builds SQL is
    searchable the same way a plain one is.
    """
    tree = _tree(obj)
    skip = _docstring_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                out.append(node.value)
    return out


def assert_in_strings(obj, needle: str, *, msg: str = "") -> None:
    """`needle` appears in a string literal the code uses.

    Replaces `assert needle in inspect.getsource(obj)`. A comment or docstring
    containing `needle` does NOT satisfy this.
    """
    if not any(needle in s for s in string_constants(obj)):
        raise AssertionError(
            msg or f"no string literal in {getattr(obj, '__name__', obj)!r} "
                   f"contains {needle!r} (comments and docstrings do not count)"
        )


def assert_not_in_strings(obj, needle: str, *, msg: str = "") -> None:
    """`needle` appears in NO string literal the code uses.

    Stricter than the source-text form it replaces: `needle not in getsource()`
    could be satisfied by deleting a comment, while this requires the value to
    actually be gone.
    """
    hits = [s for s in string_constants(obj) if needle in s]
    if hits:
        raise AssertionError(
            msg or f"{needle!r} still appears in a string literal: {hits[:3]!r}"
        )


def attributes_in(obj) -> list[str]:
    """Every attribute access, as written (`s.expected_value`, `self.x.y`)."""
    return sorted({
        _norm(n) for n in ast.walk(_tree(obj)) if isinstance(n, ast.Attribute)
    })


def assert_reads_attribute(obj, needle: str, *, msg: str = "") -> None:
    if not any(needle in a for a in attributes_in(obj)):
        raise AssertionError(
            msg or f"{getattr(obj, '__name__', obj)!r} does not read {needle!r}"
        )


def assert_has_exact_string(obj, value: str, *, msg: str = "") -> None:
    """A string literal EQUAL to `value`, not merely containing it.

    Found the hard way: `assert_in_strings(team, "provenance")` was satisfied
    by the unrelated literal `"provenance_detail"`, so deleting the real
    `"provenance"` key left the test green. Substring matching is right for a
    SQL FRAGMENT — you want `"date(timestamp,'localtime')"` to match inside a
    longer query — and wrong for a DICT KEY or an event name, where a longer
    string that happens to contain it is a different value entirely.

    Rule of thumb: matching part of a larger statement -> `assert_in_strings`;
    matching a whole identifier-like value -> this.
    """
    if value not in string_constants(obj):
        near = [s for s in string_constants(obj) if value in s][:3]
        raise AssertionError(
            msg or (
                f"no string literal equals {value!r}"
                + (f" (nearest: {near!r} — these CONTAIN it, which is not the "
                   "same thing)" if near else "")
            )
        )
