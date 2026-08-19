"""Honest savings accounting for a completed/surfaced delegation.

``actual_usd`` is everything the ledger was charged — *including* failed cheap-tier
attempts that later escalated. So churn that eats the gain shows up as low or even
negative savings, never hidden (the audit's honest-accounting lesson, B-7).
"""
from __future__ import annotations

from dataclasses import dataclass

from llm_router.agentic.ledger import TaskLedger


@dataclass(frozen=True)
class Savings:
    actual_usd: float          # everything actually spent, incl. failed attempts
    baseline_usd: float        # cost if every milestone ran once on the baseline tier
    saved_usd: float           # baseline - actual (can be ≤ 0 under heavy escalation)
    efficiency: float          # baseline / actual (∞ when actual == 0)

    def render(self) -> str:
        eff = "∞" if self.efficiency == float("inf") else f"{self.efficiency:.1f}×"
        sign = "+" if self.saved_usd >= 0 else ""
        return f"saved {sign}${self.saved_usd:.4f} ({eff} vs baseline)"


def compute_savings(ledger: TaskLedger, baseline_cost_per_milestone: float) -> Savings:
    """Savings vs running every milestone once on the baseline (premium) tier."""
    actual = ledger.spent_usd
    baseline = len(ledger.milestones) * max(0.0, baseline_cost_per_milestone)
    saved = baseline - actual
    efficiency = (baseline / actual) if actual > 0 else float("inf")
    return Savings(actual, baseline, saved, efficiency)
