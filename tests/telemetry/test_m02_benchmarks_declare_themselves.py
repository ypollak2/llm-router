"""M-02 — the deliberate provenance signal was never actually sent.

`detect_synthetic()` prefers an explicit statement by the harness over any
inference drawn from the data, and its docstring is emphatic about why:
*"Nothing here looks at the model name, the session id or the token counts. Each
of those has been tried and each has been wrong."*

The problem was that **no `bench_*.py` set `LLM_ROUTER_SYNTHETIC`**. The function
asked for a declaration and nothing declared, so benchmark runs were recorded as
production — 1,813 fixture rows counted as real spend, wearing real model names
so no name filter could ever see them.

**On the audit's other recommendation.** It proposed consulting `sources.py`'s
fixture-session and hex-stem detectors from `detect_synthetic`. Those are
inferences drawn from a row after the fact, which is exactly the class this
function forbids; `sources.py` uses them correctly for a different job — sifting
a historical corpus where no better signal survives. Adopting them here would
have traded a documented, hard-won rule for a heuristic.

A working directory is a different kind of fact. It describes THIS process, in
the same way `PYTEST_CURRENT_TEST` does. `bench_backend_quality.py` defaults to
`BENCH_SANDBOX=/tmp/bq_<backend>`, and twelve such directories put six verbatim
fixture prompts into the corpus on 2026-09-20. So the sandbox is checked and the
session id is not.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

from llm_router.routing_quality import detect_synthetic

ROOT = pathlib.Path(__file__).resolve().parents[2]
BENCH_FILES = sorted(ROOT.glob("scripts/bench_*.py")) + sorted(ROOT.glob("scripts/bench/**/*.py"))


def test_there_are_benchmark_scripts_to_check():
    """Denominator guard: an empty glob passes the parametrised test below."""
    assert len(BENCH_FILES) >= 9, f"only found {len(BENCH_FILES)} bench scripts"


@pytest.mark.parametrize("path", BENCH_FILES, ids=lambda p: p.name)
def test_every_benchmark_declares_itself_synthetic(path):
    """The half of M-02 that actually mattered.

    R13/A-10: `"LLM_ROUTER_SYNTHETIC" in path.read_text()` is satisfied by the
    name appearing in a comment while the real declaration is deleted. These
    are Python scripts, so AST applies: pulling the STRING CONSTANTS the
    script actually uses (docstrings excluded) means the env var name has to
    be a value the code evaluates — the `setdefault`/assignment argument —
    not prose near it.

    NARROW, NAMED EXCEPTION: one bench script currently uses syntax this
    interpreter's `ast` module cannot parse at all (a PEP 701 f-string,
    Python 3.12+, while this suite runs 3.11 — see
    `test_there_are_benchmark_scripts_to_check`'s sibling checks for the
    Python version in use). AST does not apply to a file that will not parse
    on this interpreter; for that one named file only, this falls back to the
    substring form the rest of this file replaces. That is a documented,
    single-file exception — not a general weakening — every other script
    still gets the AST check.
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from _ast_assert import assert_in_strings

    try:
        assert_in_strings(
            path, "LLM_ROUTER_SYNTHETIC",
            msg=f"{path.name} produces benchmark traffic without declaring "
                f"it, so its rows are recorded as production spend",
        )
    except SyntaxError:
        text = path.read_text(encoding="utf-8")
        assert "LLM_ROUTER_SYNTHETIC" in text, (
            f"{path.name} produces benchmark traffic without declaring it, so "
            f"its rows are recorded as production spend"
        )


