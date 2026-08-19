"""Hybrid planner — a model proposes the milestone breakdown, but every
acceptance check is CONSTRAINED to the objective vocabulary (cmd/lint/diff/canary)
and validated. A milestone that proposes a subjective / unknown check is rejected,
so the "a milestone is DONE only on an objective, executable check — never the
model's self-report" guarantee survives even when a model does the planning.

The planner model is injected as a callable returning a raw plan (list of dicts),
so unit tests drive it with a fake — no live model.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from llm_router.agentic.acceptance import (
    canary_check,
    cmd_check,
    diff_check,
    lint_check,
)
from llm_router.agentic.ledger import AcceptanceCheck, Milestone

# Only these acceptance types may be emitted by a planner — all objective/executable.
ALLOWED_CHECK_TYPES = frozenset({"cmd", "lint", "diff", "canary"})

# planner_model(goal) -> raw plan (list of milestone dicts), sync OR async.
# The live default planner is async (it calls llm_router routing); test fakes are sync.
PlannerModel = Callable[[str], Any]


class PlanRejected(ValueError):
    """A proposed plan/milestone lacks a valid objective acceptance check."""


# R3 (verification gaming): a cheap planner can emit a check that passes without
# any real work. Reject the trivial classes so a milestone can't freeze DONE for free.
_TRIVIAL_CMD_HEADS = frozenset({"echo", "true", ":", "printf", "exit", "test-true"})
_GENERIC_MARKERS = frozenset({
    "ok", "done", "pass", "passed", "yes", "true", "success",
    "complete", "completed", "finished", "good", "fine", "valid",
})


def _reject_if_trivial(t: str, spec: dict[str, Any]) -> None:
    """Raise PlanRejected if the acceptance check would pass without real work."""
    if t == "cmd":
        cmd = spec.get("command") or []
        head = str(cmd[0]).rsplit("/", 1)[-1].lower() if cmd else ""
        if not cmd or head in _TRIVIAL_CMD_HEADS:
            raise PlanRejected(f"trivial cmd check (no real verification): {cmd!r}")
    elif t == "canary":
        marker = str(spec.get("marker", "")).strip()
        if len(marker) < 4 or marker.lower() in _GENERIC_MARKERS:
            raise PlanRejected(f"trivial/generic canary marker: {marker!r}")
    elif t == "diff":
        if not spec.get("files") and not spec.get("symbols"):
            raise PlanRejected("trivial diff check (asserts no files or symbols)")


def build_acceptance(spec: dict[str, Any]) -> AcceptanceCheck:
    """Map an objective check spec → an AcceptanceCheck. Rejects anything not in
    the whitelist (e.g. a model trying to sneak in a subjective 'looks_good') and
    any TRIVIAL check that would pass without real work (R3)."""
    if not isinstance(spec, dict):
        raise PlanRejected(f"acceptance must be a spec dict, got {type(spec).__name__}")
    t = spec.get("type")
    if t not in ALLOWED_CHECK_TYPES:
        raise PlanRejected(f"non-objective / unknown acceptance type: {t!r}")
    _reject_if_trivial(t, spec)
    if t == "cmd":
        return cmd_check(spec["command"], cwd=spec.get("cwd"), timeout=spec.get("timeout", 60.0))
    if t == "lint":
        return lint_check(spec["paths"], cwd=spec.get("cwd"))
    if t == "diff":
        return diff_check(files=spec.get("files", ()), symbols=spec.get("symbols", ()))
    # t == "canary"
    return canary_check(spec["marker"], field=spec.get("field", "output"))


#: Hard ceiling on milestones in one plan (RED3-07). The plan comes from a MODEL,
#: so its length is model output, not a user-chosen parameter. Worst-case attempts
#: are milestones x tiers x k — an unbounded plan makes the engine's bound
#: unbounded too, and the ladder would grind through a hallucinated 400-step
#: breakdown before anyone noticed. Rejected rather than truncated: silently
#: dropping milestones would execute a DIFFERENT plan than the one produced, and
#: the dropped tail is exactly where a plan's finishing/verification steps live.
MAX_PLAN_MILESTONES = 50


def plan_to_milestones(plan: list[dict[str, Any]]) -> list[Milestone]:
    """Validate + build a raw plan into Milestones. Every milestone MUST carry an
    objective acceptance spec or the whole plan is rejected (fail closed)."""
    if not plan:
        raise PlanRejected("empty plan")
    if len(plan) > MAX_PLAN_MILESTONES:
        raise PlanRejected(
            f"plan has {len(plan)} milestones, over the {MAX_PLAN_MILESTONES} cap — "
            "worst-case attempts scale with milestone count, so an unbounded plan "
            "removes the engine's hard bound"
        )
    milestones: list[Milestone] = []
    for item in plan:
        mid = item.get("id")
        if not mid:
            raise PlanRejected("milestone missing 'id'")
        if "acceptance" not in item:
            raise PlanRejected(f"milestone {mid!r} has no acceptance check")
        acceptance = build_acceptance(item["acceptance"])  # raises PlanRejected if subjective
        milestones.append(
            Milestone(
                id=str(mid),
                description=str(item.get("description", "")),
                acceptance=acceptance,
                deps=tuple(item.get("deps", ())),
                reversible=bool(item.get("reversible", True)),
            )
        )
    return milestones


async def hybrid_plan(goal: str, planner_model: PlannerModel) -> list[Milestone]:
    """Ask the (injected) planner model for a breakdown, then constrain + build it.

    Async so the live default planner can call llm_router routing. Tolerates a sync
    planner (test fakes) — if the result is awaitable it's awaited, otherwise used
    directly.
    """
    raw = planner_model(goal)
    if inspect.isawaitable(raw):
        raw = await raw
    if not isinstance(raw, list):
        raise PlanRejected(f"planner must return a list of milestones, got {type(raw).__name__}")
    return plan_to_milestones(raw)
