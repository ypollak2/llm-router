"""Tier classification + savings roll-up — Plan 09 dashboard work.

Groups every routed model into one of three tiers so the session dashboard
can surface "what you actually paid" alongside "what you saved":

* **free_local** — Ollama and other localhost-served models. Zero $ cost,
  unbounded throughput, full data privacy. Each call to a free_local model
  is pure savings vs the paid baseline.
* **free_subscription** — Codex / Gemini CLI / Claude subscription mode.
  Zero $ cost per call (the subscription is paid separately as a flat fee).
  Same savings story as free_local but with a different value prop —
  reliability + capacity is bounded by the subscription tier.
* **paid_api** — any model where each call has a real per-token billing
  hit. OpenAI direct, Anthropic direct, OpenRouter, Perplexity, DeepSeek,
  Groq, xAI, etc.

The "savings" number per tier is the **counterfactual baseline cost minus
the actual cost** — i.e. what you'd have paid had every call in the tier
gone to a Claude Sonnet baseline instead. Free-local and free-subscription
tiers have ``actual_cost = 0`` so their "saved" equals the entire baseline
cost. Paid-api savings show how much routing-to-cheap-models beats
routing-to-Sonnet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Tier = Literal["free_local", "free_subscription", "paid_api"]


# Prefix → tier classification. Order matters only for unknowns (first match
# wins); known providers don't overlap.
_TIER_PREFIXES: dict[str, Tier] = {
    "ollama/": "free_local",
    "ollama:": "free_local",
    "codex/": "free_subscription",
    "codex:": "free_subscription",
    "gemini_cli/": "free_subscription",
    "claude_subscription/": "free_subscription",
    "openai/": "paid_api",
    "anthropic/": "paid_api",
    "google/": "paid_api",   # Google API (not subscription)
    "gemini/": "paid_api",   # Gemini API (not the CLI)
    "openrouter/": "paid_api",
    "perplexity/": "paid_api",
    "deepseek/": "paid_api",
    "groq/": "paid_api",
    "xai/": "paid_api",
    "mistral/": "paid_api",
    "cohere/": "paid_api",
    "together/": "paid_api",
    "huggingface/": "paid_api",
}


def tier_of(model: str) -> Tier:
    """Return the tier for a model identifier (``provider/model`` form).

    Unknown models default to ``"paid_api"`` so the dashboard never silently
    discards costs by misclassifying an unrecognised provider as free.
    Bias toward over-reporting paid usage — under-reporting would mask
    the savings story we're trying to surface.
    """
    for prefix, tier in _TIER_PREFIXES.items():
        if model.startswith(prefix):
            return tier
    return "paid_api"


# Baseline per-1K-output assumption for the savings counterfactual. Anchored
# to Claude Sonnet 4-6 at $15/M output, which is the most realistic "what
# this prompt would cost without llm_router" reference for code / analyze /
# generate flows. Update alongside ``calibration._PRICING_PER_M``.
_SONNET_OUTPUT_PER_K = 0.015
_SONNET_INPUT_PER_K = 0.003

# When a per-model row lacks split input/output token counts (older
# session_spend.json schemas), assume an 80/20 split — input dominates because
# context-prep typically inflates input far more than completion length.
_ASSUMED_INPUT_RATIO = 0.80


def baseline_cost_for_tokens(total_tokens: int) -> float:
    """Estimate what ``total_tokens`` would have cost at the Sonnet baseline.

    Used when a per-model entry only has a lumped ``tokens`` count without
    sub-components. The 80/20 input/output split matches what the cache-aware
    baseline math in :func:`cost.calc_savings` defaults to when called
    without sub-components.
    """
    input_t = int(total_tokens * _ASSUMED_INPUT_RATIO)
    output_t = total_tokens - input_t
    return (input_t * _SONNET_INPUT_PER_K + output_t * _SONNET_OUTPUT_PER_K) / 1000


@dataclass(frozen=True)
class TierRollup:
    """Per-tier aggregate for one session's worth of routing."""

    tier: Tier
    calls: int
    tokens: int
    actual_cost: float
    baseline_cost: float

    @property
    def saved(self) -> float:
        """USD saved vs the baseline counterfactual. SIGNED — negative is a loss.

        AUD-06: this was ``max(0.0, ...)``. Clamping each tier before summing
        made the reported total a sum of wins rather than a net, so a session
        that cost MORE than the counterfactual still displayed a saving. The
        README's own published sample demonstrates it: a Paid-API row worth
        −$0.1010 rendered as $0.0000, and a true net of −$0.0807 rendered as
        +$0.0203.

        A cost optimiser whose output cannot express "this cost you more than
        doing nothing" cannot report the one result that would change a user's
        decision. A non-negative headline must be derived from the signed net
        and labelled as such — never produced by dropping terms.
        """
        return self.baseline_cost - self.actual_cost

    @property
    def savings_ratio(self) -> float:
        """``baseline_cost / actual_cost``. Inf for free tiers; useful for "Nx cheaper" copy."""
        if self.actual_cost <= 0:
            return float("inf")
        return self.baseline_cost / self.actual_cost


