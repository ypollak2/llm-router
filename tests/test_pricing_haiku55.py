"""Claude Haiku 5.5 pricing: two rate cards by prompt length (docs/BUGS.md HAIKU55-1).

Pricing page, "Model pricing" and "Long context pricing" (checked 2026-10-10): $0.10/$0.50
per MTok for prompts up to 100,000 tokens, $0.50/$2.50 over; the prompt counts input,
cache-read and cache-write tokens; cache rates are the standard ratios of the input rate.
"""

from __future__ import annotations

import pytest

from llm_router import pricing
from llm_router.proxy import ledger

H = "claude-haiku-5-5"


def test_haiku_5_5_is_priced():
    assert pricing.resolve(H) == H
    p = pricing.price_for(H)
    assert (p.input, p.output, p.cache_read_rate, p.cache_write_rate) == pytest.approx((0.10, 0.50, 0.01, 0.125))
    assert pricing.cache_write_1h_rate(H) == pytest.approx(0.20)


def test_long_prompt_rates_apply_strictly_over_100k():
    at = pricing.rates_per_m(H, prompt_tokens=100_000)
    over = pricing.rates_per_m(H, prompt_tokens=100_001)
    assert at == pytest.approx({"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125})
    assert over == pytest.approx({"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625})
    assert pricing.cache_write_1h_rate(H, prompt_tokens=100_001) == pytest.approx(1.00)


def test_cost_usd_counts_cache_tokens_in_the_prompt_length():
    # 1,000 input + 99,500 cache read + 500 cache write = 101,000 > 100K: all at the long card.
    assert pricing.cost_usd(H, 1_000, 200, 99_500, 500) == pytest.approx(
        (1_000 * 0.50 + 200 * 2.50 + 99_500 * 0.05 + 500 * 0.625) / 1e6)
    assert pricing.cost_usd(H, 1_000, 200, 0, 0) == pytest.approx((1_000 * 0.10 + 200 * 0.50) / 1e6)


def test_other_models_ignore_prompt_length():
    for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1"):
        assert pricing.rates_per_m(m, prompt_tokens=900_000) == pricing.rates_per_m(m)


def test_live_burst_row_is_priced():
    """The shape of the 2026-10-09 burst rows: 1h cache write, small prompt."""
    row = {"served_model": H, "requested_model": H,
           "usage": {"input_tokens": 2, "output_tokens": 1211, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 10223, "cache_creation_1h": 10223, "cache_creation_5m": 0}}
    assert ledger.anthropic_cost(row) == pytest.approx((2 * 0.10 + 1211 * 0.50 + 10223 * 0.20) / 1e6)
