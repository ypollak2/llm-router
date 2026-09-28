"""Opus 5.5, Fable 5.1, Sonnet 5.5 and the ``[1m]`` model-id suffix.

Found 2026-09-28 by pricing real Claude Code transcripts: ``claude-opus-5-5``
was absent from the table, so $308.93 of real spend fell through to a fallback
rate; and Claude Code's own cost tracker names models ``<id>[1m]``, which
resolved to nothing at all.

Every expected rate below is copied from
platform.claude.com/docs/en/about-claude/pricing, re-checked 2026-09-28 —
including the two non-standard cache-read multipliers (footnotes 1 and 2).
"""

from __future__ import annotations

import pytest

from llm_router import pricing


@pytest.mark.parametrize(
    ("model", "base_in", "cw_5m", "cw_1h", "cache_read", "out"),
    [
        # Footnote 2: Opus 5.5 cache hits are 0.05x base, NOT 0.1x.
        ("claude-opus-5-5", 4.00, 5.00, 8.00, 0.20, 20.00),
        # Footnote 1: Fable 5.1 cache hits are 0.025x base.
        ("claude-fable-5-1", 10.00, 12.50, 20.00, 0.25, 50.00),
        ("claude-sonnet-5-5", 2.00, 2.50, 4.00, 0.20, 10.00),
        # Already present; re-checked so the whole current line is pinned.
        ("claude-opus-5", 5.00, 6.25, 10.00, 0.50, 25.00),
        ("claude-sonnet-5", 2.00, 2.50, 4.00, 0.20, 10.00),
        ("claude-haiku-4-5", 1.00, 1.25, 2.00, 0.10, 5.00),
        ("claude-fable-5", 10.00, 12.50, 20.00, 1.00, 50.00),
    ],
)
def test_all_five_published_rates(model, base_in, cw_5m, cw_1h, cache_read, out) -> None:
    assert pricing.input_rate(model) == pytest.approx(base_in)
    assert pricing.output_rate(model) == pytest.approx(out)
    assert pricing.cache_write_rate(model) == pytest.approx(cw_5m)
    assert pricing.cache_write_1h_rate(model) == pytest.approx(cw_1h)
    assert pricing.cache_read_rate(model) == pytest.approx(cache_read)


def test_opus_5_5_cache_read_is_not_the_standard_ratio() -> None:
    """The specific trap: 0.1x of $4 is $0.40, double the real $0.20."""
    assert pricing.cache_read_rate("claude-opus-5-5") == pytest.approx(0.20)
    assert pricing.cache_read_rate("claude-opus-5-5") != pytest.approx(0.40)


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-fable-5-1",
    ],
)
def test_1m_suffix_prices_at_base_rates(model: str) -> None:
    """Claude 4.6+ bill the full 1M context at standard rates, no surcharge."""
    assert pricing.resolve(f"{model}[1m]") == model
    assert pricing.rates_per_m(f"{model}[1m]") == pricing.rates_per_m(model)
    assert pricing.cache_write_1h_rate(f"{model}[1m]") == pricing.cache_write_1h_rate(model)


def test_1m_suffix_is_case_and_prefix_tolerant() -> None:
    assert pricing.resolve("anthropic/claude-opus-5-5[1M]") == "claude-opus-5-5"


@pytest.mark.parametrize("model", ["claude-sonnet-4-5", "claude-sonnet-4", "claude-opus-4-5"])
def test_1m_suffix_on_pre_4_6_model_stays_unknown(model: str) -> None:
    """Pre-4.6 long context was billed at a premium the table does not carry.

    Pricing it at base rates would understate it; unknown is the honest answer.
    """
    assert pricing.resolve(f"{model}[1m]") is None
    assert pricing.cost_usd(f"{model}[1m]", 1000, 1000) is None


def test_1m_suffix_on_unknown_model_stays_unknown() -> None:
    assert pricing.resolve("not-a-model[1m]") is None


def test_cost_usd_uses_opus_5_5_cache_read_rate() -> None:
    # 1M cache-read tokens on Opus 5.5 cost exactly $0.20.
    assert pricing.cost_usd("claude-opus-5-5[1m]", cache_read_tokens=1_000_000) == pytest.approx(0.20)


def test_every_long_context_model_is_priced() -> None:
    """A typo in the 4.6+ set would silently make that model's [1m] id unknown."""
    assert pricing._LONG_CONTEXT_AT_STANDARD_RATES <= pricing.known_models()
