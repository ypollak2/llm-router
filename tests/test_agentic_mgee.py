"""P1 deterministic MGEE core — proves the flow with FAKE agents (no real models).

Covers the docs/agentic-router.md §8 matrix: happy path, carry-forward on
escalation, honest surfaced failure, bounded attempts, flaky re-run, budget
surface, DAG independence, and the termination fuzz property.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from llm_router.agentic.engine import AgentRunResult, MGEEEngine, Outcome
from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger


# ── fake agent: quality == its tier; records what it was asked to do ─────────
@dataclass
class FakeAgent:
    tier: int
    cost_usd: float = 0.01
    flaky_first_n: dict[str, int] = field(default_factory=dict)  # mid -> flaky count remaining
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def run(self, milestone, frozen_context, budget_left) -> AgentRunResult:
        seen = tuple(c["id"] for c in frozen_context)
        self.calls.append((milestone.id, seen))
        flaky = self.flaky_first_n.get(milestone.id, 0)
        if flaky > 0:
            self.flaky_first_n[milestone.id] = flaky - 1
            return AgentRunResult({"tier": self.tier, "mid": milestone.id,
                                   "flaky": True, "saw": seen}, self.cost_usd)
        return AgentRunResult({"tier": self.tier, "mid": milestone.id,
                               "saw": seen}, self.cost_usd)


def needs_tier(required: int):
    """Objective acceptance check: passes iff the producing tier >= required."""
    def check(artifacts: dict[str, Any]) -> AcceptanceResult:
        if artifacts.get("flaky"):
            return AcceptanceResult(False, "flaky/non-reproducible", deterministic=False)
        got = artifacts.get("tier", -1)
        ok = got >= required
        return AcceptanceResult(ok, "" if ok else f"tier {got} < required {required}")
    return check


def ladder(*tiers: int) -> dict[int, FakeAgent]:
    return {t: FakeAgent(tier=t) for t in tiers}


def _ledger(milestones, cap=10.0):
    return TaskLedger(goal="t", milestones=milestones, budget_cap_usd=cap)


# ── S1: happy path — all pass at tier 0 ─────────────────────────────────────
def test_s1_happy_path_no_escalation():
    ms = [Milestone(f"M{i}", "", needs_tier(0)) for i in range(3)]
    agents = ladder(0, 1, 2)
    res = MGEEEngine(agents).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert all(m.achieved_by == 0 for m in ms)
    assert not any(e.kind == "escalate" for e in res.events)
    # each milestone executed exactly once
    assert all(sum(1 for c in agents[0].calls if c[0] == m.id) == 1 for m in ms)


# ── S2 + S9: single escalation carries M1 forward; done milestones NOT re-run
def test_s2_s9_escalation_carries_frozen_milestones_forward():
    ms = [Milestone("M1", "", needs_tier(0)), Milestone("M2", "", needs_tier(1))]
    agents = ladder(0, 1, 2)
    res = MGEEEngine(agents, max_attempts_per_tier=1).run(_ledger(ms))

    assert res.outcome is Outcome.COMPLETE
    assert ms[0].achieved_by == 0 and ms[1].achieved_by == 1
    # S9: M1 executed exactly once (never redone after M2 escalated)
    assert sum(1 for c in agents[0].calls if c[0] == "M1") == 1
    # S9: when M2 ran at tier 1, it received M1 in its frozen context
    m2_at_t1 = [c for c in agents[1].calls if c[0] == "M2"]
    assert m2_at_t1 and "M1" in m2_at_t1[0][1]
    assert any(e.kind == "escalate" and e.milestone_id == "M2" for e in res.events)


# ── S3: multi-escalation up to the top tier ─────────────────────────────────
def test_s3_multi_escalation_to_top():
    ms = [Milestone("M1", "", needs_tier(2))]
    res = MGEEEngine(ladder(0, 1, 2), max_attempts_per_tier=1).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert ms[0].achieved_by == 2


# ── S4: top-tier failure surfaces (never stuck), partial ledger preserved ───
def test_s4_top_tier_failure_surfaces_not_stuck():
    ms = [Milestone("M1", "", needs_tier(0)), Milestone("M2", "", needs_tier(9))]
    res = MGEEEngine(ladder(0, 1, 2), max_attempts_per_tier=1).run(_ledger(ms))
    assert res.outcome is Outcome.SURFACED
    assert "required 9" in res.reason
    assert ms[0].achieved_by == 0  # partial progress kept
    assert any(e.kind == "surface" for e in res.events)


# ── S5 + S13: flaky check re-runs once, does not cause a false escalation ────
def test_s5_flaky_check_reruns_without_escalation():
    ms = [Milestone("M1", "", needs_tier(0))]
    agents = {0: FakeAgent(tier=0, flaky_first_n={"M1": 1}), 1: FakeAgent(tier=1)}
    res = MGEEEngine(agents, max_attempts_per_tier=1).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert ms[0].achieved_by == 0  # never escalated to tier 1
    assert not any(e.kind == "escalate" for e in res.events)


# ── S6: bounded attempts — a non-passing tier escalates, never loops forever ─
def test_s6_bounded_attempts_then_escalate():
    ms = [Milestone("M1", "", needs_tier(1))]
    agents = ladder(0, 1)
    res = MGEEEngine(agents, max_attempts_per_tier=3).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    # exactly K attempts at tier 0 before escalation
    assert sum(1 for c in agents[0].calls if c[0] == "M1") == 3
    assert ms[0].achieved_by == 1


# ── S7: budget exhaustion surfaces partial, never hangs, spent ≤ cap ─────────
def test_s7_budget_exhaustion_surfaces_partial():
    ms = [Milestone("M1", "", needs_tier(9))]  # never passes → keeps spending
    agents = {0: FakeAgent(tier=0, cost_usd=0.4), 1: FakeAgent(tier=1, cost_usd=0.4)}
    ledger = _ledger(ms, cap=1.0)
    res = MGEEEngine(agents, max_attempts_per_tier=5).run(ledger)
    assert res.outcome in (Outcome.BUDGET_EXHAUSTED, Outcome.SURFACED)
    assert ledger.spent_usd <= ledger.budget_cap_usd + 0.4  # bounded by one over-charge


# ── S8: a structurally unmeetable milestone BLOCKS (replan deleted, WP-10) ───
def test_s8_unmeetable_milestone_blocks():
    """This test previously supplied replan_fn and asserted the tail was revised
    once. It was the ONLY caller of that parameter anywhere -- run_delegation
    threaded it through and no production code ever passed one, so the behaviour
    it asserted never happened outside this file.

    WP-10 required replan to work end-to-end or be deleted; owner chose deleted.
    The assertion now pins what production actually does when the ladder is
    exhausted: it blocks. Keeping the old test would have gone on describing a
    capability the product does not have.
    """
    ms = [Milestone("M1", "", needs_tier(9))]

    res = MGEEEngine(ladder(0, 1), max_attempts_per_tier=1).run(_ledger(ms))

    assert res.outcome is not Outcome.COMPLETE
    assert not hasattr(MGEEEngine, "replan_fn")


def test_replan_is_not_reachable_from_any_entry_point():
    """Guards re-introduction. The engine's event vocabulary must not name a
    transition nothing can emit -- an unreachable status reads as a supported
    capability to anyone auditing the surface."""
    import inspect

    from llm_router.agentic import delegate, engine

    assert "replan" not in engine.EVENT_KINDS, engine.EVENT_KINDS
    assert "replan_fn" not in inspect.signature(engine.MGEEEngine.__init__).parameters
    assert "replan_fn" not in inspect.signature(delegate.delegate).parameters


# ── S11: irreversible milestone is gated — no auto-freeze without confirmation
def test_s11_irreversible_milestone_gated():
    ms = [Milestone("M1", "merge", needs_tier(0), reversible=False)]
    res = MGEEEngine(ladder(0, 1), gate=lambda _m, _r: False).run(_ledger(ms))
    assert res.outcome is Outcome.SURFACED
    assert "confirmation" in res.reason


# ── S12: DAG — a blocked node doesn't stall a ready independent sibling ──────
def test_s12_dag_independent_sibling_progresses():
    # M2 depends on nothing; M3 depends on M1 (which will block at top). M2 must still run.
    ms = [
        Milestone("M1", "", needs_tier(9)),
        Milestone("M2", "", needs_tier(0)),
        Milestone("M3", "", needs_tier(0), deps=("M1",)),
    ]
    res = MGEEEngine(ladder(0, 1, 2), max_attempts_per_tier=1).run(_ledger(ms))
    # M2 (independent) got done before the run surfaced on M1
    assert any(m.id == "M2" and m.achieved_by == 0 for m in ms)
    assert res.outcome is Outcome.SURFACED  # M1 unmeetable


# ── S16: termination fuzz — random requirements/flakiness ALWAYS terminate ───
@pytest.mark.parametrize("seed", range(40))
def test_s16_always_terminates(seed):
    # deterministic pseudo-random from the seed (no Math.random dependency)
    reqs = [(seed >> i) % 4 for i in range(1, 4)]  # required tiers in 0..3
    ms = [Milestone(f"M{i}", "", needs_tier(r)) for i, r in enumerate(reqs)]
    flaky = {f"M{i}": (seed >> (i + 4)) % 2 for i in range(3)}
    agents = {t: FakeAgent(tier=t, flaky_first_n=dict(flaky)) for t in range(3)}  # top tier 2
    res = MGEEEngine(agents, max_attempts_per_tier=2).run(_ledger(ms, cap=100.0))
    # must always terminate as a real outcome — never hang, never raise
    assert res.outcome in (Outcome.COMPLETE, Outcome.SURFACED, Outcome.BUDGET_EXHAUSTED)
    # any milestone needing tier > 2 (top) can't complete → must be SURFACED
    if all(r <= 2 for r in reqs):
        assert res.outcome is Outcome.COMPLETE
