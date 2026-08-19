"""v10.1.0 — tier classification + savings dashboard tests.

The tier rollup answers the question users actually care about: "how many
calls did the router send to free models, and how much did that save me?"
The legacy ``session_spend`` JSON only carried per-model rows with raw
``cost_usd``; it did not classify provider tier and did not compute the
counterfactual Sonnet baseline. v10.1 fills that gap.

The tests below pin three contracts so a future refactor cannot silently
break the dashboard:

1. Tier classification — every prefix in the table maps to the expected
   tier, with unknown providers defaulting to ``paid_api`` (safer
   over-reporting than silent zero).
2. Free-tier cost enforcement — even if ``cost_usd`` is contaminated
   (pre-Cat-F data bug), the rollup pins ``actual_cost = 0`` for
   ``free_local`` and ``free_subscription``.
3. Total-saved arithmetic — sum of per-tier savings (each clamped to
   >= 0), not ``baseline - actual``. Prevents an over-spending paid tier
   from eroding the savings reported on free tiers.
"""

from __future__ import annotations

import pytest

from llm_router.tiers import (
    Tier,
    TierRollup,
    render_tier_table,
    summarize_tiers,
    tier_of,
    total_savings,
)


# ── tier_of ────────────────────────────────────────────────────────────────


class TestTierOf:
    """Provider prefix → tier classification."""

    def test_ollama_is_free_local(self):
        assert tier_of("ollama/llama3.2") == "free_local"
        assert tier_of("ollama/qwen3.5:latest") == "free_local"

    def test_codex_and_gemini_cli_are_free_subscription(self):
        assert tier_of("codex/gpt-5.4") == "free_subscription"
        assert tier_of("gemini_cli/gemini-2.5-flash") == "free_subscription"
        assert tier_of("claude_subscription/claude-sonnet-4") == "free_subscription"

    def test_paid_api_providers(self):
        for model in [
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4",
            "openrouter/qwen/qwen3-235b-a22b-2507",
            "perplexity/sonar",
            "deepseek/deepseek-chat",
            "groq/llama-3.3-70b",
            "xai/grok-4.3",
            "mistral/mistral-small",
            "cohere/command-r-plus",
        ]:
            assert tier_of(model) == "paid_api", f"{model} should be paid_api"

    def test_unknown_provider_defaults_to_paid(self):
        """Bias toward over-reporting paid usage — never silently classify
        a new provider as free and lose track of real spend."""
        assert tier_of("brand-new-provider/some-model") == "paid_api"

    def test_gemini_api_vs_gemini_cli(self):
        """Critical — these are different billing surfaces."""
        # Gemini API (paid per-token)
        assert tier_of("gemini/gemini-2.5-flash") == "paid_api"
        # Gemini CLI (subscription)
        assert tier_of("gemini_cli/gemini-2.5-flash") == "free_subscription"


# ── summarize_tiers ────────────────────────────────────────────────────────


class TestSummarizeTiers:
    """Per-model rollup → tier aggregates."""

    def test_returns_three_tiers_in_fixed_order(self):
        out = summarize_tiers({})
        assert [r.tier for r in out] == ["free_local", "free_subscription", "paid_api"]

    def test_aggregates_calls_tokens_and_cost(self):
        per_model = {
            "openai/gpt-4o": {"calls": 5, "tokens": 1000, "cost_usd": 0.05},
            "openai/gpt-4o-mini": {"calls": 10, "tokens": 2000, "cost_usd": 0.01},
            "ollama/llama3.2": {"calls": 3, "tokens": 500, "cost_usd": 0.0},
        }
        out = summarize_tiers(per_model)
        by_tier: dict[Tier, TierRollup] = {r.tier: r for r in out}
        assert by_tier["paid_api"].calls == 15
        assert by_tier["paid_api"].tokens == 3000
        assert by_tier["paid_api"].actual_cost == 0.06
        assert by_tier["free_local"].calls == 3

    def test_free_local_actual_cost_pinned_to_zero(self):
        """Even with contaminated cost_usd data from the pre-Cat-F bug,
        the free tier reports zero — tier classification is the source of
        truth for "should this cost money?"."""
        per_model = {
            "ollama/llama3.2": {"calls": 20, "tokens": 1400, "cost_usd": 0.020},
        }
        out = summarize_tiers(per_model)
        free_local = next(r for r in out if r.tier == "free_local")
        assert free_local.actual_cost == 0.0
        assert free_local.calls == 20
        # Baseline is still computed from the token count
        assert free_local.baseline_cost > 0.0
        assert free_local.saved == free_local.baseline_cost

    def test_free_subscription_actual_cost_pinned_to_zero(self):
        per_model = {
            "codex/gpt-5.4": {"calls": 25, "tokens": 1474, "cost_usd": 99.0},
        }
        out = summarize_tiers(per_model)
        sub = next(r for r in out if r.tier == "free_subscription")
        assert sub.actual_cost == 0.0
        # Saved = full baseline, despite the absurd contaminated cost
        assert sub.saved > 0.0


# ── TierRollup.saved + total_savings ────────────────────────────────────────


