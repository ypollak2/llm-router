"""AC-7 / INV-COST-004 regression: statusline HUD session savings.

The live statusline HUD accumulated ``total_saved_session`` only when
``render_hud`` was given a ``baseline_cost`` — but the sole production caller
(``record_routing_decision`` via ``tools/routing.py``) never supplied one, so the
HUD showed a PERMANENT $0 saved. The fix threads the task-aware Claude baseline
(computed with the same canonical ``cost`` functions ``log_usage`` persists with)
into ``record_routing_decision``.

Binds to: AC-7 (dead/broken accounting surface), INV-COST-004 (surfaces use one
canonical baseline).
"""
from __future__ import annotations

import pytest

from llm_router import statusline_hud


@pytest.fixture(autouse=True)
def _clean_hud():
    # clear_hud() only flips `enabled`; the session totals persist by design, so
    # fully reset the module global for per-test isolation.
    statusline_hud._statusline_state = statusline_hud.StatuslineState()
    yield
    statusline_hud._statusline_state = statusline_hud.StatuslineState()


def test_record_routing_decision_without_baseline_saves_zero():
    """Documents the AC-7 bug shape: with no baseline_cost, savings stay $0."""
    statusline_hud.record_routing_decision(
        model="haiku", confidence=0.9, task="code/simple", cost=0.001,
    )
    assert statusline_hud.get_session_summary()["total_saved"] == 0.0


def test_record_routing_decision_with_baseline_accumulates_savings():
    """Pass-after: a supplied baseline makes session savings non-zero and exact.

    Before the fix ``record_routing_decision`` had no ``baseline_cost`` parameter,
    so this call raised TypeError (fail-before). After, saved == baseline − cost.
    """
    statusline_hud.record_routing_decision(
        model="haiku", confidence=0.9, task="code/simple", cost=0.001,
        baseline_cost=0.030,
    )
    summary = statusline_hud.get_session_summary()
    assert summary["total_saved"] == pytest.approx(0.029, abs=1e-9)


def test_savings_accumulate_across_decisions():
    """Multiple routed calls sum their individual (baseline − cost) savings."""
    statusline_hud.record_routing_decision(
        model="haiku", confidence=0.9, task="query/simple", cost=0.001,
        baseline_cost=0.011,
    )
    statusline_hud.record_routing_decision(
        model="gpt-4o-mini", confidence=0.8, task="code/moderate", cost=0.002,
        baseline_cost=0.022,
    )
    summary = statusline_hud.get_session_summary()
    # (0.011 - 0.001) + (0.022 - 0.002) = 0.010 + 0.020 = 0.030
    assert summary["total_saved"] == pytest.approx(0.030, abs=1e-9)
