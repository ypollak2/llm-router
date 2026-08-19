"""P4a — delegate() orchestrator: milestones → engine → {outcome, ledger, events, savings}."""
from __future__ import annotations

from llm_router.agentic.delegate import delegate
from llm_router.agentic.engine import AgentRunResult, Outcome
from llm_router.agentic.ledger import AcceptanceResult, Milestone


class FakeAgent:
    def __init__(self, tier, cost=0.01):
        self.tier = tier
        self.cost = cost

    def run(self, milestone, frozen_context, budget_left):
        return AgentRunResult({"tier": self.tier, "mid": milestone.id}, self.cost)


def needs_tier(req):
    return lambda a: AcceptanceResult(a.get("tier", -1) >= req,
                                      "" if a.get("tier", -1) >= req else f"need t{req}")


def test_delegate_happy_path_bundle_and_savings():
    ms = [Milestone(f"M{i}", "", needs_tier(0)) for i in range(3)]
    res = delegate("goal", ms, {0: FakeAgent(0), 1: FakeAgent(1)},
                   baseline_cost_per_milestone=0.20)
    assert res.ok and res.outcome is Outcome.COMPLETE
    assert res.savings.saved_usd > 0
    assert res.events[0].kind == "plan" and res.events[-1].kind == "complete"


def test_delegate_escalates_without_rework():
    ms = [Milestone("M1", "", needs_tier(0)), Milestone("M2", "", needs_tier(1))]
    res = delegate("goal", ms, {0: FakeAgent(0), 1: FakeAgent(1)},
                   baseline_cost_per_milestone=0.20, max_attempts_per_tier=1)
    assert res.ok
    assert res.ledger.milestones[0].achieved_by == 0
    assert res.ledger.milestones[1].achieved_by == 1


def test_delegate_surfaces_when_unmeetable():
    ms = [Milestone("M1", "", needs_tier(9))]
    res = delegate("goal", ms, {0: FakeAgent(0), 1: FakeAgent(1)},
                   baseline_cost_per_milestone=0.20, max_attempts_per_tier=1)
    assert not res.ok and res.outcome is Outcome.SURFACED
    assert "need t9" in res.reason


def test_delegate_summary_renders_events_savings_and_verdict():
    ms = [Milestone("M1", "", needs_tier(0))]
    res = delegate("goal", ms, {0: FakeAgent(0)}, baseline_cost_per_milestone=0.20)
    s = res.summary()
    assert "plan" in s and "complete" in s
    assert "saved" in s and "✅" in s


def test_delegate_event_sink_streams_live():
    seen = []
    ms = [Milestone("M1", "", needs_tier(0))]
    delegate("goal", ms, {0: FakeAgent(0)}, baseline_cost_per_milestone=0.20,
             event_sink=seen.append)
    assert [e.kind for e in seen][:1] == ["plan"]
    assert seen[-1].kind == "complete"
