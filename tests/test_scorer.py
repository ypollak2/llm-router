"""Tests for Phase 5: Unified Scorer (scorer.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.scorer import score_model, score_all_models
from llm_router.types import COMPLEXITY_WEIGHTS, ModelCapability, ProviderTier, TaskType


# ── score_model ───────────────────────────────────────────────────────────────

class TestScoreModel:
    def test_returns_scored_model(self):
        sm = score_model("ollama/qwen3:32b", "code", "simple")
        assert sm.model_id == "ollama/qwen3:32b"
        assert 0.0 <= sm.score <= 1.0

    def test_score_components_in_range(self):
        sm = score_model("openai/gpt-4o", "code", "moderate", pressure=0.5)
        assert 0.0 <= sm.quality_score <= 1.0
        assert 0.0 <= sm.budget_score <= 1.0
        assert 0.0 <= sm.latency_score <= 1.0
        assert 0.0 <= sm.acceptance_score <= 1.0

    def test_zero_pressure_gives_full_budget_score(self):
        sm = score_model("openai/gpt-4o", "code", "moderate", pressure=0.0)
        assert sm.budget_score == pytest.approx(1.0)

    def test_full_pressure_gives_zero_budget_score(self):
        sm = score_model("openai/gpt-4o", "code", "moderate", pressure=1.0)
        assert sm.budget_score == pytest.approx(0.0)

    def test_high_pressure_lowers_score(self):
        low = score_model("ollama/qwen3:32b", "code", "simple", pressure=0.0)
        high = score_model("ollama/qwen3:32b", "code", "simple", pressure=0.95)
        assert low.score > high.score

    def test_local_free_model_high_score_zero_pressure(self):
        """Local model with no budget pressure should score near the top."""
        sm = score_model("ollama/qwen3:32b", "code", "simple", pressure=0.0)
        assert sm.score > 0.8, f"expected high score for free local, got {sm.score}"

    def test_unknown_complexity_falls_back_to_moderate(self):
        sm1 = score_model("ollama/qwen3:32b", "code", "nonexistent_complexity", pressure=0.0)
        sm2 = score_model("ollama/qwen3:32b", "code", "moderate", pressure=0.0)
        assert sm1.score == pytest.approx(sm2.score)

    def test_complex_weights_quality_more_than_simple(self):
        w_simple = COMPLEXITY_WEIGHTS["simple"]
        w_complex = COMPLEXITY_WEIGHTS["complex"]
        assert w_complex.quality > w_simple.quality

    def test_simple_weights_budget_more_than_complex(self):
        w_simple = COMPLEXITY_WEIGHTS["simple"]
        w_complex = COMPLEXITY_WEIGHTS["complex"]
        assert w_simple.budget > w_complex.budget

    def test_quality_model_wins_on_complex_task(self):
        """High-quality model should beat cheap model on complex tasks."""
        # Cheap model: moderate quality, zero pressure
        cheap = score_model("ollama/llama:8b", "code", "complex", pressure=0.0)
        # Premium model: high quality, some pressure
        premium = score_model("ollama/qwen3-coder:32b", "code", "complex", pressure=0.2)
        # qwen3-coder has higher quality score than generic llama
        assert premium.score > cheap.score

    def test_cheap_model_wins_on_simple_task_with_high_premium_pressure(self):
        """When premium model is under budget pressure, cheap free model wins on simple."""
        cheap_free = score_model("ollama/qwen3:32b", "query", "simple", pressure=0.0)
        premium_pressured = score_model("openai/gpt-4o", "query", "simple", pressure=0.9)
        assert cheap_free.score > premium_pressured.score

    def test_failure_rates_reduce_quality_score(self):
        no_failures = score_model("openai/gpt-4o", "code", "moderate",
                                   failure_rates={"openai/gpt-4o": 0.0})
        high_failures = score_model("openai/gpt-4o", "code", "moderate",
                                     failure_rates={"openai/gpt-4o": 0.8})
        assert no_failures.quality_score > high_failures.quality_score

    def test_capability_stored_on_result(self):
        cap = ModelCapability(
            model_id="ollama/qwen3:32b",
            provider="ollama",
            provider_tier=ProviderTier.LOCAL,
            task_types=frozenset({TaskType.CODE}),
        )
        sm = score_model("ollama/qwen3:32b", "code", "simple", capability=cap)
        assert sm.capability is cap

    def test_auto_capability_created_when_none(self):
        sm = score_model("openai/gpt-4o", "code", "simple")
        assert sm.capability is not None
        assert sm.capability.model_id == "openai/gpt-4o"
        assert sm.capability.provider == "openai"

    def test_score_is_weighted_sum_of_components(self):
        """Verify composite score matches the formula manually."""
        pressure = 0.3
        sm = score_model("ollama/qwen3:32b", "code", "simple",
                         pressure=pressure, failure_rates={}, acceptance_scores={})
        weights = COMPLEXITY_WEIGHTS["simple"]
        expected = (
            sm.quality_score * weights.quality
            + sm.budget_score * weights.budget
            + sm.latency_score * weights.latency
            + sm.acceptance_score * weights.acceptance
        )
        assert sm.score == pytest.approx(expected, abs=1e-6)


# ── score_all_models ──────────────────────────────────────────────────────────

class TestScoreAllModels:
    # Patch targets: cost functions are imported locally inside score_all_models,
    # so we patch at source (llm_router.cost) and the budget function at source too.
    _COST_PATCH_BASE = "llm_router.cost"
    _BUDGET_PATCH = "llm_router.budget.get_budget_state"

    @pytest.mark.asyncio
    async def test_returns_sorted_list(self):
        models = ["ollama/qwen3:32b", "ollama/llama3:8b", "openai/gpt-4o"]
        with (
            patch(f"{self._COST_PATCH_BASE}.get_model_failure_rates", new_callable=AsyncMock, return_value={}),
            patch(f"{self._COST_PATCH_BASE}.get_model_latency_stats", new_callable=AsyncMock, return_value={}),
            patch(f"{self._COST_PATCH_BASE}.get_model_acceptance_scores", new_callable=AsyncMock, return_value={}),
            patch(self._BUDGET_PATCH, new_callable=AsyncMock) as mock_budget,
        ):
            from llm_router.types import BudgetState
            mock_budget.return_value = BudgetState(provider="test", pressure=0.0)
            result = await score_all_models(models, "code", "simple")

        assert len(result) == 3
        scores = [sm.score for sm in result]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_empty_models_returns_empty(self):
        result = await score_all_models([], "code", "simple")
        assert result == []

    @pytest.mark.asyncio
    async def test_accepts_capability_objects(self):
        cap = ModelCapability(
            model_id="ollama/qwen3:32b",
            provider="ollama",
            provider_tier=ProviderTier.LOCAL,
            task_types=frozenset({TaskType.CODE}),
        )
        with (
            patch(f"{self._COST_PATCH_BASE}.get_model_failure_rates", new_callable=AsyncMock, return_value={}),
            patch(f"{self._COST_PATCH_BASE}.get_model_latency_stats", new_callable=AsyncMock, return_value={}),
            patch(f"{self._COST_PATCH_BASE}.get_model_acceptance_scores", new_callable=AsyncMock, return_value={}),
            patch(self._BUDGET_PATCH, new_callable=AsyncMock) as mock_budget,
        ):
            from llm_router.types import BudgetState
            mock_budget.return_value = BudgetState(provider="test", pressure=0.0)
            result = await score_all_models([cap], "code", "simple")

        assert len(result) == 1
        assert result[0].model_id == "ollama/qwen3:32b"
        assert result[0].capability is cap

    @pytest.mark.asyncio
    async def test_budget_pressure_affects_ranking(self):
        """Pressured model should rank below free model of equal quality."""
        # Score directly with explicit pressures to avoid network/db
        sm_free = score_model("ollama/qwen3:32b", "code", "simple", pressure=0.0)
        sm_pressured = score_model("ollama/deepseek-coder:6.7b", "code", "simple", pressure=0.95)
        assert sm_free.score > sm_pressured.score

    @pytest.mark.asyncio
    async def test_survives_fetch_exceptions(self):
        """Even if data fetching fails, should return scored models with defaults."""
        models = ["ollama/qwen3:32b"]
        with (
            patch(f"{self._COST_PATCH_BASE}.get_model_failure_rates", side_effect=RuntimeError("db down")),
            patch(f"{self._COST_PATCH_BASE}.get_model_latency_stats", new_callable=AsyncMock, return_value={}),
            patch(f"{self._COST_PATCH_BASE}.get_model_acceptance_scores", new_callable=AsyncMock, return_value={}),
            patch(self._BUDGET_PATCH, new_callable=AsyncMock) as mock_budget,
        ):
            from llm_router.types import BudgetState
            mock_budget.return_value = BudgetState(provider="test", pressure=0.0)
            result = await score_all_models(models, "code", "simple")

        assert len(result) == 1
        assert 0.0 <= result[0].score <= 1.0
