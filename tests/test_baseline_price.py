"""RETROSPECTIVE B-8 — the savings baseline price must be correct and de-staled.

The previous baseline was ``$15/$75`` per 1M tokens labelled "Opus 4.6" — wrong
on two axes: (1) frozen to a version label that drifts as newer Opus ships, and
(2) the *price* was ~3x too high ($15/$75 was the retired Opus-4.1 tier; Opus
4.5+ is $5/$25). Every historical ``saved_usd`` was therefore ~3x inflated.

These tests pin the corrected value AND that it resolves from a single
``LATEST_OPUS_MODEL`` source rather than a hardcoded per-version literal, so a
future Opus release updates one place.
"""
from __future__ import annotations

from llm_router import cost


def test_host_rates_are_current_opus_five_twentyfive():
    # The single most important number: $5/$25, not the stale $15/$75.
    assert cost._HOST_INPUT_PER_M == 5.0
    assert cost._HOST_OUTPUT_PER_M == 25.0


def test_baseline_for_known_tokens_uses_5_25_not_15_75():
    # 1M input + 1M output at Opus rates == $5 + $25 == $30. The stale tier
    # would have produced $90. Pin the ~3x correction directly.
    in_tok = out_tok = 1_000_000
    baseline = (in_tok * cost._HOST_INPUT_PER_M + out_tok * cost._HOST_OUTPUT_PER_M) / 1_000_000
    assert baseline == 30.0
    assert baseline != 90.0  # the old $15/$75 result


def test_rates_derive_from_latest_opus_single_source():
    # Not a frozen literal: the active rates must equal _OPUS_PRICING for the
    # declared latest model. Bumping LATEST_OPUS_MODEL / its price updates both.
    assert cost.LATEST_OPUS_MODEL in cost._OPUS_PRICING
    assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == cost._OPUS_PRICING[cost.LATEST_OPUS_MODEL]


def test_baseline_model_is_latest_opus_not_sonnet():
    # The dead/misleading BASELINE_MODEL_FOR_SAVINGS = "sonnet" is gone; the
    # baseline is (and is named) the latest Opus.
    assert cost.BASELINE_MODEL_FOR_SAVINGS == cost.LATEST_OPUS_MODEL
    assert "opus" in cost.BASELINE_MODEL_FOR_SAVINGS
    assert cost.BASELINE_MODEL_FOR_SAVINGS != "sonnet"


def test_all_known_opus_models_priced_at_5_25():
    # Opus 4.5 onward is $5/$25; none of the map entries carry the stale tier.
    for model, (in_pm, out_pm) in cost._OPUS_PRICING.items():
        assert (in_pm, out_pm) == (5.0, 25.0), model
