"""P2 — objective acceptance-check runners (docs/agentic-router.md §4.2)."""
from __future__ import annotations

import sys

from llm_router.agentic.acceptance import (
    canary_check,
    cmd_check,
    diff_check,
    lint_check,
    reproducible,
    validator_check,
)


def test_canary_pass_and_fail():
    chk = canary_check("PROVIDER_OK")
    assert chk({"output": "...PROVIDER_OK..."}).ok
    r = chk({"output": "nope"})
    assert not r.ok and "not found" in r.reason


def test_validator_pass_fail_and_error():
    assert validator_check(lambda a: a["n"] == 5)({"n": 5}).ok
    assert not validator_check(lambda a: a["n"] == 5)({"n": 4}).ok
    # a broken validator fails closed, never raises
    r = validator_check(lambda a: a["missing"])({})
    assert not r.ok and "error" in r.reason


def _tmp_repo(tmp_path):
    """A real git repo with one commit — diff_check reads git, so tests need one."""
    import subprocess

    d = tmp_path / "repo"
    d.mkdir()
    for cmd in (["init", "-q", "."], ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=d, check=True, capture_output=True)
    (d / "seed.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True, capture_output=True)
    return d


def test_diff_check_reads_the_repo_not_the_agents_claims(tmp_path):
    """RED3-02 / RED3-08 (P0). INVERTED — this test used to assert the defect.

    It previously passed an artifacts dict and took the verdict from it:

        assert chk({"files": ["a.py"], "diff": "def foo(): ..."}).ok

    That is the finding written as a requirement. `artifacts` is reported BY the
    executor being graded, so the test asserted that an agent *claiming* to have
    done the work was sufficient proof it had. Any real fix would have surfaced
    here as the regression.
    """
    repo = _tmp_repo(tmp_path)
    chk = diff_check(files=["a.py"], symbols=["def foo"], cwd=str(repo))

    # The exact payload the old test accepted: a perfect claim over an untouched
    # repository. It must now fail.
    gamed = chk({"files": ["a.py", "b.py"], "diff": "def foo(): ..."})
    assert not gamed.ok
    assert "missing files" in gamed.reason

    # Real work passes — including a NEW file, which `git diff HEAD` alone
    # cannot see (verified: it prints nothing and exits 0 for an untracked path).
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    assert chk({}).ok

    # Right file, wrong content still fails.
    (repo / "a.py").write_text("def bar():\n    return 1\n")
    miss = chk({})
    assert not miss.ok and "missing symbols" in miss.reason


def test_diff_check_is_scoped_to_the_declared_files(tmp_path):
    """An unrelated dirty file must not satisfy the assertion.

    Unscoped, `git diff` over the whole tree lets any other edit in the repo
    supply the symbol, and the milestone passes on somebody else's work.
    """
    repo = _tmp_repo(tmp_path)
    (repo / "unrelated.py").write_text("def foo(): pass\n")
    r = diff_check(files=["a.py"], symbols=["def foo"], cwd=str(repo))({})
    assert not r.ok


def test_diff_check_fails_when_it_cannot_see_the_repository(tmp_path):
    """Unknown is not success.

    A verification step that cannot read the repo has verified nothing. RED3-08
    left it in that state permanently: no cwd was ever wired, so the diff was
    always empty and the symbol assertion was vacuous.
    """
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    # No declared files => nothing to read off disk either => genuinely unknown.
    r = diff_check(symbols=["def foo"], cwd=str(not_a_repo))({})
    assert not r.ok
    assert "verification did not run" in r.reason

    # With declared files, the filesystem is still a witness — and a bare claim
    # is still not one.
    scoped = diff_check(files=["a.py"], symbols=["def foo"], cwd=str(not_a_repo))
    assert not scoped({"files": ["a.py"], "diff": "def foo(): ..."}).ok


def test_cmd_check_pass_fail_notfound_timeout():
    assert cmd_check([sys.executable, "-c", "import sys; sys.exit(0)"])({}).ok
    fail = cmd_check([sys.executable, "-c", "import sys; sys.exit(3)"])({})
    assert not fail.ok and "exit 3" in fail.reason
    nf = cmd_check(["definitely-not-a-real-binary-xyz"])({})
    assert not nf.ok and "not found" in nf.reason and nf.deterministic
    to = cmd_check([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)({})
    assert not to.ok and "timed out" in to.reason and to.deterministic


def test_lint_check_missing_linter_is_nondeterministic():
    chk = lint_check(["x.py"], linter="no-such-linter-binary")
    r = chk({})
    assert not r.ok and not r.deterministic  # unknown, not a hard fail → engine re-runs


def test_reproducible_detects_flaky_and_passes_stable():
    # stable check → verdict passes through unchanged
    assert reproducible(canary_check("OK"))({"output": "OK"}).ok

    # a flapping check (verdict flips each call) → flagged non-deterministic
    state = {"i": 0}

    def flapping(_artifacts):
        state["i"] += 1
        from llm_router.agentic.ledger import AcceptanceResult
        return AcceptanceResult(state["i"] % 2 == 1)

    r = reproducible(flapping)({})
    assert not r.ok and not r.deterministic


def test_reproducible_composes_with_engine_flaky_rerun():
    """A reproducible() wrapper that reports non-deterministic must make the
    engine re-run once (not escalate) — end-to-end with the real engine."""
    from llm_router.agentic.engine import MGEEEngine, Outcome
    from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger

    calls = {"n": 0}

    def eventually_ok(_artifacts):
        # non-reproducible on the 1st verify, deterministically ok afterward
        calls["n"] += 1
        if calls["n"] <= 1:
            return AcceptanceResult(False, "flaky", deterministic=False)
        return AcceptanceResult(True)

    class OneTier:
        tier = 0

        def run(self, milestone, frozen_context, budget_left):
            from llm_router.agentic.engine import AgentRunResult
            return AgentRunResult({"ok": True}, 0.01)

    ms = [Milestone("M1", "", eventually_ok)]
    res = MGEEEngine({0: OneTier(), 1: OneTier()}).run(
        TaskLedger(goal="t", milestones=ms, budget_cap_usd=10.0)
    )
    assert res.outcome is Outcome.COMPLETE
    assert ms[0].achieved_by == 0  # re-ran on tier 0, never escalated on the flake
