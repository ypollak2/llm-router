"""CF-4: the bounded-operational route — capability-driven hybrid.

The North Star says a routed model must be able to do the REAL work. A *simple* task
that nonetheless needs to write a file or run a command ("add a blank line to README")
cannot be served by an untoolable completion route — but a full MGEE plan+run+verify
loop is overkill for one line. The resolution (§8, Option C) is a bounded single-milestone
tool path selected by CAPABILITY, not by complexity label alone.

This module holds the two shared pieces so ``enforce-route`` (routing) and
``llm_delegate`` (execution) agree on one predicate and one budget:

  * :func:`should_route_bounded` — simple + (write/run/verify) capability + flag on
  * :func:`bounded_op_budget_usd` — a budget DERIVED from model pricing, never a
    magic constant, with a small floor so a $0 local tier still has escalation headroom.

Ships behind ``LLM_ROUTER_BOUNDED_OPERATIONAL`` (default OFF for the first release, §8.2
rollout) — opt-in while metrics accumulate, then flip on.
"""
from __future__ import annotations

import math
import os

# Constraints (§8.3)
MAX_BOUNDED_MILESTONES = 1
MAX_BOUNDED_ATTEMPTS = 2      # one cheap tier + at most one escalation
MAX_BOUNDED_FILE_WRITES = 3
MAX_BOUNDED_COMMANDS = 3
_BUDGET_FLOOR_USD = 0.01      # a $0 local tier still needs headroom for one escalation

# Representative tool-capable model per tier (cheapest first). Local tiers price
# at ~$0, so budget derivation uses a cheap EXTERNAL model to size a cap that can
# absorb one escalation to a paid tier.
#
# WP-03: renamed from _TIER_PRICING_MODEL. It holds model *names* and never held
# a rate, but the name matched the pricing lint's table heuristic, so this file
# was carried as accepted debt despite containing no price. A false positive in
# a baseline looks exactly like a real one, which is how a baseline stops being
# read at all.
_TIER_REFERENCE_MODEL = {
    0: "gpt-4o-mini",   # local is free; size the cap off the first paid escalation tier
    1: "gpt-4o-mini",
    2: "gpt-4o",
    3: "claude-sonnet-4-6",
}


def bounded_operational_enabled() -> bool:
    """Default OFF for the first release (opt-in). Flip the default after
    verified_route_rate > 0 on bounded_operational rows."""
    return os.environ.get("LLM_ROUTER_BOUNDED_OPERATIONAL", "0").strip().lower() in (
        "1", "true", "yes", "on")


def get_model_prices(model_tier: int) -> tuple[float, float]:
    """Return (input_price_per_mtok, output_price_per_mtok) for the representative
    tool-capable model at *model_tier*, from the single calibration price table."""
    model = _TIER_REFERENCE_MODEL.get(int(model_tier), "gpt-4o-mini")
    try:
        from llm_router.calibration import _lookup_pricing
        p = _lookup_pricing(model)
        return float(p["input"]), float(p["output"])
    except Exception:  # noqa: BLE001 — pricing lookup must never break routing
        from llm_router import pricing
        fallback = pricing.price_for("gpt-4o-mini")
        return (fallback.input, fallback.output) if fallback else (0.0, 0.0)


def bounded_op_budget_usd(task_type: str = "", model_tier: int = 1) -> float:
    """Budget cap for a bounded operational route, DERIVED from model pricing.

    Sizing (§8.2): ~2000 input + ~1000 output tokens per attempt, up to 2 attempts,
    rounded up to the cent, floored so a free local tier still has escalation headroom.
    """
    input_price, output_price = get_model_prices(model_tier)
    per_attempt = (2000 * input_price + 1000 * output_price) / 1_000_000
    raw = per_attempt * MAX_BOUNDED_ATTEMPTS
    return max(_BUDGET_FLOOR_USD, math.ceil(raw * 100) / 100)


def should_route_bounded(prompt: str, complexity: str) -> bool:
    """Route to the bounded operational path iff: the flag is on, the task is SIMPLE,
    and it genuinely needs tools (write_files / run_commands / objective_verification).

    A pure Q&A simple task returns False (stays on completion); a moderate/complex task
    returns False (full delegate handles it). Capability, not complexity, is the gate."""
    if not bounded_operational_enabled():
        return False
    if complexity != "simple":
        return False
    try:
        from llm_router.capabilities import detect_capabilities
        req = detect_capabilities(prompt).required
    except Exception:  # noqa: BLE001 — detection failure → conservative (no bounded route)
        return False
    return bool(req.write_files or req.run_commands or req.objective_verification)
