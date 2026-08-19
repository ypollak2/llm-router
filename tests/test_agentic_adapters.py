"""P3 — Codex adapter behind run_agentic(), driven by a FAKE subprocess runner.

No live model is ever invoked. The real subprocess_runner is exercised with a
trivial python process (stdin echo) to prove the plumbing without a model.
"""
from __future__ import annotations

import sys

from llm_router.agentic.acceptance import canary_check
from llm_router.agentic.adapters import (
    CodexAdapter,
    ProcResult,
    pack_prompt,
    subprocess_runner,
)
from llm_router.agentic.engine import MGEEEngine, Outcome
from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger


def _ledger(ms, cap=10.0):
    return TaskLedger(goal="t", milestones=ms, budget_cap_usd=cap)


def test_pack_prompt_includes_task_and_frozen_context():
    m = Milestone("M2", "implement foo", lambda a: AcceptanceResult(True))
    frozen = [{"id": "M1", "description": "scaffold", "artifacts": {}}]
    prompt = pack_prompt(m, frozen)
    assert "implement foo" in prompt
    assert "M1" in prompt and "scaffold" in prompt
    assert "do NOT redo" in prompt


def test_codex_adapter_captures_runner_output():
    calls = []

    def fake_runner(argv, input_text):
        calls.append((argv, input_text))
        return ProcResult(0, "PROVIDER_CODEX_CANARY\n", "")

    a = CodexAdapter(tier=1, runner=fake_runner, binary="codex-x")
    res = a.run(Milestone("M1", "do it", lambda _a: AcceptanceResult(True)), [], 5.0)
    assert res.artifacts["provider"] == "codex"
    assert "PROVIDER_CODEX_CANARY" in res.artifacts["output"]
    assert res.confidence == 1.0
    # correct `codex exec` invocation; prompt is the last argv element (not stdin)
    argv = calls[0][0]
    assert argv[:2] == ["codex-x", "exec"]
    assert "--json" in argv and "--skip-git-repo-check" in argv
    # workspace-write sandbox is REQUIRED — without it codex runs read-only and
    # every patch is rejected (Phase B: "writing is blocked by read-only sandbox").
    assert "--sandbox" in argv and "workspace-write" in argv
    assert argv[-1].startswith("TASK: do it")


def test_codex_adapter_argv_includes_model_and_cwd_when_set():
    calls = []

    def fake(argv, input_text):
        calls.append(argv)
        return ProcResult(0, "ok", "") if "diff" not in argv else ProcResult(0, "", "")

    CodexAdapter(tier=1, runner=fake, binary="cx", model="gpt-5.5", cwd="/repo").run(
        Milestone("M1", "x", lambda _a: AcceptanceResult(True)), [], 5.0
    )
    argv = calls[0]
    assert "-m" in argv and "gpt-5.5" in argv
    assert "-C" in argv and "/repo" in argv


def test_codex_adapter_captures_git_diff_as_artifact():
    def fake(argv, input_text):
        if argv[:1] == ["git"]:  # the git diff call
            return ProcResult(0, "diff --git a/m.py b/m.py\n+++ b/m.py\n+def foo(): ...\n", "")
        return ProcResult(0, "done", "")

    res = CodexAdapter(tier=1, runner=fake, binary="cx", cwd="/repo").run(
        Milestone("M1", "impl foo", lambda _a: AcceptanceResult(True)), [], 5.0
    )
    assert "def foo" in res.artifacts["diff"]
    assert res.artifacts["files"] == ["m.py"]  # parsed from +++ b/ lines


def test_codex_adapter_failure_lowers_confidence():
    def failing(argv, input_text):
        return ProcResult(1, "", "boom")

    res = CodexAdapter(tier=1, runner=failing).run(
        Milestone("M1", "", lambda _a: AcceptanceResult(True)), [], 5.0
    )
    assert res.artifacts["returncode"] == 1 and res.confidence < 1.0


def test_codex_adapter_drives_engine_to_completion():
    def good(argv, input_text):
        return ProcResult(0, "PROVIDER_CODEX_CANARY", "")

    ms = [Milestone("M1", "", canary_check("PROVIDER_CODEX_CANARY"))]
    res = MGEEEngine({1: CodexAdapter(tier=1, runner=good)}).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert ms[0].achieved_by == 1


def test_escalation_to_codex_carries_frozen_context_into_prompt():
    """M1 done on a cheap tier; M2 escalates to Codex, whose prompt must carry M1."""
    class Cheap:
        tier = 0

        def run(self, milestone, frozen_context, budget_left):
            from llm_router.agentic.engine import AgentRunResult
            # produces the canary only for M1; M2 gets a non-canary → fails at t0
            out = "PROVIDER_CODEX_CANARY" if milestone.id == "M1" else "weak"
            return AgentRunResult({"output": out, "tier": 0}, 0.0)

    seen_prompts = []

    def codex(argv, input_text):
        seen_prompts.append(argv[-1])  # prompt is the last argv element now
        return ProcResult(0, "PROVIDER_CODEX_CANARY", "")

    ms = [
        Milestone("M1", "scaffold", canary_check("PROVIDER_CODEX_CANARY")),
        Milestone("M2", "impl", canary_check("PROVIDER_CODEX_CANARY")),
    ]
    res = MGEEEngine(
        {0: Cheap(), 1: CodexAdapter(tier=1, runner=codex)}, max_attempts_per_tier=1
    ).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert ms[0].achieved_by == 0 and ms[1].achieved_by == 1
    # Codex was invoked for M2 and its prompt carried the completed M1 (carry-forward)
    assert any("M1" in p and "scaffold" in p for p in seen_prompts)


def test_real_subprocess_runner_without_a_model():
    """Exercise the REAL runner with a trivial python process (stdin echo)."""
    r = subprocess_runner([sys.executable, "-c", "import sys; print(sys.stdin.read())"], "hi-there")
    assert r.returncode == 0 and "hi-there" in r.stdout
    # missing binary is captured, not raised
    nf = subprocess_runner(["definitely-not-real-xyz"], "")
    assert nf.returncode == 127 and "not found" in nf.stderr
