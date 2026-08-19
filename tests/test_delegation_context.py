"""North Star P1-S2: provision conversation context into delegated agents.

Known Limit A: delegated agents got only their own milestone context, not the
broader Claude Code conversation. llm_delegate now accepts a `context` string that
threads through run_delegation -> delegate -> TaskLedger.session_context ->
frozen_context() -> pack_prompt, so every routed agent sees it.
"""
from __future__ import annotations

from llm_router.agentic.adapters import pack_prompt
from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger


def _ms():
    return [Milestone("M1", "do it", lambda _a: AcceptanceResult(True))]


def test_ledger_frozen_context_includes_session_context():
    led = TaskLedger(goal="t", milestones=_ms(), budget_cap_usd=1.0,
                     session_context="The project code is ORANGE-742.")
    fc = led.frozen_context()
    assert any(e.get("id") == "SESSION_CONTEXT" for e in fc)
    assert any("ORANGE-742" in str(e.get("description", "")) for e in fc)


def test_no_session_context_is_unchanged():
    led = TaskLedger(goal="t", milestones=_ms(), budget_cap_usd=1.0)
    assert led.frozen_context() == []  # no DONE milestones, no context


def test_pack_prompt_surfaces_session_context_distinctly():
    led = TaskLedger(goal="t", milestones=_ms(), budget_cap_usd=1.0,
                     session_context="SECRET-CTX-99 must be used verbatim.")
    prompt = pack_prompt(_ms()[0], led.frozen_context())
    assert "SECRET-CTX-99" in prompt
    # rendered as conversation context, NOT under the "do NOT redo" completed block
    assert "do NOT redo" not in prompt.split("SECRET-CTX-99")[0].split("\n")[-1]


def test_delegate_threads_context_to_agent(monkeypatch):
    """End-to-end: delegate(session_context=...) reaches the agent's prompt."""
    from llm_router.agentic.delegate import delegate
    from llm_router.agentic.engine import AgentRunResult

    seen = {}

    class CaptureAgent:
        tier = 0

        def run(self, milestone, frozen_context, budget_left):
            seen["prompt"] = pack_prompt(milestone, frozen_context)
            return AgentRunResult({"output": "PASS_CANARY", "tier": 0}, 0.0)

    from llm_router.agentic.acceptance import canary_check
    ms = [Milestone("M1", "impl", canary_check("PASS_CANARY"))]
    delegate("goal", ms, {0: CaptureAgent()}, baseline_cost_per_milestone=0.2,
             session_context="CARRY-ME-THROUGH")
    assert "CARRY-ME-THROUGH" in seen["prompt"]
