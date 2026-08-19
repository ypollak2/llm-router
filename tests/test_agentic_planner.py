"""P4b(planner) — Hybrid planner: model proposes, objective checks enforced."""
from __future__ import annotations

import sys

import pytest

from llm_router.agentic.delegate import delegate
from llm_router.agentic.engine import AgentRunResult, Outcome
from llm_router.agentic.ledger import Milestone
from llm_router.agentic.planner import (
    PlanRejected,
    build_acceptance,
    hybrid_plan,
    plan_to_milestones,
)


def test_build_acceptance_objective_types():
    assert build_acceptance({"type": "canary", "marker": "OBJ_CANARY"})({"output": "OBJ_CANARY"}).ok
    ok = build_acceptance({"type": "cmd", "command": [sys.executable, "-c", "pass"]})
    assert ok({}).ok


def test_build_acceptance_diff_does_not_trust_the_claim():
    """RED3-02. INVERTED — third test asserting the same defect.

    It read `build_acceptance({"type": "diff", ...})({"files": ["a.py"]}).ok`,
    i.e. that saying "I touched a.py" was proof of touching a.py. Three separate
    tests encoded that, which is why the behaviour felt well covered: the suite
    agreed with the code, and both were wrong in the same direction.

    The builder's job is to produce a diff check; whether the diff is real is
    test_agentic_acceptance.py's business.
    """
    check = build_acceptance({"type": "diff", "files": ["a.py"]})
    result = check({"files": ["a.py"]})
    assert not result.ok
    assert "repository is unchanged" in result.reason


def test_build_acceptance_rejects_subjective_and_unknown():
    for bad in ({"type": "looks_good"}, {"type": "model_says_done"}, {"type": None}, {}):
        with pytest.raises(PlanRejected):
            build_acceptance(bad)


def test_plan_to_milestones_builds_and_validates():
    plan = [
        {"id": "M1", "description": "scaffold",
         "acceptance": {"type": "diff", "files": ["m.py"]}},
        {"id": "M2", "description": "impl", "deps": ["M1"],
         "acceptance": {"type": "canary", "marker": "M2_CANARY"}},
    ]
    ms = plan_to_milestones(plan)
    assert [m.id for m in ms] == ["M1", "M2"]
    assert ms[1].deps == ("M1",)
    # RED3-02: this asserted `ms[0].acceptance({"files": ["m.py"]}).ok` — i.e.
    # that merely CLAIMING to have touched m.py satisfied the check. A diff
    # check now reads the repository, so the claim alone must not pass. What
    # this test is really about is that the planner BUILDS the checks, so it
    # asserts that and leaves diff semantics to test_agentic_acceptance.py.
    assert callable(ms[0].acceptance)
    assert not ms[0].acceptance({"files": ["m.py"]}).ok
    assert ms[1].acceptance({"output": "... M2_CANARY ..."}).ok


def test_plan_rejected_when_milestone_has_no_objective_check():
    # a milestone with a subjective acceptance sinks the whole plan (fail closed)
    plan = [{"id": "M1", "acceptance": {"type": "vibes"}}]
    with pytest.raises(PlanRejected):
        plan_to_milestones(plan)
    # ...and one with no acceptance at all
    with pytest.raises(PlanRejected):
        plan_to_milestones([{"id": "M1", "description": "x"}])


async def test_hybrid_plan_with_fake_model_feeds_delegate():
    def fake_planner(goal):
        assert "build widget" in goal
        return [{"id": "M1", "description": "make it",
                 "acceptance": {"type": "canary", "marker": "WIDGET_OK"}}]

    ms = await hybrid_plan("build widget", fake_planner)
    assert isinstance(ms[0], Milestone)

    class Agent0:
        tier = 0

        def run(self, milestone, frozen_context, budget_left):
            return AgentRunResult({"output": "WIDGET_OK"}, 0.01)

    res = delegate("build widget", ms, {0: Agent0()}, baseline_cost_per_milestone=0.2)
    assert res.outcome is Outcome.COMPLETE


async def test_hybrid_plan_rejects_non_list_and_empty():
    with pytest.raises(PlanRejected):
        await hybrid_plan("g", lambda _g: {"not": "a list"})
    with pytest.raises(PlanRejected):
        await hybrid_plan("g", lambda _g: [])


async def test_hybrid_plan_awaits_async_planner():
    async def async_planner(goal):
        return [{"id": "M1", "description": "x", "acceptance": {"type": "canary", "marker": "ASYNC_CANARY"}}]

    ms = await hybrid_plan("g", async_planner)
    assert ms[0].id == "M1"
