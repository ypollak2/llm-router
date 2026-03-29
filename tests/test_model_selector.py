"""Tests for the smart model selector with budget-aware routing."""

from __future__ import annotations


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
# New thresholds: 0-85% no shift, 85-95% shift 1, 95%+ shift 2


class TestDownshiftAmount:
    def test_no_shift_below_85(self):
        assert _downshift_amount(0.0) == 0
        assert _downshift_amount(0.25) == 0
        assert _downshift_amount(0.50) == 0
        assert _downshift_amount(0.70) == 0
        assert _downshift_amount(0.84) == 0
        assert _downshift_amount(0.85) == 0  # at boundary = no shift

    def test_shift_1_between_85_and_95(self):
        assert _downshift_amount(0.86) == 1
        assert _downshift_amount(0.90) == 1
        assert _downshift_amount(0.94) == 1
        assert _downshift_amount(0.95) == 1  # at boundary

    def test_shift_2_above_95(self):
        assert _downshift_amount(0.96) == 2
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


# ── Budget pressure downshifting (late safety net) ───────────────────────────


class TestBudgetPressure:
    def test_complex_at_60pct_no_downshift(self):
        """Below 85% — complexity routing only, no budget pressure."""
        rec = select_model(_make_classification("complex"), budget_pct_used=0.60)
        assert rec.recommended_model == "opus"
        assert not rec.was_downshifted

    def test_complex_at_90pct_downshifts_to_sonnet(self):
        """85-95% — downshift by 1 tier."""
        rec = select_model(_make_classification("complex"), budget_pct_used=0.90)
        assert rec.recommended_model == "sonnet"
        assert rec.base_model == "opus"
        assert rec.was_downshifted

    def test_complex_at_96pct_downshifts_to_haiku(self):
        """95%+ — downshift by 2 tiers."""
        rec = select_model(_make_classification("complex"), budget_pct_used=0.96)
        assert rec.recommended_model == "haiku"
        assert rec.was_downshifted

    def test_moderate_at_90pct_downshifts_to_haiku(self):
        """85-95% — moderate (sonnet) downshifts by 1 to haiku."""
        rec = select_model(_make_classification("moderate"), budget_pct_used=0.90)
        assert rec.recommended_model == "haiku"
        assert rec.was_downshifted

    def test_simple_cannot_downshift_below_haiku(self):
        rec = select_model(_make_classification("simple"), budget_pct_used=0.99)
        assert rec.recommended_model == "haiku"

    def test_moderate_at_30pct_no_downshift(self):
        rec = select_model(_make_classification("moderate"), budget_pct_used=0.30)
        assert rec.recommended_model == "sonnet"
        assert not rec.was_downshifted

    def test_moderate_at_80pct_no_downshift(self):
        """80% is still below the 85% threshold — no downshift."""
        rec = select_model(_make_classification("moderate"), budget_pct_used=0.80)
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
        rec = select_model(_make_classification("complex"), budget_pct_used=0.90)
        header = rec.header()
        assert "downshifted" in header
        assert "opus" in header

    def test_header_shows_budget_bar(self):
        rec = select_model(_make_classification("simple"), budget_pct_used=0.60)
        header = rec.header()
        assert "60%" in header
        assert "[" in header  # budget bar
