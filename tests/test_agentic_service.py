"""P4b — delegation service serialization + llm_delegate MCP tool (fake backends)."""
from __future__ import annotations

import json

from llm_router.agentic.engine import AgentRunResult
from llm_router.agentic.ledger import AcceptanceResult, Milestone
from llm_router.agentic.service import run_delegation


class _Agent:
    def __init__(self, tier, out="OK"):
        self.tier = tier
        self.out = out

    def run(self, milestone, frozen_context, budget_left):
        return AgentRunResult({"output": self.out, "tier": self.tier}, 0.0)


def _canary(marker):
    return lambda a: AcceptanceResult(marker in str(a.get("output", "")))


def test_run_delegation_returns_json_serializable_bundle():
    ms = [Milestone("M1", "do", _canary("OK"))]
    out = run_delegation("goal", ms, {1: _Agent(1)}, baseline_cost_per_milestone=0.2)
    # fully JSON-serializable (MCP tools return strings)
    json.dumps(out)
    assert out["outcome"] == "complete" and out["ok"] is True
    assert out["milestones"][0]["status"] == "done"
    assert out["events"][0]["kind"] == "plan" and out["events"][-1]["kind"] == "complete"
    assert out["savings"]["saved_usd"] > 0


# ── the MCP tool ────────────────────────────────────────────────────────────
def _fake_planner_factory():
    def pm(_goal):
        return [{"id": "M1", "description": "do", "acceptance": {"type": "canary", "marker": "SVC_CANARY"}}]
    return pm


def _fake_adapters_factory():
    return {1: _Agent(1, out="SVC_CANARY")}  # output must contain the (non-trivial) canary marker


async def test_llm_delegate_tool_with_injected_backends(monkeypatch):
    import llm_router.tools.agentic as tool
    monkeypatch.setattr(tool, "planner_factory", _fake_planner_factory)
    monkeypatch.setattr(tool, "adapters_factory", _fake_adapters_factory)
    out = json.loads(await tool.llm_delegate("build the thing"))
    assert out["outcome"] == "complete" and out["ok"] is True


async def test_llm_delegate_default_planner_fails_closed(monkeypatch):
    """When the live planner's routing fails, the tool fails closed with an honest
    'planning failed' — never fabricates a plan. (routing mocked to fail; no live call)"""
    import llm_router.router as router
    import llm_router.tools.agentic as tool

    async def boom(*a, **k):
        raise RuntimeError("router unavailable")

    monkeypatch.setattr(router, "route_and_call", boom)
    out = json.loads(await tool.llm_delegate("x"))
    assert out["ok"] is False and "planning failed" in out["reason"]


def test_register_attaches_llm_delegate_tool():
    import llm_router.tools.agentic as tool
    names: list[str] = []

    class FakeMCP:
        def tool(self):
            def deco(fn):
                names.append(fn.__name__)
                return fn
            return deco

    tool.register(FakeMCP())
    assert "llm_delegate" in names
