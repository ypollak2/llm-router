"""Serializable delegation service — the reusable core the ``llm_delegate`` MCP
tool calls. Runs ``delegate()`` and returns a JSON-safe result dict (outcome,
summary, honest savings, per-milestone status, transparency events).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from llm_router.agentic.delegate import DelegationResult, delegate
from llm_router.agentic.engine import Agent, Event
from llm_router.agentic.ledger import Milestone


def serialize(result: DelegationResult) -> dict[str, Any]:
    eff = result.savings.efficiency
    return {
        "outcome": result.outcome.value,
        "ok": result.ok,
        "reason": result.reason,
        "summary": result.summary(),
        "savings": {
            "actual_usd": result.savings.actual_usd,
            "baseline_usd": result.savings.baseline_usd,
            "saved_usd": result.savings.saved_usd,
            "efficiency": None if eff == float("inf") else eff,
        },
        "milestones": [
            {"id": m.id, "status": m.status.value, "achieved_by": m.achieved_by}
            for m in result.ledger.milestones
        ],
        "events": [e.to_dict() for e in result.events],
    }


def run_delegation(
    goal: str,
    milestones: list[Milestone],
    adapters_by_tier: dict[int, Agent],
    *,
    baseline_cost_per_milestone: float,
    budget_cap_usd: float = 1.0,
    max_attempts_per_tier: int = 2,
    event_sink: Callable[[Event], None] | None = None,
    session_context: str = "",
    workdir: str | None = None,
) -> dict[str, Any]:
    """Run one delegation and return a JSON-serializable result dict.

    RED3-08: ``workdir`` is forwarded so the acceptance check inspects the tree
    the agents worked in, not whatever directory the process happens to be in.
    """
    result = delegate(
        goal,
        milestones,
        adapters_by_tier,
        baseline_cost_per_milestone=baseline_cost_per_milestone,
        budget_cap_usd=budget_cap_usd,
        max_attempts_per_tier=max_attempts_per_tier,
        event_sink=event_sink,
        session_context=session_context,
        workdir=workdir,
    )
    return serialize(result)
