"""Tests for the smart model selector with budget-aware routing."""

from __future__ import annotations

import pytest

from llm_router.model_selector import select_model, _downshift_amount
from llm_router.types import (
    ClassificationResult, Complexity, QualityMode, TaskType,
)


def _make_classification(complexity: str = "moderate") -> ClassificationResult:
    return ClassificationResult(
        complexity=Complexity(complexity),
        confidence=0.9,
        reasoning="test",
        inferred_task_type=TaskType.QUERY,
        classifier_model="gemini/gemini-2.5-flash",
        classifier_cost_usd=0.0003,
        classifier_latency_ms=200.0,
    )


# ── Downshift threshold tests ────────────────────────────────────────────────


class TestDownshiftAmount:
    def test_no_shift_below_50(self):
        assert _downshift_amount(0.0) == 0
        assert _downshift_amount(0.25) == 0
        assert _downshift_amount(0.49) == 0

    def test_shift_1_between_50_and_80(self):
        assert _downshift_amount(0.50) == 0  # at boundary = no shift
        assert _downshift_amount(0.51) == 1
        assert _downshift_amount(0.79) == 1

    def test_shift_2_between_80_and_95(self):
        assert _downshift_amount(0.80) == 1  # at boundary = previous tier
        assert _downshift_amount(0.81) == 2
        assert _downshift_amount(0.94) == 2

    def test_shift_2_above_95(self):
        assert _downshift_amount(0.95) == 2  # at boundary
        assert _downshift_amount(0.99) == 2
        assert _downshift_amount(1.0) == 2
        assert _downshift_amount(1.5) == 2  # over budget


# ── Base model selection (no budget pressure) ────────────────────────────────


class TestBaseSelection:
    def test_simple_gets_haiku(self):
        rec = select_model(_make_classification("simple"), budget_pct_used=0)
        assert rec.recommended_model == "haiku"
        assert not rec.was_downshifted

    def test_moderate_gets_sonnet(self):
        rec = select_model(_make_classification("moderate"), budget_pct_used=0)
        assert rec.recommended_model == "sonnet"
        assert not rec.was_downshifted

    def test_complex_gets_opus(self):
        rec = select_model(_make_classification("complex"), budget_pct_used=0)
        assert rec.recommended_model == "opus"
        assert not rec.was_downshifted


# ── Budget pressure downshifting ─────────────────────────────────────────────


class TestBudgetPressure:
    def test_complex_at_60pct_downshifts_to_sonnet(self):
        rec = select_model(_make_classification("complex"), budget_pct_used=0.60)
        assert rec.recommended_model == "sonnet"
        assert rec.base_model == "opus"
        assert rec.was_downshifted

    def test_complex_at_85pct_downshifts_to_haiku(self):
        rec = select_model(_make_classification("complex"), budget_pct_used=0.85)
        assert rec.recommended_model == "haiku"
        assert rec.was_downshifted

    def test_moderate_at_60pct_downshifts_to_haiku(self):
        rec = select_model(_make_classification("moderate"), budget_pct_used=0.60)
        assert rec.recommended_model == "haiku"
        assert rec.was_downshifted

    def test_simple_cannot_downshift_below_haiku(self):
        rec = select_model(_make_classification("simple"), budget_pct_used=0.99)
        assert rec.recommended_model == "haiku"

    def test_moderate_at_30pct_no_downshift(self):
        rec = select_model(_make_classification("moderate"), budget_pct_used=0.30)
        assert rec.recommended_model == "sonnet"
        assert not rec.was_downshifted


# ── Quality mode overrides ───────────────────────────────────────────────────


class TestQualityMode:
    def test_best_always_opus(self):
        rec = select_model(
            _make_classification("simple"),
            budget_pct_used=0.90,
            quality_mode=QualityMode.BEST,
        )
        assert rec.recommended_model == "opus"

    def test_conserve_uses_cheaper(self):
        rec = select_model(
            _make_classification("complex"),
            budget_pct_used=0,
            quality_mode=QualityMode.CONSERVE,
        )
        assert rec.recommended_model == "sonnet"  # one below complex's opus

    def test_conserve_respects_min_model(self):
        rec = select_model(
            _make_classification("moderate"),
            budget_pct_used=0,
            quality_mode=QualityMode.CONSERVE,
            min_model="sonnet",
        )
        assert rec.recommended_model == "sonnet"


# ── Minimum model floor ─────────────────────────────────────────────────────


class TestMinModel:
    def test_min_sonnet_prevents_haiku(self):
        rec = select_model(
            _make_classification("simple"),
            budget_pct_used=0,
            min_model="sonnet",
        )
        assert rec.recommended_model == "sonnet"

    def test_min_sonnet_with_budget_pressure(self):
        rec = select_model(
            _make_classification("complex"),
            budget_pct_used=0.90,
            min_model="sonnet",
        )
        # Would downshift to haiku, but floor prevents it
        assert rec.recommended_model == "sonnet"

    def test_min_opus_forces_opus(self):
        rec = select_model(
            _make_classification("simple"),
            budget_pct_used=0.99,
            min_model="opus",
        )
        assert rec.recommended_model == "opus"


# ── Recommendation header ───────────────────────────────────────────────────


class TestRecommendationHeader:
    def test_header_shows_downshift_warning(self):
        rec = select_model(_make_classification("complex"), budget_pct_used=0.70)
        header = rec.header()
        assert "downshifted" in header
        assert "opus" in header

    def test_header_shows_budget_bar(self):
        rec = select_model(_make_classification("simple"), budget_pct_used=0.60)
        header = rec.header()
        assert "60%" in header
        assert "[" in header  # budget bar
