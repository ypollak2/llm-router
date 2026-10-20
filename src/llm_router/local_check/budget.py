"""local-check judge budget: call/USD cap, cost estimate, subscription quota pressure (LC-4, design section 2).

The cap object is checked BEFORE every judge call; when it would be exceeded it raises ``BudgetExhausted`` so the caller can stop with an
INCONCLUSIVE, resumable state. USD is only counted for the metered API backend; a subscription run is capped by calls (its reported
cost is notional and is not money). A cost that cannot be priced is ``None`` and is never coerced to zero.
No prices live here: every rate comes from ``llm_router.pricing``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

DEFAULT_CAP_USD = 3.0
DEFAULT_CAP_CALLS = 120
PLANT_CALLS = 30                   # 12 good + 12 swapped + 6 mutated (design section 1)
MAX_ANSWER_CALLS = 70              # one pooled draw; programmatic failures are not judged
# Token-count assumptions for the per-call ESTIMATE (not prices): a judge prompt carries the diff (<= ~12k chars), the rubric and the answer.
EST_INPUT_TOKENS = 6000
EST_OUTPUT_TOKENS = 150
EST_MARGIN = 1.30                  # same +30% margin the Q37 prereg used for its estimates
QUOTA_WARN = 0.80
QUOTA_REFUSE = 0.95


class BudgetExhausted(Exception):
    """Raised by ``Budget.check`` when the next call would exceed the call or USD cap."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    cap_usd: float = DEFAULT_CAP_USD
    cap_calls: int = DEFAULT_CAP_CALLS
    metered: bool = True               # True: API backend (USD counts); False: subscription (calls only)
    est_call_usd: float | None = None  # per-call estimate used to look ahead; None = unknown price
    calls: int = 0
    spent_usd: float = 0.0
    unpriced_calls: int = 0            # metered calls whose cost could not be priced (spent_usd is then a lower bound)
    log: list[dict] = field(default_factory=list)

    def check(self) -> None:
        if self.calls >= self.cap_calls:
            raise BudgetExhausted(f"call cap reached ({self.calls}/{self.cap_calls})")
        if self.metered:
            if self.spent_usd >= self.cap_usd:
                raise BudgetExhausted(f"USD cap reached (${self.spent_usd:.2f} of ${self.cap_usd:.2f})")
            if self.est_call_usd is not None and self.spent_usd + self.est_call_usd > self.cap_usd:
                raise BudgetExhausted(f"next call could exceed the USD cap (${self.spent_usd:.2f} spent, ~${self.est_call_usd:.3f}/call, cap ${self.cap_usd:.2f})")

    def record(self, cost_usd: float | None) -> None:
        """Count one attempted call (successful or not: a failed attempt may still be billed or counted against a plan)."""
        self.calls += 1
        if self.metered:
            if cost_usd is None:
                self.unpriced_calls += 1
            else:
                self.spent_usd += cost_usd
        self.log.append({"call": self.calls, "cost_usd": cost_usd})

    def summary(self) -> dict:
        return {"calls": self.calls, "cap_calls": self.cap_calls, "metered": self.metered,
                "spent_usd": round(self.spent_usd, 6) if self.metered else None,
                "cap_usd": self.cap_usd if self.metered else None, "unpriced_calls": self.unpriced_calls}


def planned_calls(n_answers: int = MAX_ANSWER_CALLS, n_plants: int = PLANT_CALLS) -> int:
    return n_plants + n_answers


def per_call_estimate_usd(model: str) -> float | None:
    """Estimated USD for one judge call from the repo's pricing source; ``None`` when the model is not priced there."""
    from llm_router import pricing
    base = pricing.cost_usd(model, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)
    return None if base is None else base * EST_MARGIN


def estimate_run_usd(model: str, n_answers: int = MAX_ANSWER_CALLS, n_plants: int = PLANT_CALLS) -> float | None:
    est = per_call_estimate_usd(model)
    return None if est is None else est * planned_calls(n_answers, n_plants)


@dataclass(frozen=True)
class QuotaDecision:
    action: str                 # "ok" | "warn" | "refuse" | "unknown"
    pressure: float | None      # 0..1 highest subscription limit in use; None when unknown/stale
    message: str


def check_subscription_quota(force: bool = False, reader: Callable[[], tuple] | None = None) -> QuotaDecision:
    """Warn above 80%, refuse above 95% unless ``force``. ``reader`` returns ``(value, state, as_of)`` like ``get_claude_pressure_reading``.

    A missing/stale reading is reported as "unknown" (never as 0%): the run may continue, and the plan screen says the quota could not be read.
    """
    if reader is None:
        from llm_router.claude_usage import get_claude_pressure_reading
        reader = get_claude_pressure_reading
    value, state, _as_of = reader()
    if value is None or state != "ok":
        return QuotaDecision("unknown", None, f"Claude plan usage could not be read ({state}); the judge calls will count toward your plan limit.")
    pct = round(value * 100)
    if value > QUOTA_REFUSE and not force:
        return QuotaDecision("refuse", value, f"Your Claude plan is at {pct}% of its limit; refusing to start a judge run. Re-run with --force to override.")
    if value > QUOTA_WARN:
        return QuotaDecision("warn", value, f"Your Claude plan is at {pct}% of its limit; the judge run will use more of it.")
    return QuotaDecision("ok", value, f"Your Claude plan is at {pct}% of its limit.")
