"""RETROSPECTIVE B-6/B-7/B-8 — user-facing savings must be honestly labelled.

Guards against the specific labelling regressions we just fixed reappearing:
  - the dashboard priced against Opus but was titled "vs SONNET BASELINE";
  - it advertised efficiency as "than using Sonnet";
  - the baseline was a stale $15/$75 literal.

These are source-level guards on the user-facing surfaces (cheap, fast, and they
fail loudly if someone reintroduces the misleading copy).
"""
from __future__ import annotations

from pathlib import Path

from llm_router import cost

_SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"


def test_dashboard_does_not_mislabel_baseline_as_sonnet():
    txt = (_SRC / "tools" / "admin.py").read_text()
    assert "SAVINGS vs SONNET BASELINE" not in txt
    assert "than using Sonnet for every request" not in txt


def test_dashboard_surfaces_both_figures():
    txt = (_SRC / "tools" / "admin.py").read_text()
    # Both the baseline-avoided and the honest real-$ figure must be shown.
    assert "baseline_avoided_usd" in txt
    assert "real_dollars_avoided_usd" in txt


def test_no_stale_15_75_host_rate_literal_in_cost():
    # The corrected rates live in _OPUS_PRICING; the stale tier must not be the
    # active host rate anywhere in cost.py.
    assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == (5.0, 25.0)
    txt = (_SRC / "cost.py").read_text()
    # A stray "$15/$75" *documented as history* is fine; an active rate literal
    # is not. The active assignment derives from _OPUS_PRICING, so no bare
    # "_HOST_INPUT_PER_M = 15.0" assignment should exist.
    assert "_HOST_INPUT_PER_M = 15.0" not in txt
    assert "_HOST_OUTPUT_PER_M = 75.0" not in txt


def test_real_dollars_helper_exists_and_defaults_conservative(monkeypatch):
    # The honest-accounting switch exists and defaults to subscription (real ≈ $0),
    # never over-claiming cash when the mode is unknown.
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    assert cost._host_is_metered() is False
