"""MCP tool: ``llm_delegate`` — agentic delegation entry point.

Thin wrapper. The heavy logic (planning, milestone-gated escalating execution,
acceptance, savings) lives in ``llm_router.agentic``. This module builds the default
backends (planner + Codex adapter ladder) and calls the delegation service. The
planner/adapter factories are module-level and injectable so tests never touch a
live model.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from llm_router.agentic.adapters import CodexAdapter
from llm_router.agentic.planner import PlannerModel, PlanRejected, hybrid_plan
from llm_router.agentic.service import run_delegation

# Injectable factories — tests override these; production uses the defaults.
planner_factory: Callable[[], PlannerModel] | None = None
adapters_factory: Callable[[], dict[int, Any]] | None = None


_PLANNER_SYSTEM = (
    "You are a task planner for an automated coding agent. Break the task into a "
    "small ordered list of milestones. Every milestone MUST have an OBJECTIVE, "
    "executable acceptance check — never a subjective one. Output ONLY a JSON array."
)


def _planner_prompt(goal: str) -> str:
    return (
        f"Task: {goal}\n\n"
        "Return ONLY a JSON array of milestones (no prose). Each item is "
        '{"id": str, "description": str, "acceptance": <check>} where <check> is one of:\n'
        '  {"type":"cmd","command":["argv","..."]}   # passes iff the command exits 0\n'
        '  {"type":"lint","paths":["path","..."]}     # passes iff the linter is clean\n'
        '  {"type":"diff","files":["f"],"symbols":["s"]}  # produced files/symbols present\n'
        '  {"type":"canary","marker":"TOKEN"}         # marker present in the output\n'
    )


def _extract_plan_json(text: str) -> list[dict[str, Any]] | None:
    """Robustly pull a JSON milestone array out of a model's text response."""
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        i, j = text.find("["), text.rfind("]")
        candidate = text[i : j + 1] if (i != -1 and j > i) else None
    if candidate is None:
        return None
    try:
        val = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, list) else None


def _default_planner() -> PlannerModel:
    """Live planner: routes to a llm_router-selected model that emits an objective-check
    milestone plan, then parses the JSON. Fails closed (PlanRejected) on any error
    so the tool surfaces an honest 'planning failed' rather than fabricating done."""
    async def planner_model(goal: str) -> list[dict[str, Any]]:
        try:
            from llm_router.router import route_and_call
            from llm_router.types import TaskType
            # QUERY (not ANALYZE): the planner must return BARE JSON, but ANALYZE
            # carries a STRUCTURE gate that requires >=2 markdown markers and so
            # rejects every valid plan ("0 markers"). QUERY has only a LENGTH gate.
            # suppress_ledger: this planner call is INTERNAL to a delegation. The
            # parent `delegate` row (record_delegation) accounts for the whole
            # operation, so this must not emit its own top-level completion row —
            # aggregate-delegation-only, no double counting (CF-1 §4.4).
            resp = await route_and_call(
                TaskType.QUERY, _planner_prompt(goal), system_prompt=_PLANNER_SYSTEM,
                suppress_ledger=True,
            )
        except Exception as exc:  # noqa: BLE001 — any routing failure fails closed
            raise PlanRejected(f"planner routing failed: {exc}") from exc
        plan = _extract_plan_json(getattr(resp, "content", "") or "")
        if plan is None:
            raise PlanRejected("planner model did not return a parseable JSON milestone plan")
        return plan
    return planner_model


def _default_adapters() -> dict[int, Any]:
    # tier 0 = local ReAct/Ollama agent (cheapest, best-effort); tier 1 = Codex.
    from llm_router.agentic.react import ReActAgent
    return {0: ReActAgent(tier=0), 1: CodexAdapter(tier=1)}


