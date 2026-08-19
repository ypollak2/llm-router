"""The one signed savings subtraction. AUD-06's structural fix.

AUD-06: *"TOTAL saved is a sum of wins, not a net — losses clamped to zero before
aggregation."* Invariant I-2, *"unknown/adverse never becomes favourable"*, is
recorded FALSE because of it.

WP-04/WP-05 fixed **one** site — `hooks/session-end.py` — and pinned it with
`tests/economics/test_savings_sign.py`, which loads that one file. Eleven other
surfaces kept the clamp, including `cost.get_team_savings`, whose output
`team.py` broadcasts to Slack/Discord.

WHY A MODULE INSTEAD OF ELEVEN EDITS
------------------------------------
`13_HISTORICAL_DEFECT_PATTERNS.md` records the `$15/$75` price bug being fixed
locally **four separate times** and returning every time, "because no fix was
ever made structural". AUD-06's remediation repeated that: one site fixed, eleven
left. Editing eleven `max(0.0, …)` calls would repeat it a second time — the
twelfth surface someone writes would clamp again, and nothing would notice.

So the fix is a canonical function plus `scripts/lint_savings_sign.py`, which
fails CI on a clamped subtraction in any money module. Same shape as
`tool_surface.py` + CHZ-SURF-01 for tool names, and `net_bind.py` + its
source-level test for public binds: turn "remember not to clamp" into "call the
helper", and let a test fail instead of a user's dashboard.

WHAT THIS IS NOT
----------------
It does not make a negative number *pleasant*. It makes it **visible**. A user
who spent more than the baseline needs to know that, and the clamp is precisely
what stopped them finding out.
"""

from __future__ import annotations

__all__ = ["net_saved", "GROSS_POTENTIAL_RATIONALE"]


def net_saved(baseline_usd: float, actual_usd: float) -> float:
    """``baseline − actual``, **signed**. Negative means routing cost more.

    Trivial by design. The value is not the arithmetic — it is that every money
    surface performs this subtraction in one place that cannot be clamped, and
    that a lint can point at the places that do not use it.

    Args:
        baseline_usd: what the counterfactual (unrouted) path would have cost.
        actual_usd: what was actually spent.

    Returns:
        The net saving. **May be negative**, and callers must render it as such
        — `llm_router.provenance.Measured` exists for figures that also need a
        confidence tag.
    """
    return float(baseline_usd) - float(actual_usd)


#: The single legitimate reason a `max(0, …)` may wrap a savings subtraction:
#: a metric whose DEFINITION is upside-only, paired with a separate signed net.
#: `execution_ledger.potential_savings_usd` is documented as
#: "Σ max(0, baseline_eq − actual) over ALL routes" and sits beside
#: `net_realized_savings_usd`, which is signed. Anything claiming this exemption
#: must (a) say "potential"/"gross" in its own name, and (b) have a signed
#: sibling — otherwise it is AUD-06 wearing a justification.
GROSS_POTENTIAL_RATIONALE = (
    "upside-only metric whose name says so, paired with a signed net field"
)