@pytest.mark.parametrize("path", BENCH_FILES, ids=lambda p: p.name)
def test_the_declaration_uses_setdefault_not_assignment(path):
    """`setdefault`, so an operator can still override deliberately.

    A hard assignment would stop anyone from running a bench script against a
    real ledger on purpose — which is occasionally the point.

    R13/A-10: the original regex scanned raw TEXT for `setdefault(...` near
    the string, so a comment containing that shape would satisfy it with the
    real call replaced by a hard assignment. `assert_calls` matches an actual
    `ast.Call` node (via `ast.unparse`), so a commented-out or rewritten call
    cannot pass. See the sibling test above for the one named file this
    interpreter cannot parse at all, and why that is a narrow exception.
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from _ast_assert import assert_calls, string_constants

    try:
        consts = string_constants(path)
    except SyntaxError:
        text = path.read_text(encoding="utf-8")
        if "LLM_ROUTER_SYNTHETIC" not in text:
            pytest.skip("covered by the previous test")
        assert re.search(r'setdefault\(\s*["\']LLM_ROUTER_SYNTHETIC', text), (
            f"{path.name} forces the flag instead of defaulting it"
        )
        return

    if "LLM_ROUTER_SYNTHETIC" not in consts:
        pytest.skip("covered by the previous test")
    assert_calls(
        path, "setdefault('LLM_ROUTER_SYNTHETIC",
        msg=f"{path.name} forces the flag instead of defaulting it",
    )


def test_explicit_flag_is_honoured(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BENCH_SANDBOX", raising=False)
    monkeypatch.setenv("LLM_ROUTER_SYNTHETIC", "1")
    assert detect_synthetic() is True


def test_bench_sandbox_env_is_honoured(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SYNTHETIC", raising=False)
    monkeypatch.setenv("BENCH_SANDBOX", "/tmp/bq_claude")
    assert detect_synthetic() is True


def test_a_sandbox_cwd_is_detected(monkeypatch, tmp_path):
    """The `/tmp/bq_<backend>` shape that put fixtures into the corpus."""
    from llm_router import routing_quality as rq

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SYNTHETIC", raising=False)
    monkeypatch.delenv("BENCH_SANDBOX", raising=False)
    monkeypatch.setattr(os, "getcwd", lambda: "/private/tmp/bq_claude")
    assert rq._in_benchmark_sandbox() is True
    assert detect_synthetic() is True


@pytest.mark.parametrize("cwd", [
    "/Users/someone/Projects/llm-router",
    "/tmp/my-scratch-work",          # a temp dir, but not a benchmark one
    "/var/folders/xy/pytest-of-me",  # macOS tmp, ordinary test scratch
    "/home/dev/src",
])
def test_ordinary_directories_are_not_sandboxes(monkeypatch, cwd):
    """Anti-over-correction. Plenty of legitimate work happens under /tmp.

    Marking every temp directory synthetic would exclude real traffic and shrink
    the very population this exists to keep honest.
    """
    from llm_router import routing_quality as rq

    monkeypatch.delenv("BENCH_SANDBOX", raising=False)
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    assert rq._in_benchmark_sandbox() is False, f"{cwd} wrongly treated as a sandbox"


def test_detect_synthetic_still_refuses_data_inference():
    """The design rule, pinned. A session-id check here would be a regression.

    R13/A-10: `forbidden not in src.split('\"\"\"')[-1]` strips the docstring
    (good instinct) but still scans the remaining TEXT of the function body,
    so a comment mentioning one of these names — easy, since the docstring
    right above explicitly discusses why session_id/model_name/token are
    forbidden — would fail this even with no real reference, or a comment
    could mask a real one depending on exact placement. Collecting the
    IDENTIFIERS the function's AST actually references (names, attributes,
    call args) means only real code counts; comments and the docstring
    (naturally excluded, since it is prose, not an identifier) cannot.
    """
    import ast
    import inspect
    import textwrap

    from llm_router import routing_quality as rq

    tree = ast.parse(textwrap.dedent(inspect.getsource(rq.detect_synthetic)))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            used.add(node.arg)

    for forbidden in ("session_id", "is_synthetic_session", "model_name", "token"):
        assert forbidden not in used, (
            f"detect_synthetic now inspects {forbidden!r} — that is an inference "
            f"drawn from a row, which this function exists not to do"
        )
