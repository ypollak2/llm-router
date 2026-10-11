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


# ── Follow-up (#396 review): suffixed Anthropic ids ──────────────────────────
# Claude Code and the API can name a model with a release date ("-20260901") or the
# long-context marker ("[1m]"). Before this, both spellings of Haiku 5.5 resolved to
# None: the proxy row got a tier label from the id's words but a null cost (G3).


@pytest.mark.parametrize("model", [
    "claude-haiku-5-5-20260901",
    "claude-haiku-5-5[1m]",
    "claude-haiku-5-5-20260901[1m]",
    "anthropic/claude-haiku-5-5-20260901",
])
def test_suffixed_haiku_5_5_uses_the_tiered_entry(model):
    assert pricing.resolve(model) == H
    assert pricing.rates_per_m(model, prompt_tokens=100_000) == pytest.approx(
        {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125})
    assert pricing.rates_per_m(model, prompt_tokens=100_001) == pytest.approx(
        {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625})
    assert pricing.cache_write_1h_rate(model, prompt_tokens=100_001) == pytest.approx(1.00)


def test_a_date_suffix_resolves_for_every_anthropic_model():
    for key in pricing._ANTHROPIC:
        assert pricing.resolve(f"{key}-20260901") == key, key


def test_exact_matches_win_and_unknown_bases_stay_unknown():
    # The explicit alias still answers for its own spelling.
    assert pricing.resolve("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    # A date on an id this table does not know is still unknown, not a guess.
    assert pricing.resolve("claude-haiku-9-9-20260901") is None
    assert pricing.resolve("gpt-4o-20260901") is None
    # Only an 8-digit date is a date suffix.
    assert pricing.resolve("claude-haiku-5-5-2026") is None


def test_one_m_marker_keeps_its_pre_4_6_guard_after_a_date():
    # Pre-4.6 long context was billed at a premium this table does not carry.
    assert pricing.resolve("claude-sonnet-4-5[1m]") is None
    assert pricing.resolve("claude-sonnet-4-5-20250929[1m]") is None
    assert pricing.resolve("claude-opus-5-5-20260901[1m]") == "claude-opus-5-5"


def test_dated_long_prompt_ledger_row_is_priced_on_the_long_card():
    m = "claude-haiku-5-5-20260901"
    row = {"served_model": m, "requested_model": m,
           "usage": {"input_tokens": 1_000, "output_tokens": 200, "cache_read_input_tokens": 150_000,
                     "cache_creation_input_tokens": 0, "cache_creation_1h": 0, "cache_creation_5m": 0}}
    assert ledger.anthropic_cost(row) == pytest.approx((1_000 * 0.50 + 200 * 2.50 + 150_000 * 0.05) / 1e6)


def test_mythos_5_1_is_priced():
    # Pricing page, "Model pricing" (checked 2026-10-10): "Claude Mythos 5.1 ... | $10 / MTok |
    # $12.50 / MTok | $20 / MTok | $0.25 / MTok<sup>1</sup> | $50 / MTok", footnote 1: "Cache hits
    # and refreshes on Claude Fable 5.1 and Claude Mythos 5.1 are priced at 0.025x the base input price."
    m = "claude-mythos-5-1"
    assert pricing.resolve(m) == m
    assert pricing.rates_per_m(m) == pytest.approx(
        {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50})
    assert pricing.cache_write_1h_rate(m) == pytest.approx(20.00)
    # "Claude 4.6 and later models (except Claude Haiku 5.5) ... include the full 1M token
    # context window at standard pricing."
    assert pricing.resolve(f"{m}[1m]") == m
