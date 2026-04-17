"""Tests for rate limit detection and automatic provider switching."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from llm_router.health import ProviderHealth, HealthTracker, RATE_LIMIT_COOLDOWN_SECONDS
from llm_router.router import _is_rate_limit_error
from llm_router.types import LLMResponse, RoutingProfile, TaskType


# ── Rate Limit Detection ─────────────────────────────────────────────────────


class TestRateLimitDetection:
    def test_detects_429_in_message(self):
        assert _is_rate_limit_error(Exception("Error 429: Too Many Requests"))

    def test_detects_rate_limit_phrase(self):
        assert _is_rate_limit_error(Exception("Rate limit exceeded for model gpt-4o"))

    def test_detects_rate_limit_underscore(self):
        assert _is_rate_limit_error(Exception("rate_limit_exceeded"))

    def test_detects_too_many_requests(self):
        assert _is_rate_limit_error(Exception("too many requests"))

    def test_detects_quota_exceeded(self):
        assert _is_rate_limit_error(Exception("quota exceeded for this billing period"))

    def test_detects_ratelimit_class_name(self):
        class RateLimitError(Exception):
            pass
        assert _is_rate_limit_error(RateLimitError("hit the limit"))

    def test_does_not_match_auth_error(self):
        assert not _is_rate_limit_error(Exception("Invalid API key provided"))

    def test_does_not_match_timeout(self):
        assert not _is_rate_limit_error(Exception("Connection timed out"))

    def test_does_not_match_generic_error(self):
        assert not _is_rate_limit_error(Exception("Internal server error"))

    def test_litellm_rate_limit_format(self):
        assert _is_rate_limit_error(
            Exception("litellm.RateLimitError: RateLimitError: Rate limit reached for gpt-4o")
        )

    def test_openai_429_format(self):
        assert _is_rate_limit_error(
            Exception("Error code: 429 - {'error': {'message': 'Rate limit reached'}}")
        )


# ── Health Tracking with Rate Limits ─────────────────────────────────────────


class TestProviderHealthRateLimit:
    def test_rate_limited_provider_is_unhealthy(self):
        health = ProviderHealth()
        health.record_rate_limit()
        assert not health.is_healthy()
        assert "rate-limited" in health.status

    def test_rate_limit_clears_after_cooldown(self):
        health = ProviderHealth()
        health.record_rate_limit()
        # Simulate time passing
        health.rate_limit_time = time.monotonic() - RATE_LIMIT_COOLDOWN_SECONDS - 1
        assert health.is_healthy()

    def test_success_clears_rate_limit(self):
        health = ProviderHealth()
        health.record_rate_limit()
        assert not health.is_healthy()
        health.record_success()
        assert health.is_healthy()
        assert not health.rate_limited

    def test_rate_limit_count_increments(self):
        health = ProviderHealth()
        health.record_rate_limit()
        health.record_rate_limit()
        assert health.rate_limit_count == 2
        assert health.total_calls == 2

    def test_rate_limit_separate_from_failure(self):
        health = ProviderHealth()
        health.record_rate_limit()
        # Rate limit doesn't affect consecutive_failures
        assert health.consecutive_failures == 0


class TestHealthTrackerRateLimit:
    def test_tracker_records_rate_limit(self):
        tracker = HealthTracker()
        tracker.record_rate_limit("openai")
        assert not tracker.is_healthy("openai")

    def test_tracker_rate_limit_different_from_failure(self):
        tracker = HealthTracker()
        tracker.record_rate_limit("openai")
        tracker.record_failure("gemini")
        # OpenAI is rate-limited, Gemini has 1 failure (still healthy)
        assert not tracker.is_healthy("openai")
        assert tracker.is_healthy("gemini")


# ── Router Integration ───────────────────────────────────────────────────────


@pytest.mark.requires_api_keys
class TestRouterRateLimitSwitching:
    @pytest.mark.asyncio
    async def test_switches_provider_on_rate_limit(self):
        """When first provider is rate-limited, router should switch to the next."""
        from llm_router.router import route_and_call

        call_count = 0

        async def mock_call_llm(model, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if "gemini" in model:
                raise Exception("Rate limit exceeded for gemini-2.5-flash")
            return LLMResponse(
                content="Success from OpenAI",
                model=model,
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
                latency_ms=100.0,
                provider="openai",
            )

        with (
            patch("llm_router.router.providers.call_llm", side_effect=mock_call_llm),
            patch("llm_router.router.cost.log_usage", new_callable=AsyncMock),
            patch("llm_router.router.get_config") as mock_config,
            patch("llm_router.router.get_tracker") as mock_get_tracker,
        ):
            mock_config.return_value.llm_router_profile = RoutingProfile.BUDGET
            mock_config.return_value.llm_router_monthly_budget = 0
            mock_config.return_value.available_providers = {"gemini", "openai"}
            mock_config.return_value.compaction_mode = "off"
            mock_config.return_value.compaction_threshold = 4000
            mock_config.return_value.ollama_models_for_profile.return_value = []
            mock_config.return_value.all_ollama_models.return_value = []

            tracker = HealthTracker()
            mock_get_tracker.return_value = tracker

            with patch(
                "llm_router.router.get_model_chain",
                return_value=["gemini/gemini-2.5-flash", "openai/gpt-4o-mini"],
            ):
                resp = await route_and_call(TaskType.QUERY, "test prompt")

            assert resp.content == "Success from OpenAI"
            assert call_count == 2  # tried gemini, then openai
            # Gemini should be rate-limited now
            assert not tracker.is_healthy("gemini")

    @pytest.mark.asyncio
    async def test_all_providers_rate_limited_raises(self):
        """When all providers are rate-limited, router should raise."""
        from llm_router.router import route_and_call

        async def mock_call_llm(model, messages, **kwargs):
            raise Exception("429 Too Many Requests")

        with (
            patch("llm_router.router.providers.call_llm", side_effect=mock_call_llm),
            patch("llm_router.router.get_config") as mock_config,
            patch("llm_router.router.get_tracker") as mock_get_tracker,
        ):
            mock_config.return_value.llm_router_profile = RoutingProfile.BUDGET
            mock_config.return_value.llm_router_monthly_budget = 0
            mock_config.return_value.available_providers = {"openai"}
            mock_config.return_value.compaction_mode = "off"
            mock_config.return_value.compaction_threshold = 4000
            mock_config.return_value.ollama_models_for_profile.return_value = []
            mock_config.return_value.all_ollama_models.return_value = []

            tracker = HealthTracker()
            mock_get_tracker.return_value = tracker

            with patch(
                "llm_router.router.get_model_chain",
                return_value=["openai/gpt-4o-mini"],
            ):
                with pytest.raises(RuntimeError, match="All models failed"):
                    await route_and_call(TaskType.QUERY, "test")
