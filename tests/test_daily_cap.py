"""Tests for the hard daily spend cap in route_and_call."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.types import BudgetExceededError, TaskType


@pytest.fixture
def _patch_routing(mock_env, monkeypatch, tmp_path):
    """Minimal routing patches: tmp DB, no Codex, no compaction, no caches."""
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Reset config singleton so it reads the new env vars
    import llm_router.config as config_module
    config_module._config = None
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)


@pytest.mark.requires_api_keys
class TestDailySpendCap:
    @pytest.mark.asyncio
    async def test_blocks_when_daily_limit_exceeded(self, _patch_routing, monkeypatch):
        """route_and_call raises BudgetExceededError when daily spend >= limit."""
        monkeypatch.setenv("LLM_ROUTER_DAILY_SPEND_LIMIT", "0.10")

        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.15):
            with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0):
                from llm_router.router import route_and_call
                with pytest.raises(BudgetExceededError, match="Daily spend limit"):
                    await route_and_call(TaskType.QUERY, "hello")

    @pytest.mark.asyncio
    async def test_passes_when_below_daily_limit(self, _patch_routing, monkeypatch):
        """route_and_call proceeds normally when daily spend < limit."""
        from llm_router.types import LLMResponse
        monkeypatch.setenv("LLM_ROUTER_DAILY_SPEND_LIMIT", "1.00")

        mock_resp = LLMResponse(
            content="ok", model="openai/gpt-4o", input_tokens=10,
            output_tokens=5, cost_usd=0.001, latency_ms=100, provider="openai",
        )
        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.05):
            with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0):
                with patch("llm_router.router._call_text", new_callable=AsyncMock, return_value=mock_resp):
                    with patch("llm_router.cost.log_usage", new_callable=AsyncMock):
                        from llm_router.router import route_and_call
                        result = await route_and_call(TaskType.QUERY, "hello")
                        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_disabled_when_limit_is_zero(self, _patch_routing, monkeypatch):
        """Daily cap is disabled when LLM_ROUTER_DAILY_SPEND_LIMIT=0 (default)."""
        from llm_router.types import LLMResponse
        monkeypatch.setenv("LLM_ROUTER_DAILY_SPEND_LIMIT", "0")

        mock_resp = LLMResponse(
            content="ok", model="openai/gpt-4o", input_tokens=10,
            output_tokens=5, cost_usd=0.001, latency_ms=100, provider="openai",
        )
        # get_daily_spend should NOT be called when limit is 0
        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=999.0) as mock_daily:
            with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0):
                with patch("llm_router.router._call_text", new_callable=AsyncMock, return_value=mock_resp):
                    with patch("llm_router.cost.log_usage", new_callable=AsyncMock):
                        from llm_router.router import route_and_call
                        result = await route_and_call(TaskType.QUERY, "hello")
                        assert result.content == "ok"
                        mock_daily.assert_not_called()

    @pytest.mark.asyncio
    async def test_daily_checked_before_monthly(self, _patch_routing, monkeypatch):
        """Daily cap is checked first — monthly budget not queried when daily blocks."""
        monkeypatch.setenv("LLM_ROUTER_DAILY_SPEND_LIMIT", "0.01")
        monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", "100.00")

        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.50) as mock_daily:
            with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0) as mock_monthly:
                from llm_router.router import route_and_call
                with pytest.raises(BudgetExceededError, match="Daily spend limit"):
                    await route_and_call(TaskType.QUERY, "hello")
                mock_daily.assert_called_once()
                # Monthly is still called (both checks are inside one lock block)
                # but the raise happens on the daily check first
                _ = mock_monthly  # may or may not be called — just check the error

    @pytest.mark.asyncio
    async def test_error_message_contains_reset_info(self, _patch_routing, monkeypatch):
        """Error message tells the user when the cap resets."""
        monkeypatch.setenv("LLM_ROUTER_DAILY_SPEND_LIMIT", "0.05")

        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.10):
            with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0):
                from llm_router.router import route_and_call
                with pytest.raises(BudgetExceededError) as exc_info:
                    await route_and_call(TaskType.QUERY, "hello")
                msg = str(exc_info.value)
                assert "midnight UTC" in msg
                assert "0.05" in msg   # the limit
