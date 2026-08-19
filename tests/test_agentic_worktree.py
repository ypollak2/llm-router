"""P5 — reversibility gate + worktree isolation (fake ops, no real git)."""
from __future__ import annotations

from llm_router.agentic.engine import AgentRunResult, MGEEEngine, Outcome
from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger
from llm_router.agentic.worktree import FakeWorktreeOps, reversibility_gate


def _ledger(ms):
    return TaskLedger(goal="t", milestones=ms, budget_cap_usd=10.0)


class _Agent:
    tier = 0

    def __init__(self, artifacts):
        self._art = artifacts

    def run(self, milestone, frozen_context, budget_left):
        return AgentRunResult(dict(self._art), 0.0)


def test_reversible_milestone_freezes_without_worktree():
    ops = FakeWorktreeOps()
    ms = [Milestone("M1", "", lambda a: AcceptanceResult(True), reversible=True)]
    res = MGEEEngine({0: _Agent({"output": "ok"})}, gate=reversibility_gate(ops)).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert ops.merged == []  # reversible → no worktree merge attempted


def test_irreversible_freezes_only_after_successful_merge():
    ops = FakeWorktreeOps(merge_ok=True)
    ms = [Milestone("M1", "merge PR", lambda a: AcceptanceResult(True), reversible=False)]
    agent = _Agent({"output": "ok", "worktree": "wt-M1"})
    res = MGEEEngine({0: agent}, gate=reversibility_gate(ops)).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE
    assert ops.merged == ["wt-M1"] and ops.discarded == []


def test_irreversible_blocked_when_merge_fails():
    ops = FakeWorktreeOps(merge_ok=False)
    ms = [Milestone("M1", "merge PR", lambda a: AcceptanceResult(True), reversible=False)]
    agent = _Agent({"output": "ok", "worktree": "wt-M1"})
    res = MGEEEngine({0: agent}, gate=reversibility_gate(ops)).run(_ledger(ms))
    assert res.outcome is Outcome.SURFACED           # not stuck — surfaced
    assert "confirmation" in res.reason
    assert ops.merged == ["wt-M1"] and ops.discarded == ["wt-M1"]  # rolled back


def test_irreversible_without_worktree_is_refused():
    ops = FakeWorktreeOps()
    ms = [Milestone("M1", "delete", lambda a: AcceptanceResult(True), reversible=False)]
    agent = _Agent({"output": "ok"})  # no 'worktree' → not isolated
    res = MGEEEngine({0: agent}, gate=reversibility_gate(ops)).run(_ledger(ms))
    assert res.outcome is Outcome.SURFACED
    assert ops.merged == []
