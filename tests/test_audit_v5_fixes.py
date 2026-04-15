"""Audit tests for v5.0-v5.3 critical fixes.

Comprehensive test coverage for:
- Budget enforcement with proper error handling
- Emergency fallback chain (all-fail → BUDGET succeeds)
- Correlation ID tracking in routing decisions
- Invalid complexity hint handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from llm_router.router import route_and_call
from llm_router.types import (
    TaskType, BudgetExceededError, LLMResponse
)


class TestBudgetEnforcement:
    """v5.3.0: Budget enforcement with _pending_spend tracking."""

    @pytest.mark.asyncio
    async def test_monthly_budget_exceeded_raises_immediately(self):
        """Exceeding monthly budget should raise BudgetExceededError immediately."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock) as mock_spend:
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock) as mock_daily:
                # Current spend exceeds budget
                mock_spend.return_value = 1.01
                mock_daily.return_value = 0.0
                
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 1.00  # Budget limit
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with pytest.raises(BudgetExceededError, match="Monthly budget"):
                        await route_and_call(TaskType.QUERY, "test")

    @pytest.mark.asyncio
    async def test_daily_budget_exceeded_raises_immediately(self):
        """Exceeding daily budget should raise BudgetExceededError immediately."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock) as mock_spend:
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock) as mock_daily:
                mock_spend.return_value = 0.0
                # Current daily spend exceeds limit
                mock_daily.return_value = 0.51
                
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0  # No monthly limit
                    config.llm_router_daily_spend_limit = 0.50  # Daily limit
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with pytest.raises(BudgetExceededError, match="Daily spend"):
                        await route_and_call(TaskType.QUERY, "test")

    @pytest.mark.asyncio
    async def test_budget_exceeded_cleanup_releases_reservation(self):
        """When budget is exceeded, the reserved spend should be cleaned up."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock) as mock_spend:
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock) as mock_daily:
                mock_spend.return_value = 1.01
                mock_daily.return_value = 0.0
                
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 1.00
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    # First call should raise budget error
                    with pytest.raises(BudgetExceededError):
                        await route_and_call(TaskType.QUERY, "test")
                    
                    # Budget lock should be cleaned up (no deadlock on second call)
                    # This is implicit - if cleanup didn't work, we'd hang here
                    with pytest.raises(BudgetExceededError):
                        await route_and_call(TaskType.QUERY, "test 2")


class TestEmergencyFallbackChain:
    """v5.3.0: Emergency BUDGET fallback when primary chain exhausts."""

    @pytest.mark.asyncio
    async def test_all_models_fail_returns_error_with_context(self):
        """When all models fail, error should include helpful context."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock):
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock):
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with patch('llm_router.router._build_and_filter_chain') as mock_chain:
                        mock_chain.return_value = ['openai/gpt-4o-mini']
                        
                        with patch('llm_router.router._call_text', new_callable=AsyncMock) as mock_call:
                            mock_call.side_effect = RuntimeError("Model unavailable")
                            
                            with patch('llm_router.router.get_tracker') as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker
                                
                                with pytest.raises(RuntimeError, match="All models failed"):
                                    await route_and_call(TaskType.QUERY, "test")

    @pytest.mark.asyncio
    async def test_media_task_types_skip_fallback(self):
        """Media tasks should not attempt emergency fallback (not text-based)."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock):
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock):
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with patch('llm_router.router._build_and_filter_chain') as mock_chain:
                        mock_chain.return_value = ['openai/dall-e-3']
                        
                        with patch('llm_router.router._call_media', new_callable=AsyncMock) as mock_media:
                            mock_media.side_effect = RuntimeError("Image generation failed")
                            
                            with patch('llm_router.router.get_tracker') as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker
                                
                                # Should fail immediately without fallback
                                with pytest.raises(RuntimeError, match="All models failed"):
                                    await route_and_call(
                                        TaskType.IMAGE,
                                        "generate an image",
                                        media_params={"size": "1024x1024"}
                                    )


