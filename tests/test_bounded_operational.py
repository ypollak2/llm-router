"""CF-4: the bounded-operational route — decision, pricing budget, and execution.

Covers §8: capability-driven route selection (simple + write/cmd/verify, flag-gated),
a budget DERIVED from model pricing (never a magic constant), and Scenario 4 — a bounded
run that actually modifies a file, passes an objective diff_check, and records exactly one
`route_kind="bounded_operational"` ledger row.
"""
from __future__ import annotations

import json

import pytest

from llm_router.agentic.engine import AgentRunResult
from llm_router.bounded_operational import (
    bounded_op_budget_usd,
    bounded_operational_enabled,
    should_route_bounded,
)
from llm_router.routing_quality import load_records


# ── budget is pricing-derived, not a magic constant ──────────────────────────

def test_budget_is_pricing_derived_and_positive():
    b1 = bounded_op_budget_usd("delegate", model_tier=1)
    b3 = bounded_op_budget_usd("delegate", model_tier=3)
    assert b1 > 0 and b3 > 0
    assert b3 > b1  # a pricier tier yields a larger cap → derived from pricing, not fixed
    assert b1 >= 0.01  # floor so a $0 local tier still has escalation headroom


# ── decision matrix (§8.2) ────────────────────────────────────────────────────

def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)
    assert bounded_operational_enabled() is False
    # even a perfect bounded candidate does NOT route bounded while the flag is off
    assert should_route_bounded("Add a blank line to README.md", "simple") is False


@pytest.mark.parametrize("prompt, complexity, expected", [
    ("Add a blank line to README.md", "simple", True),     # simple + write → bounded
    ("Run the tests and show failures", "simple", True),    # simple + run_commands → bounded
    ("What is the GIL?", "simple", False),                  # simple + no tools → completion
    ("Add a blank line to README.md", "moderate", False),   # moderate → full delegate, not bounded
    ("Rename X across the codebase", "complex", False),     # complex → full delegate
])
def test_should_route_bounded_matrix(monkeypatch, prompt, complexity, expected):
    monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
    assert should_route_bounded(prompt, complexity) is expected


# ── Scenario 4: bounded run modifies a file, passes diff_check, one ledger row ─

class _WritingAgent:
    """Fake tool-capable agent: actually writes the file, returns diff artifacts."""
    def __init__(self, tier, target):
        self.tier = tier
        self.target = target

    def run(self, milestone, frozen_context, budget_left):
        # do the REAL work: append a blank line
        with open(self.target, "a", encoding="utf-8") as f:
            f.write("\n")
        return AgentRunResult(
            {"files": ["README.md"], "diff": "+\n", "tier": self.tier}, 0.0)


@pytest.mark.asyncio
async def test_scenario4_bounded_edit_writes_verifies_records(tmp_path, monkeypatch, temp_db):
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n")
    ledger = tmp_path / "rq.jsonl"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(ledger))
    monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")

    import llm_router.tools.agentic as tool

    def _planner_factory():
        # one milestone whose acceptance is a REAL diff_check on README.md
        def pm(_goal):
            return [{"id": "M1", "description": "add blank line",
                     "acceptance": {"type": "diff", "files": ["README.md"]}}]
        return pm

    monkeypatch.setattr(tool, "planner_factory", _planner_factory)
    monkeypatch.setattr(tool, "adapters_factory",
                        lambda: {0: _WritingAgent(0, str(readme))})

    # RED3-08: the acceptance check reads the repository now, so this test has
    # to say WHERE the work happens. It did not need to before — verification
    # was vacuous, so a milestone declaring `files: ["README.md"]` passed
    # without anyone establishing which README.md was meant. Being forced to
    # name the directory is the fix working, not a burden it imposed.
    out = json.loads(
        await tool.llm_delegate(
            "Add a blank line to README.md", bounded=True, workdir=str(tmp_path)
        )
    )

    assert out["outcome"] == "complete" and out["ok"] is True
    assert out["route_kind"] == "bounded_operational"
    # the file was ACTUALLY modified
    assert readme.read_text() == "# Title\n\n"
    # exactly one parent ledger row, tagged bounded_operational, verified
    rows = [r for r in load_records(str(ledger)) if not r.get("_invalid")]
    assert len(rows) == 1
    r = rows[0]
    assert r["route_kind"] == "bounded_operational"
    assert r["verification_attempted"] is True and r["verification_passed"] is True


@pytest.mark.asyncio
async def test_bounded_caps_plan_to_one_milestone(tmp_path, monkeypatch, temp_db):
    monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    import llm_router.tools.agentic as tool

    def _planner_factory():
        def pm(_goal):
            return [{"id": f"M{i}", "description": "step",
                     "acceptance": {"type": "canary", "marker": "BOUNDED_CANARY"}} for i in range(3)]
        return pm

    class _A:
        tier = 0
        def run(self, m, fc, bl):
            return AgentRunResult({"output": "BOUNDED_CANARY", "tier": 0}, 0.0)

    monkeypatch.setattr(tool, "planner_factory", _planner_factory)
    monkeypatch.setattr(tool, "adapters_factory", lambda: {0: _A()})
    out = json.loads(await tool.llm_delegate("add a blank line to README.md", bounded=True))
    # bounded caps the plan to a single milestone even though the planner emitted 3
    assert len(out["milestones"]) == 1


@pytest.mark.asyncio
async def test_auto_detect_off_by_default_stays_delegate(tmp_path, monkeypatch, temp_db):
    monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)  # flag off
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    import llm_router.tools.agentic as tool

    def _planner_factory():
        def pm(_goal):
            return [{"id": "M1", "description": "do",
                     "acceptance": {"type": "canary", "marker": "BOUNDED_CANARY"}}]
        return pm

    class _A:
        tier = 0
        def run(self, m, fc, bl):
            return AgentRunResult({"output": "BOUNDED_CANARY", "tier": 0}, 0.0)

    monkeypatch.setattr(tool, "planner_factory", _planner_factory)
    monkeypatch.setattr(tool, "adapters_factory", lambda: {0: _A()})
    # bounded=None → auto-detect; flag off → NOT bounded → route_kind delegate
    out = json.loads(await tool.llm_delegate("Add a blank line to README.md"))
    assert out["route_kind"] == "delegate"
