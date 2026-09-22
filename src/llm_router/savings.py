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

__all__ = [
    "net_saved",
    "GROSS_POTENTIAL_RATIONALE",
    "CanonicalSavings",
    "canonical_savings",
    "SURFACES",
    "Surface",
    "label_money",
]


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


# ═══════════════════════════════════════════════════════════════════════════
# R6 / R7 — the canonical FIGURE, not just the canonical subtraction
# ═══════════════════════════════════════════════════════════════════════════
#
# `net_saved` made the arithmetic canonical. It did not make the NUMBER
# canonical, and the 2026-09-22 audit found twenty user-facing savings surfaces
# that would print different dollar amounts from the same database at the same
# instant, for four independent reasons:
#
#   1. PROVENANCE. Twelve of the twenty query SQL directly and apply no
#      `production_only(...)` filter, so one benchmark row inflates exactly
#      those twelve. `dashboard_data.py` — which feeds `llm-router status` and
#      session-end's 14-day panel — contains ZERO references to
#      `production_only`, `is_simulated` or `provenance`.
#   2. BASELINE. Opus in `cost.py` and `dashboard_data.py`; Sonnet in the web
#      dashboard tile and `share.py`; a hardcoded per-model multiplier table in
#      `gain.py`. Same tokens, three different dollar answers.
#   3. "NET" MEANS TWO THINGS, both called net. `get_realized_savings` nets
#      against ROUTING OVERHEAD; `get_lifetime_savings_summary` and
#      session-end's headline net against ACTUAL SPEND with no overhead term.
#   4. TABLE MEMBERSHIP. `savings_stats` only, or `routing_decisions` only, or
#      `usage` only, or a union of four. A free DIRECT route recorded in one is
#      invisible to the surfaces reading the others.
#
# `dashboard_data.py` documents the consequence in its own source: three
# hand-rolled queries once reported **$73.97, $102.31 and $205.19 for the same
# day**. The lesson was written down and the third query was added anyway.
#
# Two things were found that no one had noticed:
#
#   * `cost.get_realized_savings` is the only accessor that is BOTH provenance-
#     filtered and a true net. It reaches exactly ONE surface
#     (`llm_session_dashboard`).
#   * `dashboard_data.query_realized_savings` — the only accessor reading the
#     execution ledger, carrying INV-COST-004 and a docstring explaining why it
#     must never become a fourth independent calculation — has **no caller at
#     all**. It is the same CLASS-A shape as the 58 fail-open writers with zero
#     readers: built, documented, correct, and wired to nothing.

from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class CanonicalSavings:
    """One savings figure, with everything a reader needs to interpret it.

    Every field is here because its absence produced a wrong number somewhere:

    * `baseline_model` — a figure computed against Opus and one against Sonnet
      are not comparable, and neither said which it was.
    * `real_dollars_avoided_usd` vs `baseline_equivalent_avoided_usd` — under a
      Claude subscription no dollars are avoided at all, because the
      subscription is paid either way. Printing "$47.20 saved" to a subscriber
      is not a rounding error, it is a different claim. The subscription gate
      was applied on one of four surfaces.
    * `routing_overhead_usd` — the term that makes the difference between the
      two meanings of "net".
    * `n_rows` — CLAUDE.md: "a rate without its denominator is not a
      measurement". A savings total over three calls is noise.
    * `provenance_filtered` — so a caller can tell a filtered figure from an
      unfiltered one rather than assuming.
    """

    window: str
    baseline_equivalent_avoided_usd: float
    routing_overhead_usd: float
    real_dollars_avoided_usd: float
    baseline_model: str
    n_rows: int
    provenance_filtered: bool
    under_subscription: bool
    source: str

    @property
    def net_avoided_usd(self) -> float:
        """Baseline-equivalent avoided MINUS routing overhead. Signed.

        This is the only meaning of "net" this module recognises. Delegates the
        subtraction to `net_saved` so it cannot be clamped here.
        """
        return net_saved(
            self.baseline_equivalent_avoided_usd, self.routing_overhead_usd
        )

    def headline(self) -> str:
        """The number to show a user, with its qualifier attached. R7.

        Never a bare dollar amount. A figure whose meaning has to be looked up
        elsewhere is a figure that will be quoted without its meaning.
        """
        if self.under_subscription:
            # $0, and why. Under a subscription the money was spent regardless;
            # what routing bought was quota headroom, not cash.
            return (
                f"$0.00 real dollars avoided (subscription: the plan is paid "
                f"either way) · ${self.net_avoided_usd:.2f} baseline-equivalent "
                f"vs {self.baseline_model}, n={self.n_rows}"
            )
        return (
            f"${self.real_dollars_avoided_usd:.2f} real dollars avoided "
            f"vs {self.baseline_model}, n={self.n_rows}"
        )


