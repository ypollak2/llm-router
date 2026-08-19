"""Pricing has exactly one source of truth, and it is correct.

Regression tests for the audit's economics cluster (RED2-01, RED8-01, RED8-03,
RED8-04, RED8-07). Five independent pricing tables disagreed; three of them fed
user-visible savings figures using a $15/$75 Opus rate that is 3x the real one.

Two properties are asserted here, and the second is the one that actually
prevents recurrence:

1. The numbers are right (checked against published rates).
2. **A wrong number fails a test.** Before this file, mutating the Opus input
   rate to 999.0 produced zero test failures across the entire suite — the
   proven blind spot from the audit's mutation testing. That is what
   ``TestMutationGate`` closes.

Dates are always passed explicitly. A pricing test that depends on the wall
clock starts failing on a date nobody chose.
"""

from __future__ import annotations

import datetime as dt

import pytest

from llm_router import pricing

# A date inside Sonnet 5's introductory window, and one after it.
DURING_INTRO = dt.date(2026, 8, 11)
AFTER_INTRO = dt.date(2026, 9, 15)


class TestPublishedRates:
    """Against Anthropic's published per-Mtok rates."""

    @pytest.mark.parametrize(
        ("model", "expected_in", "expected_out"),
        [
            ("claude-opus-5", 5.00, 25.00),
            ("claude-opus-4-8", 5.00, 25.00),
            ("claude-opus-4-7", 5.00, 25.00),
            ("claude-opus-4-6", 5.00, 25.00),
            ("claude-sonnet-4-6", 3.00, 15.00),
            ("claude-haiku-4-5", 1.00, 5.00),
            ("claude-fable-5", 10.00, 50.00),
        ],
    )
    def test_rate(self, model: str, expected_in: float, expected_out: float) -> None:
        assert pricing.input_rate(model, as_of=DURING_INTRO) == expected_in
        assert pricing.output_rate(model, as_of=DURING_INTRO) == expected_out

    def test_opus_is_not_the_retired_opus_3_rate(self) -> None:
        """The specific regression: $15/$75 is Opus 3, retired 2026-01-05.

        This bug was fixed locally four times and returned every time. It is
        called out by name so a future reintroduction fails with an obvious
        message rather than a bare equality mismatch.
        """
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-6"):
            assert pricing.input_rate(model) != 15.00, f"{model}: Opus 3 rate reintroduced"
            assert pricing.output_rate(model) != 75.00, f"{model}: Opus 3 rate reintroduced"


class TestOneRatePerModel:
    """The structural property. Previously four tables gave Haiku four prices."""

    def test_haiku_has_exactly_one_price_across_every_spelling(self) -> None:
        spellings = [
            "haiku",
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",
            "anthropic/claude-haiku-4-5",
        ]
        rates = {pricing.input_rate(s) for s in spellings}
        assert rates == {1.00}, f"Haiku priced {len(rates)} different ways: {rates}"

    def test_opus_has_exactly_one_price_across_every_spelling(self) -> None:
        rates = {pricing.input_rate(s) for s in ["opus", "claude-opus-5", "anthropic/claude-opus-5"]}
        assert rates == {5.00}

    def test_aliases_resolve_to_a_model_id_and_carry_no_price(self) -> None:
        """An alias that carries its own price is how families drift apart."""
        assert pricing.resolve("opus") == "claude-opus-5"
        assert pricing.resolve("opus") in pricing.known_models()
        assert "opus" not in pricing.known_models()


class TestUnknownIsNotZero:
    """Unknown must stay unknown. Coercing to zero manufactures savings."""

    def test_unknown_model_prices_to_none(self) -> None:
        assert pricing.price_for("some-model-that-does-not-exist") is None
        assert pricing.input_rate("some-model-that-does-not-exist") is None

    def test_unknown_model_costs_none_not_zero(self) -> None:
        cost = pricing.cost_usd("some-model-that-does-not-exist", 1000, 1000)
        assert cost is None, "unknown price became a number; savings would be fabricated"

    def test_unknown_is_not_free(self) -> None:
        assert pricing.is_free("some-model-that-does-not-exist") is False

    def test_local_is_genuinely_free(self) -> None:
        """Zero is data. Ollama really does cost nothing."""
        assert pricing.is_free("ollama") is True
        assert pricing.cost_usd("qwen2.5-coder:7b", 10_000, 10_000) == 0.0


