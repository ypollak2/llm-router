"""RED3-02/03/08 (P0) — "verified" must mean something the executor cannot fake.

The engine's central promise is that a milestone is DONE only when an objective,
executable check passes, never on the model's self-report. Three defects made
that promise false in three different ways, and they compounded:

* **RED3-08** — no ``cwd`` was ever wired, so ``artifacts["diff"]`` was always
  empty and the symbol assertion was vacuous. The check was dead.
* **RED3-02** — the evidence came from ``artifacts`` at all. Those are supplied
  BY the executor being graded. The oracle asked the defendant for the verdict.
* **RED3-03** — a ``return True`` stub submitted as the acceptance check for a
  security-hole task was ACCEPTED, and the milestone recorded DONE.

The plan requires RED3-08 and RED3-02 to ship together, and the reason is worth
restating: wiring ``cwd`` without fixing the evidence source converts a check
everyone knows is dead into one that looks alive and can be told what to say.
Strictly worse than leaving it broken.
"""

from __future__ import annotations

import subprocess

import pytest

from llm_router.agentic.acceptance import diff_check, is_stub_check, reject_stubs
from llm_router.agentic.ledger import AcceptanceResult


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    for cmd in (["init", "-q", "."], ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=d, check=True, capture_output=True)
    (d / "seed.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True, capture_output=True)
    return d


# ── RED3-03: a stub is not a check ───────────────────────────────────────────


def _stub_return_true(_artifacts):
    return True


def _stub_with_docstring(_artifacts):
    """Verify the security hole is closed."""
    return True


def _stub_pass(_artifacts):
    pass


def _stub_not_implemented(_artifacts):
    raise NotImplementedError("todo")


def _real_check(artifacts):
    return AcceptanceResult(bool(artifacts.get("files")))


@pytest.mark.parametrize(
    "fn",
    [_stub_return_true, _stub_with_docstring, _stub_pass, _stub_not_implemented],
    ids=["return_true", "docstring_then_return_true", "pass", "raise_NotImplementedError"],
)
def test_a_stub_acceptance_check_is_rejected(fn):
    """Baseline: accepted, and the milestone recorded DONE."""
    assert is_stub_check(fn), f"{fn.__name__} not recognised as a stub"
    verdict = reject_stubs(fn)({})
    assert not verdict.ok
    assert "stub" in verdict.reason


def test_a_real_check_is_not_mistaken_for_a_stub():
    """A gate with false positives gets disabled, and a disabled gate is where
    this surface already was."""
    assert not is_stub_check(_real_check)
    assert reject_stubs(_real_check)({"files": ["a.py"]}).ok


def test_source_unavailable_is_not_treated_as_a_stub():
    """A C function or runtime-built callable has no source. Unavailable is not
    evidence of a stub, and guessing would reject legitimate checks."""
    assert not is_stub_check(len)  # builtin: inspect.getsource raises


def test_the_engine_rejects_a_stub_before_marking_done():
    """Wired at the single verification point, not just available as a helper.

    An unwired guard is the RED3-10 defect — dead safety code that reads as
    coverage. The stub does not come from acceptance.py's factories, it comes
    from an executor asked to supply its own check, so the guard has to sit
    where every check is run.
    """
    from llm_router.agentic.engine import MGEEEngine
    from llm_router.agentic.ledger import Milestone

    milestone = Milestone(id="m1", description="close the hole", acceptance=_stub_return_true)
    engine = MGEEEngine({0: object()})
    verdict = engine._verify(milestone, {})
    assert not verdict.ok, "the engine accepted a do-nothing oracle"
    assert "stub" in verdict.reason


# ── RED3-02 + RED3-08: the evidence comes from the repository ────────────────


def test_a_perfect_claim_over_an_untouched_repo_fails(repo):
    """The adjudicator's reproducer, in one assertion.

    An agent that did nothing supplies artifacts describing the work it did not
    do. Before this change that passed.
    """
    chk = diff_check(files=["mod.py"], symbols=["def solve"], cwd=str(repo))
    verdict = chk({"files": ["mod.py"], "diff": "+def solve(): return 42"})
    assert not verdict.ok
    assert "repository is unchanged" in verdict.reason or "missing files" in verdict.reason


def test_real_work_passes_including_a_newly_created_file(repo):
    """`git diff HEAD` cannot see an untracked file — verified directly: it
    prints nothing and exits 0. Since "implement X" usually CREATES a file, a
    diff-only check would report the repo unchanged for exactly the work it
    exists to confirm."""
    (repo / "mod.py").write_text("def solve():\n    return 42\n")
    chk = diff_check(files=["mod.py"], symbols=["def solve"], cwd=str(repo))
    assert chk({}).ok


def test_an_unrelated_dirty_file_cannot_satisfy_the_check(repo):
    """Unscoped, any other edit in the tree supplies the symbol and the
    milestone passes on somebody else's work."""
    (repo / "unrelated.py").write_text("def solve(): pass\n")
    chk = diff_check(files=["mod.py"], symbols=["def solve"], cwd=str(repo))
    assert not chk({}).ok


def test_no_git_and_nothing_to_read_is_unknown_and_fails(tmp_path):
    """Unknown is not success — the permanent state RED3-08 left this in.

    With no declared files there is nothing to read off disk either, so the
    check genuinely cannot run and must say so.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    verdict = diff_check(symbols=["x"], cwd=str(plain))({})
    assert not verdict.ok
    assert "verification did not run" in verdict.reason


def test_outside_git_the_filesystem_is_still_the_witness(tmp_path):
    """A plain directory is a legitimate case (the bounded-operational path).

    Refusing to verify there at all would make the honest path unusable while
    the gameable one still worked — which is how a safety check gets switched
    off. The fallback is weaker (it cannot tell "created" from "already there")
    but the evidence is still the filesystem, never the executor's claim.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    chk = diff_check(files=["a.py"], symbols=["def foo"], cwd=str(plain))

    # Claim without work: still rejected, with no git anywhere in sight.
    assert not chk({"files": ["a.py"], "diff": "def foo(): ..."}).ok

    (plain / "a.py").write_text("def foo():\n    return 1\n")
    assert chk({}).ok


def test_deleting_the_work_flips_the_verdict(repo):
    """The check must track the repository, in both directions.

    A check that only ever ratchets to pass is a check that stopped reading.
    """
    chk = diff_check(files=["mod.py"], symbols=["def solve"], cwd=str(repo))
    (repo / "mod.py").write_text("def solve():\n    return 42\n")
    assert chk({}).ok
    (repo / "mod.py").unlink()
    assert not chk({}).ok


# ── RED3-01: irreversible work does not auto-freeze ──────────────────────────


def _irreversible(**artifacts):
    from llm_router.agentic.engine import AgentRunResult
    from llm_router.agentic.ledger import Milestone

    m = Milestone(
        id="m1", description="push to production",
        acceptance=_real_check, reversible=False,
    )
    return m, AgentRunResult(artifacts=artifacts)


def test_the_default_gate_refuses_unisolated_irreversible_work():
    """RED3-01 (P0). The gate mechanism was wired; its DEFAULT said yes.

    `self.gate = gate or (lambda _m, _r: True)` approved every irreversible
    milestone, and no caller ever supplied a real gate. That is what made the
    README's "irreversible steps run in an isolated git worktree, merged only
    after they verify" false — not a missing mechanism, a permissive default on
    the one it had. In review that reads as though the protection is present.
    """
    from llm_router.agentic.engine import MGEEEngine

    engine = MGEEEngine({0: object()})
    milestone, run = _irreversible()  # no worktree — ran in the live tree
    assert not engine.gate(milestone, run), (
        "an irreversible milestone froze without ever being isolated"
    )


def test_reversible_work_is_unaffected():
    """The gate must not turn into a blanket stall — most work is reversible."""
    from llm_router.agentic.engine import AgentRunResult, MGEEEngine
    from llm_router.agentic.ledger import Milestone

    engine = MGEEEngine({0: object()})
    m = Milestone(id="m1", description="edit a file", acceptance=_real_check)
    assert engine.gate(m, AgentRunResult(artifacts={}))


def test_isolated_irreversible_work_may_freeze():
    from llm_router.agentic.engine import MGEEEngine

    engine = MGEEEngine({0: object()})
    milestone, run = _irreversible(worktree="wt-1")
    assert engine.gate(milestone, run)


def test_reversibility_gate_discards_a_worktree_that_fails_to_merge():
    """Unverified work must never reach the main tree."""
    from llm_router.agentic.worktree import FakeWorktreeOps, reversibility_gate

    ops = FakeWorktreeOps(merge_ok=False)
    gate = reversibility_gate(ops)
    milestone, run = _irreversible(worktree="wt-1")

    assert not gate(milestone, run)
    assert ops.discarded == ["wt-1"], "a failed merge left the worktree behind"


def test_the_real_gate_is_actually_constructed_somewhere():
    """RED3-01's root cause: `reversibility_gate` had ZERO importers.

    The function was written, tested in isolation, and never reached production
    — dead safety code, which reads as coverage. This asserts a caller exists.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "llm_router"
    callers = []
    for path in src.rglob("*.py"):
        if path.name == "worktree.py":
            continue  # the definition itself
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "reversibility_gate"
            ):
                callers.append(f"{path.relative_to(src)}:{node.lineno}")
    assert callers, "reversibility_gate is defined but never constructed"
