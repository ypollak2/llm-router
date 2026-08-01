# Ported from Chuzom's bounded_operational.py; env var renamed to
# LLM_ROUTER_BOUNDED_OPERATIONAL; pricing lookup rewired to llm_router's own
# calibration table (llm_router.calibration._lookup_pricing).
"""CF-4: the bounded-operational route -- decision predicate and pricing-derived budget.

A bounded-operational route is a capability-gated hybrid between a pure completion
(no tools) and a full delegation (unbounded milestones/attempts): it is offered only
for a *simple* task that still needs at least one of write/run/verify, and it is
hard-capped (see the ``MAX_BOUNDED_*`` constants) so a wrong guess costs little.

This module intentionally contains ONLY the pure decision + budget logic. It has no
dependency on any delegation-execution engine -- llm-router does not yet have one to
wire it to. ``should_route_bounded`` and ``bounded_op_budget_usd`` are complete,
tested, and ready for a future workstream to call from wherever routing decisions are
made.

The feature is OFF by default (``LLM_ROUTER_BOUNDED_OPERATIONAL`` unset/falsy):
``should_route_bounded`` always returns ``False`` while the flag is off, no matter how
good a candidate the prompt is.
"""
from __future__ import annotations

import math
import os

# Hard caps: a bounded route is intentionally small-blast-radius. These are NOT
# tunable via config -- they are a safety property of the route, not a preference.
MAX_BOUNDED_MILESTONES = 1
MAX_BOUNDED_ATTEMPTS = 2
MAX_BOUNDED_FILE_WRITES = 3
MAX_BOUNDED_COMMANDS = 3

# A tier priced at $0 (e.g. a free local model) still gets a nonzero budget floor so
# there's room for at least one retry/escalation inside the bounded route.
_BUDGET_FLOOR_USD = 0.01

# Generic third-party model names used purely to look up a representative price per
# tier; not brand-specific to any routing product.
_TIER_PRICING_MODEL = {
    0: "gpt-4o-mini",
    1: "gpt-4o-mini",
    2: "gpt-4o",
    3: "claude-sonnet-4-6",
}


def bounded_operational_enabled() -> bool:
    """Whether the bounded-operational route is enabled. Defaults OFF."""
    return os.environ.get("LLM_ROUTER_BOUNDED_OPERATIONAL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def get_model_prices(model_tier: int) -> tuple[float, float]:
    """Return ``(input_price_per_m, output_price_per_m)`` USD for *model_tier*.

    FAIL-OPEN: if pricing lookup fails for any reason, returns a conservative
    default (``0.15``, ``0.60``) rather than raising -- a pricing-lookup failure
    must never block a routing decision.
    """
    try:
        from llm_router.calibration import _lookup_pricing

        model = _TIER_PRICING_MODEL.get(model_tier, _TIER_PRICING_MODEL[1])
        prices = _lookup_pricing(model)
        return float(prices["input"]), float(prices["output"])
    except Exception:  # noqa: BLE001 -- pricing lookup must never break routing
        return 0.15, 0.60


def bounded_op_budget_usd(task_type: str = "", model_tier: int = 1) -> float:
    """A budget cap DERIVED from the tier's real pricing -- never a magic constant.

    Assumes a representative ~2000 input / ~1000 output tokens per attempt, times
    ``MAX_BOUNDED_ATTEMPTS``, rounded up to the nearest cent, floored at
    ``_BUDGET_FLOOR_USD`` so a free/cheap tier still has escalation headroom.
    """
    input_price, output_price = get_model_prices(model_tier)
    per_attempt = (2000 * input_price + 1000 * output_price) / 1_000_000
    raw = per_attempt * MAX_BOUNDED_ATTEMPTS
    return max(_BUDGET_FLOOR_USD, math.ceil(raw * 100) / 100)


def should_route_bounded(prompt: str, complexity: str) -> bool:
    """Decide whether *prompt* (already classified as *complexity*) qualifies for
    the bounded-operational route.

    Requires ALL of:
      * the feature flag is enabled (``LLM_ROUTER_BOUNDED_OPERATIONAL``);
      * complexity is exactly ``"simple"`` (moderate/complex always go through full
        delegation, never bounded);
      * the prompt needs at least one of write_files / run_commands /
        objective_verification.

    Uses ``llm_router.capabilities.detect_capabilities`` (WS4) to obtain the
    capability vector. FAIL-OPEN: any import/lookup failure here conservatively
    returns ``False`` (never bounded), so the route degrades safely to full
    delegation rather than guessing.
    """
    if not bounded_operational_enabled():
        return False
    if complexity != "simple":
        return False
    try:
        from llm_router.capabilities import detect_capabilities

        decision = detect_capabilities(prompt)
        req = decision.required
        return bool(req.write_files or req.run_commands or req.objective_verification)
    except Exception:  # noqa: BLE001 -- undetected capability must never crash routing
        return False