class TestCacheRatesAreDerived:
    """Derived from input, so they cannot drift away from it."""

    def test_sonnet_matches_previously_correct_hand_values(self) -> None:
        """Cross-check on the one table that had these right: 0.30 / 3.75."""
        assert pricing.cache_read_rate("claude-sonnet-4-6") == pytest.approx(0.30)
        assert pricing.cache_write_rate("claude-sonnet-4-6") == pytest.approx(3.75)

    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-haiku-4-5", "claude-sonnet-4-6"])
    def test_ratios_hold(self, model: str) -> None:
        rate = pricing.input_rate(model)
        assert pricing.cache_read_rate(model) == pytest.approx(rate * 0.10)
        assert pricing.cache_write_rate(model) == pytest.approx(rate * 1.25)


class TestTimeDependentPricing:
    def test_intro_rate_applies_during_the_window(self) -> None:
        assert pricing.input_rate("claude-sonnet-5", as_of=DURING_INTRO) == 2.00
        assert pricing.output_rate("claude-sonnet-5", as_of=DURING_INTRO) == 10.00

    def test_standard_rate_applies_after(self) -> None:
        assert pricing.input_rate("claude-sonnet-5", as_of=AFTER_INTRO) == 3.00
        assert pricing.output_rate("claude-sonnet-5", as_of=AFTER_INTRO) == 15.00


class TestCostArithmetic:
    def test_known_quantity(self) -> None:
        # 1M in + 1M out on Opus = $5 + $25
        assert pricing.cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(30.00)

    def test_cache_tokens_are_counted(self) -> None:
        # 1M cache reads on Opus = 1M * 0.50 = $0.50
        assert pricing.cost_usd("claude-opus-5", cache_read_tokens=1_000_000) == pytest.approx(0.50)

    def test_zero_tokens_costs_zero_not_none(self) -> None:
        """A real call with no tokens costs zero. Distinct from unknown."""
        assert pricing.cost_usd("claude-opus-5") == 0.0


class TestStaleness:
    def test_fresh_table_is_not_stale(self) -> None:
        assert pricing.is_stale(as_of=pricing.PRICES_AS_OF) is False

    def test_old_table_is_stale(self) -> None:
        old = pricing.PRICES_AS_OF + dt.timedelta(days=pricing.STALENESS_DAYS + 1)
        assert pricing.is_stale(as_of=old) is True

    def test_unverified_rates_are_declared(self) -> None:
        """Provenance is not accuracy. Carried-forward rates say so."""
        unverified = pricing.unverified_models()
        assert "o3" in unverified
        # Anthropic rates were confirmed against published pricing.
        assert not any(m.startswith("claude-") for m in unverified)


class TestMutationGate:
    """The gate that was missing.

    Audit mutation testing set the Opus input rate to 999.0 and **zero tests
    failed** across the whole suite. These assertions are what make that
    mutation fail — they are deliberately redundant with the rate tests above,
    because a gate whose only guard is one equality check is one refactor away
    from silently disappearing.
    """

    def test_opus_rate_is_within_a_sane_band(self) -> None:
        rate = pricing.input_rate("claude-opus-5")
        assert 1.0 <= rate <= 20.0, f"Opus input rate {rate} is not a plausible per-Mtok price"

    def test_every_known_model_has_plausible_rates(self) -> None:
        for model in pricing.known_models():
            p = pricing.price_for(model)
            assert p is not None
            assert 0.0 <= p.input <= 100.0, f"{model}: implausible input rate {p.input}"
            assert 0.0 <= p.output <= 500.0, f"{model}: implausible output rate {p.output}"

    def test_output_costs_at_least_as_much_as_input(self) -> None:
        """True of every current model, and a cheap way to catch a swap."""
        for model in pricing.known_models():
            p = pricing.price_for(model)
            assert p is not None
            assert p.output >= p.input, f"{model}: output rate below input rate — fields swapped?"

    def test_relative_ordering_holds(self) -> None:
        """Haiku < Sonnet < Opus < Fable. Catches a value in the wrong row."""
        haiku = pricing.input_rate("claude-haiku-4-5")
        sonnet = pricing.input_rate("claude-sonnet-4-6")
        opus = pricing.input_rate("claude-opus-5")
        fable = pricing.input_rate("claude-fable-5")
        assert haiku < sonnet < opus < fable