async def llm_delegate(
    task: str, budget_usd: float = 1.0, baseline_cost_per_milestone: float = 0.20,
    context: str = "", bounded: bool | None = None, workdir: str | None = None,
) -> str:
    """Agentic delegation: decompose *task* into milestones, run them on the
    cheapest capable tier with objective acceptance checks, escalate on failure
    without redoing achieved milestones, and return a JSON result with the
    outcome, transparency events, and honest savings. Never gets stuck — an
    unmeetable milestone is surfaced, not looped.

    *context* is optional conversation context from the calling session; it's
    handed to every delegated agent (bounded) so a routed model isn't blind to
    what was discussed — not just its own milestones (North Star P1-S2).

    *bounded* selects the CF-4 bounded-operational mode: a SIMPLE task that needs
    tools (write/command/verify) runs as a single-milestone, tightly-budgeted,
    mandatorily-verified route rather than a full plan+run+verify loop or an
    untoolable completion. ``None`` (default) auto-detects via
    :func:`should_route_bounded`; ``True``/``False`` force it. Bounded runs cap the
    plan to one milestone, derive the budget from model pricing, and record the
    parent ledger row as ``route_kind="bounded_operational"``."""
    from llm_router.bounded_operational import (
        MAX_BOUNDED_ATTEMPTS, MAX_BOUNDED_MILESTONES, bounded_op_budget_usd,
        should_route_bounded,
    )
    if bounded is None:
        try:
            from llm_router.classify import classify_signals
            _complexity = classify_signals(task).complexity.value
            bounded = should_route_bounded(task, _complexity)
        except Exception:  # noqa: BLE001 — detection failure → full delegate (safe default)
            bounded = False

    planner = (planner_factory or _default_planner)()
    adapters = (adapters_factory or _default_adapters)()
    try:
        milestones = await hybrid_plan(task, planner)
    except PlanRejected as exc:
        return json.dumps({"outcome": "surfaced", "ok": False, "reason": f"planning failed: {exc}"})

    route_kind = "delegate"
    max_attempts = 2
    if bounded:
        # CF-4: cap to a single milestone, derive the budget from pricing, and cap
        # escalation to one tier. Mandatory objective verification is enforced by the
        # planner's acceptance check on the (single) milestone — an unverifiable plan
        # is rejected by hybrid_plan, so a bounded run never records verify=False.
        route_kind = "bounded_operational"
        milestones = milestones[:MAX_BOUNDED_MILESTONES]
        budget_usd = bounded_op_budget_usd(task_type="delegate", model_tier=1)
        max_attempts = MAX_BOUNDED_ATTEMPTS

    # RED3-08: the acceptance check has to know WHERE the work happened.
    # Defaulting to the process cwd is right for the MCP server (it runs in the
    # user's project) but must stay overridable — an explicit value is the only
    # way a caller working in a scratch directory can be verified against the
    # right tree. Left unresolved, a repo-reading check silently inspects
    # whatever directory the server happens to be in.
    import os as _os

    effective_workdir = workdir or _os.getcwd()

    result = run_delegation(
        task, milestones, adapters,
        baseline_cost_per_milestone=baseline_cost_per_milestone,
        budget_cap_usd=budget_usd,
        max_attempts_per_tier=max_attempts,
        session_context=(context or "")[:2000],  # bound: don't blow the agent's prompt
        workdir=effective_workdir,
    )
    result["route_kind"] = route_kind
    # Record the honest saving into llm_router's ledger (fail-open — never breaks the call).
    from llm_router.agentic.telemetry import record_delegation_savings
    await record_delegation_savings(result)
    # North Star measurement: record routing quality — escalation/mis-route/completion/
    # savings — so "route cheap, escalate on failure" is measured, not assumed. Fail-open.
    from llm_router.routing_quality import record_delegation
    record_delegation(result, route_kind=route_kind)
    return json.dumps(result)


def register(mcp, should_register=None) -> None:
    """Register the llm_delegate tool with the MCPServer server, honouring the slim
    gate. Under the consolidated default it is hidden behind the llm_act door
    (the function stays importable; llm_act dispatches to it)."""
    if should_register is None or should_register("llm_delegate"):
        mcp.tool()(llm_delegate)
