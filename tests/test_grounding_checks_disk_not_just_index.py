"""A symbol that exists on disk is not an invention, whatever the index says.

The reported problem was a "bootstrapping deadlock": create a file, cite it, get
rejected as a hallucination, escalate to a premium model — unless `okf index` was
run first. Measured 2026-09-15, the premise was half right:

    new file, full path       ACCEPTED   grounding_violations already checks disk
    new file, bare filename   accepted   _DRAFT_PATH_RE requires a "/"; never checked
    NEW SYMBOL, retrieval on  REJECTED   <- the real defect
    new symbol, no retrieval  accepted   the gate declines to judge

`grounding_violations` has had a disk escape since S2-6 ("Existing on disk is
evidence too"). `symbol_violations` never got one: it consulted `known_symbols()`,
which reads the OKF bundle, and nothing else. With 11,697 symbols indexed, a
function written five minutes earlier read as invented.

Re-indexing at session start does NOT fix this, which is why the fix is here and
not there: the gap is WITHIN a session (write at 14:20, ask at 14:21), and the
index covers TRACKED files only, so an uncommitted file is never in it however
often the index runs.
"""
from __future__ import annotations

import time
import uuid

import pytest

import llm_router.grounding as g

CTX = "<knowledge_context>\nretrieved repo docs\n</knowledge_context>"


@pytest.fixture(autouse=True)
def _clear_cache():
    g._DISK_SYMBOL_CACHE.clear()
    yield
    g._DISK_SYMBOL_CACHE.clear()


@pytest.fixture(autouse=True)
def _index_is_populated(monkeypatch):
    """The gate declines to judge when the index is empty ("nothing indexed ->
    nothing checkable"). These tests are about the DISK fallback, so the index
    must be non-empty or the gate never engages and every test passes vacuously."""
    monkeypatch.setattr(g, "known_symbols",
                        lambda: {"build_chain", "execute_chain", "route_and_call"})


@pytest.fixture
def new_symbol(tmp_path, monkeypatch):
    """A function that exists on disk, is untracked, and is in no index."""
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    name = f"reconcile_{uuid.uuid4().hex[:8]}"
    (tmp_path / "new_module.py").write_text(f"def {name}(x):\n    return x\n")
    monkeypatch.chdir(tmp_path)
    return name


def test_a_symbol_written_seconds_ago_is_not_an_invention(new_symbol):
    assert g.symbol_violations(f"Call {new_symbol}() to settle it.", CTX, "fix it") == [], (
        "a function that exists on disk was reported as invented — this is the "
        "rejection that escalates a correct draft to a premium model"
    )


def test_an_invented_symbol_is_still_caught(new_symbol):
    bogus = f"totally_invented_{uuid.uuid4().hex[:8]}"
    assert g.symbol_violations(f"Call {bogus}() now.", CTX, "fix it") == [bogus], (
        "the gate stopped catching fabrications; a disk fallback must not become "
        "an escape hatch for everything"
    )


def test_a_class_counts_as_a_definition(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    name = f"Reconciler{uuid.uuid4().hex[:6]}"
    (tmp_path / "m.py").write_text(f"class {name}:\n    pass\n")
    monkeypatch.chdir(tmp_path)
    assert g.symbol_violations(f"Use {name}() here.", CTX, "x") == []


def test_a_mere_mention_does_not_ground_a_symbol(tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    name = f"never_defined_{uuid.uuid4().hex[:6]}"
    # Present in the repo as a CALL, never as a definition.
    (tmp_path / "m.py").write_text(f"result = {name}()\n")
    monkeypatch.chdir(tmp_path)
    assert g.symbol_violations(f"Call {name}() here.", CTX, "x") == [name], (
        "matching a call site would ground any name the draft itself could invent"
    )


def test_one_subprocess_for_many_symbols(new_symbol, monkeypatch):
    calls = {"n": 0}
    real = g.subprocess.run if hasattr(g, "subprocess") else None

    import subprocess as sp
    orig = sp.run

    def counting(*a, **k):
        if a and a[0] and a[0][0] == "git" and "grep" in a[0]:
            calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(sp, "run", counting)
    many = " ".join(f"bogus_{i}_{uuid.uuid4().hex[:4]}()" for i in range(8))
    g.symbol_violations(many, CTX, "x")
    assert calls["n"] <= 1, (
        f"{calls['n']} subprocesses for 8 symbols; the cost here is process spawn "
        "and it was measured at 893ms unbatched vs 42ms batched"
    )


def test_the_check_is_fast_enough_for_the_draft_budget(new_symbol):
    t0 = time.monotonic()
    g.symbol_violations(f"Call {new_symbol}() here.", CTX, "x")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, (
        f"{elapsed:.1f}s against a ~37s first-model budget. Unscoped `git grep "
        "--untracked` walked stray data/ dirs and cost 3.3s; it is scoped to "
        "source extensions for this reason."
    )


def test_a_broken_git_falls_back_to_the_index_verdict(new_symbol, monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    # Degrades to the pre-existing behaviour — stricter, never looser.
    assert g.symbol_violations(f"Call {new_symbol}() here.", CTX, "x") == [new_symbol]


def test_the_gate_still_declines_to_judge_without_retrieval(new_symbol):
    bogus = f"totally_invented_{uuid.uuid4().hex[:8]}"
    assert g.symbol_violations(f"Call {bogus}() now.", "", "x") == [], (
        "with no injected knowledge the draft is general-purpose and this index "
        "has no standing to judge the names in it"
    )
