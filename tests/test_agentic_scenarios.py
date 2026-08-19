"""P1/P2 remaining scenarios — S10 context continuity, S14 savings, S15 events."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from llm_router.agentic.engine import (
    AgentRunResult,
    MGEEEngine,
    Outcome,
    validate_event_stream,
)
from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger
from llm_router.agentic.savings import compute_savings


@dataclass
class RecordingAgent:
    tier: int
    cost_usd: float = 0.01
    produce: dict[str, Any] = field(default_factory=dict)
    saw: list[list[dict[str, Any]]] = field(default_factory=list)

    def run(self, milestone, frozen_context, budget_left) -> AgentRunResult:
        self.saw.append(list(frozen_context))
        art = {"tier": self.tier, "mid": milestone.id, **self.produce}
        return AgentRunResult(art, self.cost_usd)


def _ledger(ms, cap=10.0):
    return TaskLedger(goal="t", milestones=ms, budget_cap_usd=cap)


# ── S10: milestone N sees N-1's actual artifacts in its packed context ──────
def test_s10_context_continuity_between_milestones():
    def m1_check(a):  # passes and stamps an artifact value later milestones need
        return AcceptanceResult(a.get("tier", -1) >= 0)

    def m2_needs_m1_artifact(a):
        return AcceptanceResult(a.get("tier", -1) >= 0)

    a0 = RecordingAgent(tier=0, produce={"token": "ORANGE-742"})
    ms = [Milestone("M1", "", m1_check), Milestone("M2", "", m2_needs_m1_artifact)]
    res = MGEEEngine({0: a0}).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE

    # when M2 ran, its frozen context carried M1 with the ACTUAL artifact value
    frozen_at_m2 = a0.saw[-1]
    m1_seen = [c for c in frozen_at_m2 if c["id"] == "M1"]
    assert m1_seen and m1_seen[0]["artifacts"].get("token") == "ORANGE-742"


# ── S14: cheap-tier completion records a positive, honest saving ────────────
def test_s14_savings_positive_on_cheap_completion():
    ms = [Milestone(f"M{i}", "", lambda a: AcceptanceResult(True)) for i in range(3)]
    agents = {0: RecordingAgent(tier=0, cost_usd=0.01), 1: RecordingAgent(tier=1)}
    ledger = _ledger(ms)
    MGEEEngine(agents).run(ledger)
    sv = compute_savings(ledger, baseline_cost_per_milestone=0.20)  # premium baseline
    assert sv.actual_usd == pytest.approx(0.03) and sv.baseline_usd == pytest.approx(0.60)
    assert sv.saved_usd > 0 and sv.efficiency > 1


# ── S14b: honest accounting — escalation churn can erase savings ────────────
def test_s14b_savings_honest_under_escalation_churn():
    # a milestone only tier-2 can pass; failed t0/t1 attempts still cost money
    def needs_two(a):
        return AcceptanceResult(a.get("tier", -1) >= 2, "" if a.get("tier", -1) >= 2 else "weak")

    ms = [Milestone("M1", "", needs_two)]
    agents = {t: RecordingAgent(tier=t, cost_usd=0.10) for t in range(3)}
    ledger = _ledger(ms)
    MGEEEngine(agents, max_attempts_per_tier=1).run(ledger)
    sv = compute_savings(ledger, baseline_cost_per_milestone=0.10)
    # actual includes the wasted t0 + t1 attempts → saving is negative, shown honestly
    assert ledger.spent_usd == pytest.approx(0.30) and sv.saved_usd < 0


# ── S15: every transition emits a schema-valid, terminal-closed event stream ─
def test_s15_event_stream_schema_valid_on_complete_and_surface():
    ok = [Milestone("M1", "", lambda a: AcceptanceResult(True))]
    res_ok = MGEEEngine({0: RecordingAgent(0)}).run(_ledger(ok))
    valid, why = validate_event_stream(res_ok.events)
    assert valid, why
    assert res_ok.events[0].kind == "plan" and res_ok.events[-1].kind == "complete"

    bad = [Milestone("M1", "", lambda a: AcceptanceResult(False, "nope"))]
    res_bad = MGEEEngine({0: RecordingAgent(0)}, max_attempts_per_tier=1).run(_ledger(bad))
    valid, why = validate_event_stream(res_bad.events)
    assert valid, why
    assert res_bad.events[-1].kind == "surface"          # closes on surface, not stuck
    assert all(e.to_dict()["kind"] for e in res_bad.events)  # every event serialises