def summarize_tiers(per_model: dict[str, dict]) -> list[TierRollup]:
    """Group ``per_model`` (the ``SessionSpend.per_model`` dict) into tier rollups.

    Returns a list in the fixed order ``[free_local, free_subscription, paid_api]``
    so the dashboard always renders the same vertical layout regardless of
    which tiers had activity this session.

    **Free-tier cost enforcement.** Tier classification is the source of truth
    for "should this cost money?" — if the recorded ``cost_usd`` for a model
    classified as ``free_local`` or ``free_subscription`` is non-zero, that's
    data contamination from a pre-v10.1 pricing bug. We pin the actual cost
    at ``0`` for those tiers regardless of what the row says; the savings
    math then reflects the *correct* counterfactual.
    """
    buckets: dict[Tier, dict[str, float]] = {
        "free_local": {"calls": 0.0, "tokens": 0.0, "actual": 0.0, "baseline": 0.0},
        "free_subscription": {"calls": 0.0, "tokens": 0.0, "actual": 0.0, "baseline": 0.0},
        "paid_api": {"calls": 0.0, "tokens": 0.0, "actual": 0.0, "baseline": 0.0},
    }

    for model, row in per_model.items():
        t = tier_of(model)
        calls = float(row.get("calls", 0))
        tokens = float(row.get("tokens", 0))
        cost = float(row.get("cost_usd", 0.0))
        # Truth-by-tier: free tiers have $0 actual cost by definition.
        if t in ("free_local", "free_subscription"):
            cost = 0.0
        buckets[t]["calls"] += calls
        buckets[t]["tokens"] += tokens
        buckets[t]["actual"] += cost
        buckets[t]["baseline"] += baseline_cost_for_tokens(int(tokens))

    return [
        TierRollup(
            tier=tier,
            calls=int(b["calls"]),
            tokens=int(b["tokens"]),
            actual_cost=round(b["actual"], 6),
            baseline_cost=round(b["baseline"], 6),
        )
        for tier, b in buckets.items()
    ]


def render_tier_table(rollups: list[TierRollup]) -> str:
    """Render the tier rollup as a fixed-width Markdown-friendly table.

    Used by the session-end hook and the ``llm_session_savings`` MCP tool.
    Designed to fit a 78-column terminal without wrapping.
    """
    actual, baseline, saved = total_savings(rollups)
    lines = [
        "🧮 Routing Summary — this session",
        "Tier              | Calls | Tokens |   Actual |  Baseline |    Saved",
        "─" * 66,
    ]
    pretty = {
        "free_local":        "Free local",
        "free_subscription": "Free subscription",
        "paid_api":          "Paid API",
    }
    for r in rollups:
        lines.append(
            f"{pretty[r.tier]:<17} | {r.calls:>5} | {r.tokens:>6} | "
            f"${r.actual_cost:>7.4f} | ${r.baseline_cost:>8.4f} | ${r.saved:>7.4f}"
        )
    lines.append("─" * 66)
    tcalls = sum(r.calls for r in rollups)
    ttokens = sum(r.tokens for r in rollups)
    lines.append(
        f"{'TOTAL':<17} | {tcalls:>5} | {ttokens:>6} | "
        f"${actual:>7.4f} | ${baseline:>8.4f} | ${saved:>7.4f}"
    )
    if actual > 0:
        ratio = baseline / actual
        lines.append("")
        if ratio >= 1:
            lines.append(f"Effective savings ratio: {ratio:.2f}× (Sonnet baseline / actual — higher is better)")
        else:
            # ratio < 1 means paid routes cost MORE than the baseline — don't dress
            # an overspend up as savings; state it plainly.
            lines.append(
                f"Effective savings ratio: {ratio:.2f}× — ⚠ paid routes ran "
                f"{1 / ratio:.2f}× OVER the Sonnet baseline this session"
            )
    return "\n".join(lines)


def total_savings(rollups: list[TierRollup]) -> tuple[float, float, float]:
    """Return ``(total_actual, total_baseline, total_saved)`` for the session.

    Total saved is a **signed net**: the sum of per-tier savings where a tier
    that over-spends contributes a negative term. Because ``TierRollup.saved``
    is now signed, this sum equals ``baseline - actual`` by construction.

    AUD-06: this previously summed per-tier values each clamped to >= 0, on the
    rationale that an over-spending tier "should NOT erode" savings on cheap
    tiers. That produces a figure which cannot go negative — so the one result
    a cost optimiser must be able to report ("this cost you more than doing
    nothing") was unrepresentable. If a wins-only figure is wanted for a
    headline, derive it from this net and label it distinctly; do not obtain it
    by discarding terms.
    """
    actual = sum(r.actual_cost for r in rollups)
    baseline = sum(r.baseline_cost for r in rollups)
    saved = sum(r.saved for r in rollups)
    return round(actual, 4), round(baseline, 4), round(saved, 4)
