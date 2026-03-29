"""Tests for freemium tier gating and budget enforcement."""

from unittest.mock import patch

import pytest

from llm_router.types import BudgetExceededError, TaskType


class TestBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_blocks_when_budget_exceeded(self, mock_env, mock_acompletion, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "5.00")

        with patch("llm_router.cost.get_monthly_spend", return_value=5.50):
            with pytest.raises(BudgetExceededError, match="Monthly budget"):
                from llm_router.router import route_and_call
                await route_and_call(TaskType.QUERY, "Hello")

    @pytest.mark.asyncio
    async def test_allows_when_under_budget(self, mock_env, mock_acompletion, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "10.00")

        with patch("llm_router.cost.get_monthly_spend", return_value=3.50):
            from llm_router.router import route_and_call
            resp = await route_and_call(TaskType.QUERY, "Hello")
            assert resp.content == "Mock response"

    @pytest.mark.asyncio
    async def test_no_budget_means_unlimited(self, mock_env, mock_acompletion, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "0")

        from llm_router.router import route_and_call
        resp = await route_and_call(TaskType.QUERY, "Hello")
        assert resp.content == "Mock response"

    @pytest.mark.asyncio
    async def test_budget_exactly_at_limit(self, mock_env, mock_acompletion, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "5.00")

        with patch("llm_router.cost.get_monthly_spend", return_value=5.00):
            with pytest.raises(BudgetExceededError):
                from llm_router.router import route_and_call
                await route_and_call(TaskType.QUERY, "Hello")


class TestTierGating:
    @pytest.mark.asyncio
    async def test_free_tier_blocks_auto_orchestrate(self, mock_env, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_TIER", "free")
        from llm_router.server import llm_orchestrate
        result = await llm_orchestrate("Do a complex analysis")
        assert "Pro tier" in result

    @pytest.mark.asyncio
    async def test_pro_tier_allows_auto_orchestrate(self, mock_env, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_TIER", "pro")

        call_count = 0

        async def _route(task_type, prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            import json
            from llm_router.types import LLMResponse
            if call_count == 1:
                content = json.dumps([
                    {"task_type": "research", "prompt": "Find: {input}"},
                    {"task_type": "generate", "prompt": "Write: {previous_result}"},
                ])
            else:
                content = f"Result {call_count}"
            return LLMResponse(
                content=content, model="openai/gpt-4o",
                input_tokens=100, output_tokens=50,
                cost_usd=0.001, latency_ms=300.0, provider="openai",
            )

        with patch("llm_router.orchestrator.route_and_call", side_effect=_route):
            from llm_router.server import llm_orchestrate
            result = await llm_orchestrate("Do a complex analysis")
            assert "Pro tier" not in result

    @pytest.mark.asyncio
    async def test_free_tier_blocks_long_templates(self, mock_env, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_TIER", "free")
        from llm_router.server import llm_orchestrate
        # research_report has 3 steps — exceeds free tier limit of 2
        result = await llm_orchestrate("AI trends", template="research_report")
        assert "free tier allows up to 2" in result

    @pytest.mark.asyncio
    async def test_free_tier_check_function(self, mock_env, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_TIER", "free")
        from llm_router.server import _check_tier
        assert _check_tier("multi_step") is not None
        assert "Pro tier" in _check_tier("multi_step")
        assert _check_tier("nonexistent_feature") is None

    @pytest.mark.asyncio
    async def test_pro_tier_check_function(self, mock_env, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_TIER", "pro")
        from llm_router.server import _check_tier
        assert _check_tier("multi_step") is None
        assert _check_tier("budget_optimizer") is None


class TestBudgetInUsage:
    @pytest.mark.asyncio
    async def test_usage_shows_budget_status(self, mock_env, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "20.00")

        from llm_router.server import llm_usage
        result = await llm_usage("all")
        assert "MONTHLY BUDGET" in result
        assert "20.00" in result

    @pytest.mark.asyncio
    async def test_usage_hides_budget_when_unlimited(self, mock_env, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "0")

        from llm_router.server import llm_usage
        result = await llm_usage("all")
        assert "Budget Status" not in result