async def canonical_savings(
    period: str = "today", *, platform: str = "all"
) -> CanonicalSavings:
    """THE savings figure. Every surface that shows a dollar saving calls this.

    Delegates to `cost.get_realized_savings`, which is provenance-filtered and
    computes the true net. This wrapper exists rather than pointing surfaces
    straight at that coroutine because the figure needs the four interpretation
    fields above travelling WITH it — a surface handed a bare float will label
    it from whatever it happens to believe, which is how three baselines ended
    up in one product.
    """
    from llm_router import cost

    raw = await cost.get_realized_savings(period=period, platform=platform)
    gross = float(raw.get("gross_saved_usd", 0.0))
    overhead = float(raw.get("routing_overhead_usd", 0.0))
    n = int(raw.get("n_rows", 0) or 0)

    sub = _under_subscription()
    return CanonicalSavings(
        window=period,
        baseline_equivalent_avoided_usd=gross,
        routing_overhead_usd=overhead,
        # Under a subscription the avoided dollars are ZERO, not the gross
        # figure. Not clamped — computed. `net_saved` still owns the signed
        # subtraction for the baseline-equivalent figure.
        real_dollars_avoided_usd=0.0 if sub else net_saved(gross, overhead),
        baseline_model=_baseline_model(),
        n_rows=n,
        provenance_filtered=True,
        under_subscription=sub,
        source="cost.get_realized_savings",
    )


def _under_subscription() -> bool:
    """Is Claude being paid for by a flat subscription rather than per token?"""
    import os

    raw = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _baseline_model() -> str:
    """The counterfactual this product measures against, named once.

    Hardcoded deliberately: the point of R6 is that the baseline is ONE value,
    and a configurable one would let two surfaces disagree again while both
    claiming to be canonical. Changing it is a code change with a test.
    """
    return "claude-opus-4"


@dataclass(frozen=True)
class Surface:
    """A place a user sees a dollar savings figure."""

    id: str
    where: str
    #: True once the surface reads `canonical_savings`. False records a surface
    #: still computing its own figure — visible and bounded rather than
    #: forgotten.
    canonical: bool
    #: What it does instead, when `canonical` is False. Required: an unmigrated
    #: surface with no stated discrepancy is a surface nobody has looked at.
    divergence: str = ""