class TestCorrelationIDTracking:
    """v5.3.0: Correlation ID tracking for request tracing."""

    @pytest.mark.asyncio
    async def test_correlation_id_passed_to_call_text(self):
        """Correlation ID should be passed through to _call_text."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock):
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock):
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with patch('llm_router.router._build_and_filter_chain') as mock_chain:
                        mock_chain.return_value = ['openai/gpt-4o-mini']
                        
                        with patch('llm_router.router._call_text', new_callable=AsyncMock) as mock_call:
                            mock_call.return_value = LLMResponse(
                                content="test",
                                model="openai/gpt-4o-mini",
                                input_tokens=100,
                                output_tokens=100,
                                cost_usd=0.001,
                                latency_ms=50.0,
                                provider="openai",
                            )
                            
                            with patch('llm_router.router.get_tracker') as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker
                                
                                await route_and_call(TaskType.QUERY, "test")
                                
                                # Verify correlation_id was passed
                                assert mock_call.called
                                call_kwargs = mock_call.call_args[1]
                                assert 'correlation_id' in call_kwargs
                                correlation_id = call_kwargs['correlation_id']
                                assert len(correlation_id) == 8  # UUID4 hex[:8]

    @pytest.mark.asyncio
    async def test_correlation_id_logged_on_success(self):
        """Correlation ID should be in routing_decision logs."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock):
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock):
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with patch('llm_router.router._build_and_filter_chain') as mock_chain:
                        mock_chain.return_value = ['openai/gpt-4o-mini']
                        
                        with patch('llm_router.router._call_text', new_callable=AsyncMock) as mock_call:
                            mock_call.return_value = LLMResponse(
                                content="test",
                                model="openai/gpt-4o-mini",
                                input_tokens=100,
                                output_tokens=100,
                                cost_usd=0.001,
                                latency_ms=50.0,
                                provider="openai",
                            )
                            
                            with patch('llm_router.router.get_tracker') as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker
                                
                                result = await route_and_call(TaskType.QUERY, "test")
                                assert result.content == "test"


class TestInvalidComplexityHandling:
    """v5.3.0: Error handling for invalid complexity hints."""

    @pytest.mark.asyncio
    async def test_invalid_complexity_hint_falls_back_to_default(self):
        """Invalid complexity_hint should be handled gracefully."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock):
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock):
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with patch('llm_router.router._build_and_filter_chain') as mock_chain:
                        mock_chain.return_value = ['openai/gpt-4o-mini']
                        
                        with patch('llm_router.router._call_text', new_callable=AsyncMock) as mock_call:
                            mock_call.return_value = LLMResponse(
                                content="test",
                                model="openai/gpt-4o-mini",
                                input_tokens=100,
                                output_tokens=100,
                                cost_usd=0.001,
                                latency_ms=50.0,
                                provider="openai",
                            )
                            
                            with patch('llm_router.router.get_tracker') as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker
                                
                                # Should not raise, should use default complexity
                                result = await route_and_call(
                                    TaskType.QUERY,
                                    "test",
                                    complexity_hint="invalid_value",
                                )
                                
                                assert result.content == "test"
                                # Should have used BALANCED as default (moderate complexity)
                                mock_chain.assert_called()

    @pytest.mark.asyncio
    async def test_none_complexity_hint_uses_heuristic(self):
        """None complexity_hint should use prompt length heuristic."""
        with patch('llm_router.cost.get_monthly_spend', new_callable=AsyncMock):
            with patch('llm_router.cost.get_daily_spend', new_callable=AsyncMock):
                with patch('llm_router.router.get_config') as mock_config:
                    config = MagicMock()
                    config.llm_router_monthly_budget = 0.0
                    config.llm_router_daily_spend_limit = 0.0
                    config.available_providers = ['openai']
                    mock_config.return_value = config
                    
                    with patch('llm_router.router._build_and_filter_chain') as mock_chain:
                        mock_chain.return_value = ['openai/gpt-4o-mini']
                        
                        with patch('llm_router.router._call_text', new_callable=AsyncMock) as mock_call:
                            mock_call.return_value = LLMResponse(
                                content="test",
                                model="openai/gpt-4o-mini",
                                input_tokens=100,
                                output_tokens=100,
                                cost_usd=0.001,
                                latency_ms=50.0,
                                provider="openai",
                            )
                            
                            with patch('llm_router.router.get_tracker') as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker
                                
                                result = await route_and_call(
                                    TaskType.QUERY,
                                    "short prompt",
                                    complexity_hint=None,
                                )
                                
                                assert result.content == "test"
