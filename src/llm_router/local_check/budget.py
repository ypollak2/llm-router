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
    unpriced_calls: int = 0            # SUCCESSFUL metered calls whose cost could not be priced (spent_usd is then a lower bound)
    failed_calls: int = 0              # attempted calls that returned no usage (timeout, network error, 4xx/5xx); they count toward the call cap
    estimated_usd: float = 0.0         # part of spent_usd that is the conservative per-call estimate charged for failed, sent calls
    log: list[dict] = field(default_factory=list)

    def check(self) -> None:
        if self.calls >= self.cap_calls:
            raise BudgetExhausted(f"call cap reached ({self.calls}/{self.cap_calls})")
        if self.metered:
            if self.unpriced_calls:
                # spent_usd is only a lower bound once a metered call could not be priced: the USD cap can no longer be enforced, so stop.
                raise BudgetExhausted(f"{self.unpriced_calls} metered call(s) could not be priced; the ${self.cap_usd:.2f} cap can no longer be enforced")
            if self.spent_usd >= self.cap_usd:
                raise BudgetExhausted(f"USD cap reached (${self.spent_usd:.2f} of ${self.cap_usd:.2f})")
            if self.est_call_usd is not None and self.spent_usd + self.est_call_usd > self.cap_usd:
                raise BudgetExhausted(f"next call could exceed the USD cap (${self.spent_usd:.2f} spent, ~${self.est_call_usd:.3f}/call, cap ${self.cap_usd:.2f})")

    def record(self, cost_usd: float | None, *, failed: bool = False, sent: bool = True) -> float | None:
        """Count one attempted call (it always counts toward the call cap). Returns the USD charged for it (None = unknown).

        * ``failed=False``: a response was received. ``cost_usd`` is the exact metered cost from its usage; ``None`` means the served model could
          not be priced, which stops the metered budget (``check``).
        * ``failed=True``: no response/usage was received (timeout, network error, 4xx/5xx). That is NOT "unpriceable": the call is counted and,
          if the request may have been sent (``sent=True``), a failed call may still have been billed, so the conservative ``est_call_usd`` is
          charged against the USD cap. ``sent=False`` (client-side failure before the request left) charges nothing. Without an ``est_call_usd``
          nothing can be charged; the call cap still bounds the run.
        """
        self.calls += 1
        charged: float | None = cost_usd
        if failed:
            self.failed_calls += 1
            charged = 0.0
            if self.metered and sent:
                if self.est_call_usd is None:
                    charged = None
                else:
                    charged = self.est_call_usd
                    self.spent_usd += charged
                    self.estimated_usd += charged
        elif self.metered:
            if cost_usd is None:
                self.unpriced_calls += 1
            else:
                self.spent_usd += cost_usd
        self.log.append({"call": self.calls, "cost_usd": charged, "failed": failed})
        return charged

    def summary(self) -> dict:
        return {"calls": self.calls, "cap_calls": self.cap_calls, "metered": self.metered,
                "spent_usd": round(self.spent_usd, 6) if self.metered else None,
                "cap_usd": self.cap_usd if self.metered else None, "unpriced_calls": self.unpriced_calls,
                "failed_calls": self.failed_calls, "estimated_usd": round(self.estimated_usd, 6) if self.metered else None}


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


def recheck_quota(force: bool = False, reader: Callable[[], tuple] | None = None) -> QuotaDecision:
    """Quota re-check for the runner to call between batches (the start-of-run check is one-shot).

    Same thresholds as ``check_subscription_quota``; on "refuse" it raises ``BudgetExhausted`` so the runner stops with the same resumable
    INCONCLUSIVE path as a call/USD cap. "warn"/"ok"/"unknown" are returned for the runner to show; "unknown" never stops the run.
    """
    d = check_subscription_quota(force=force, reader=reader)
    if d.action == "refuse":
        raise BudgetExhausted("subscription quota: " + d.message)
    return d