#: Every user-facing savings surface found by the 2026-09-22 audit.
#:
#: The registry is the deliverable as much as the migration is. Twenty surfaces
#: grew because nobody could see how many there were; a list that a test
#: enforces means the twenty-first has to join it.
SURFACES: tuple[Surface, ...] = (
    Surface("mcp_session_dashboard", "tools/admin.py:963", True),
    Surface("cli_savings_report", "commands/savings_report.py:100", True,
            "headline from canonical_savings; the per-model free/paid "
            "breakdown still reads savings_stats (the only table carrying it) "
            "but is now provenance-filtered the same way, and the ledger total "
            "is shown BESIDE the canonical figure, never instead of it"),
    Surface("cli_share", "commands/share.py:242", False,
            "SONNET baseline BY DESIGN — the card's framing is 'cheaper than "
            "always-Sonnet', so canonicalising the baseline would change what "
            "it claims, not just what it computes. Now provenance-filtered "
            "(a published number must not carry the project's own benchmark "
            "rows) and every dollar figure states its baseline"),
    Surface("cli_gain", "commands/gain.py:262", False,
            "raw SQL over routing_decisions; no filter; hardcoded per-model "
            "multiplier table as a fourth baseline methodology"),
    Surface("cli_status_premium", "ui/status_premium.py:134", False,
            "dashboard_data.query_window, which has zero provenance-filter "
            "references anywhere in the module"),
    Surface("hook_session_end_headline", "hooks/session-end.py:806", False,
            "raw SQL over usage; no provenance filter; 'Net saved' means net "
            "of SPEND, not of routing overhead"),
    Surface("hook_session_end_cumulative", "hooks/session-end.py:1758", False,
            "dashboard_data.query_window; unfiltered"),
    Surface("hook_session_end_clawcode", "hooks/session-end-clawcode.py:159", False,
            "raw SQL over usage; unfiltered; CLAMPED to >= 0, so it disagrees "
            "with the unclamped headline on the same rows"),
    Surface("hook_status_bar", "hooks/status-bar.py:332", False,
            "raw SQL over usage; unfiltered; clamped"),
    Surface("hook_status_bar_clawcode", "hooks/status-bar-clawcode.py:100", False,
            "raw SQL over usage; unfiltered; clamped"),
    Surface("observability_surface_status", "observability/surface_status.py:391", False,
            "raw SQL over savings_stats, last 2000 rows only; unfiltered"),
    Surface("cli_explain_dashboard", "commands/explain_dashboard.py:225", True,
            "its per-panel figures stay RAW on purpose — this command exists "
            "to show why the panels disagree, and filtering them would hide "
            "the rows causing it. What was missing was a reference to compare "
            "against; it now prints the canonical figure at the top and says "
            "the panels below are deliberately unfiltered"),
    Surface("web_dashboard_tiles", "dashboard/server.py:1039", False,
            "raw SQL over usage; unfiltered; SONNET baseline"),
    Surface("web_dashboard_metrics", "dashboard/server.py:1276", False,
            "cost.get_savings_by_period — filtered, but GROSS and Opus-based"),
    Surface("mcp_llm_savings", "tools/admin.py:672", False,
            "cost.get_savings_by_period — filtered; splits 'Avoided' from "
            "'Real $' on its own axis"),
    Surface("mcp_llm_usage_cache", "tools/admin.py:159", False,
            "cost.get_cache_savings — filtered, gross"),
    Surface("mcp_llm_usage_routing", "tools/admin.py:175", False,
            "cost.get_savings_summary — filtered, gross, claude_usage only"),
    Surface("mcp_llm_usage_lifetime", "tools/admin.py:207", False,
            "cost.get_routing_savings_vs_sonnet — named for Sonnet, baselines "
            "against Opus"),
    Surface("mcp_llm_stream_envelope", "tools/routing.py:621", False,
            "cost.get_lifetime_savings_summary — labelled 'net saved' but nets "
            "against spend, not overhead"),
    Surface("cli_team", "commands/team.py:69", False,
            "cost.get_team_savings — filtered, gross; broadcast to Slack"),
)


def label_money(amount_usd: float, s: CanonicalSavings) -> str:
    """R7. A dollar figure with its qualifier, or nothing.

    There is no way to render a bare amount through this module. That is the
    point: the audit found money strings whose meaning lived only in the head
    of whoever wrote the surface.
    """
    kind = (
        "real dollars avoided"
        if not s.under_subscription
        else "baseline-equivalent avoided (subscription: no cash changed hands)"
    )
    return f"${amount_usd:.2f} {kind} vs {s.baseline_model} (n={s.n_rows})"