class TestSavingsMath:
    """The numbers users actually read off the dashboard."""

    def test_saved_is_signed_and_reports_a_loss(self):
        """A tier that over-spends its baseline reports a NEGATIVE saving.

        INVERTED (AUD-06). This previously asserted ``r.saved == 0.0`` and
        documented the clamp as intended behaviour — it encoded the defect,
        which is worse than no test at all: it actively defended the bug, and
        any mutation restoring the clamp would have passed silently.
        """
        r = TierRollup(
            tier="paid_api",
            calls=10, tokens=1000,
            actual_cost=0.50, baseline_cost=0.10,
        )
        assert r.saved == pytest.approx(-0.40), "over-spend must surface as a loss, not zero"

    def test_total_saved_is_a_net_not_a_sum_of_wins(self):
        """Total saved nets losses against gains.

        INVERTED (AUD-06). The old version asserted the opposite — that an
        over-spending paid tier "must NOT erode" savings on free tiers — which
        is precisely how the README's published sample shows +$0.0203 for a
        session that actually cost $0.0807 more than the counterfactual.
        """
        rollups = [
            TierRollup("free_local",        21, 1400,  0.0, 0.0076),
            TierRollup("free_subscription", 25, 1474,  0.0, 0.0080),
            TierRollup("paid_api",          92, 5884, 0.0563, 0.0318),
        ]
        actual, baseline, saved = total_savings(rollups)
        assert actual == 0.0563
        assert baseline == round(0.0076 + 0.0080 + 0.0318, 4)
        # Net: 0.0076 + 0.0080 + (0.0318 - 0.0563) = -0.0089
        assert saved == pytest.approx(round(baseline - actual, 4), abs=1e-4)
        assert saved < 0, "paid over-spend must pull the total negative"

    def test_readme_published_sample_reports_a_loss(self):
        """The exact figures shipped in README.md.

        Actual $0.1735 vs baseline $0.0928 is a real net loss of $0.0807. The
        README rendered it as +$0.0203 — the sum of the two positive rows — and
        that sample shipped as marketing copy without anyone noticing, which is
        the strongest evidence that nobody was reconstructing these numbers.
        """
        rollups = [
            TierRollup("free_local",        16,   240, 0.0000, 0.0013),
            TierRollup("free_subscription",  5,  3516, 0.0000, 0.0190),
            TierRollup("paid_api",          27, 13421, 0.1735, 0.0725),
        ]
        _actual, _baseline, saved = total_savings(rollups)
        assert saved == pytest.approx(-0.0807, abs=1e-4), (
            "the README sample is a $0.0807 LOSS; +$0.0203 is the sum of wins"
        )

    def test_total_saved_includes_paid_when_paid_actually_saves(self):
        """Sanity check the inverse: when paid tier IS cheaper than Sonnet,
        its savings DO contribute to the total."""
        rollups = [
            TierRollup("paid_api", 10, 1000, actual_cost=0.05, baseline_cost=0.10),
        ]
        actual, baseline, saved = total_savings(rollups)
        assert saved == 0.05

    def test_savings_ratio_inf_for_free(self):
        """Free tiers have infinite savings ratio — useful copy ("∞×
        cheaper") but render code must guard against printing it."""
        r = TierRollup("free_local", 5, 500, actual_cost=0.0, baseline_cost=0.01)
        assert r.savings_ratio == float("inf")


# ── render_tier_table ──────────────────────────────────────────────────────


class TestRenderTierTable:
    """Surface contract for the human-readable table."""

    def test_render_contains_all_three_tier_labels(self):
        out = render_tier_table(summarize_tiers({"ollama/qwen": {"calls": 1, "tokens": 100, "cost_usd": 0}}))
        assert "Free local" in out
        assert "Free subscription" in out
        assert "Paid API" in out
        assert "TOTAL" in out

    def test_render_includes_header_emoji(self):
        out = render_tier_table([])
        assert "🧮" in out
        # Empty rollup still renders the table skeleton
        assert "TOTAL" in out

    def test_render_omits_ratio_when_no_paid_spend(self):
        """``Effective savings ratio`` only renders when actual > 0 (avoids
        a divide-by-zero AND avoids "inf×" copy on free-only sessions)."""
        rollups = [
            TierRollup("free_local", 5, 500, actual_cost=0.0, baseline_cost=0.01),
            TierRollup("free_subscription", 5, 500, actual_cost=0.0, baseline_cost=0.01),
            TierRollup("paid_api", 0, 0, actual_cost=0.0, baseline_cost=0.0),
        ]
        out = render_tier_table(rollups)
        assert "Effective savings ratio" not in out

    def test_render_shows_ratio_when_paid_spent(self):
        rollups = [
            TierRollup("free_local", 0, 0, 0.0, 0.0),
            TierRollup("free_subscription", 0, 0, 0.0, 0.0),
            TierRollup("paid_api", 10, 1000, actual_cost=0.05, baseline_cost=0.10),
        ]
        out = render_tier_table(rollups)
        assert "Effective savings ratio" in out
        assert "2.00×" in out  # baseline 0.10 / actual 0.05 = 2.0
