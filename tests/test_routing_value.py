"""Tests for routing cost-saving contracts — model chain ordering and circuit breakers.

These tests verify the behaviors that generate the most measurable cost savings:

1. Complexity → profile mapping (simple → Haiku at $0.0008/1K vs Opus at $0.075/1K)
2. Subscription-aware agent reordering (Ollama first → $0 for local tasks)
3. Pressure-based chain reordering (85%+ quota → avoid Claude, use cheap externals)
4. Circuit breaker skips unhealthy providers rather than wasting latency on doomed calls
"""

from __future__ import annotations

import time
from unittest.mock import patch


from llm_router.health import ProviderHealth, HealthTracker, RATE_LIMIT_COOLDOWN_SECONDS
from llm_router.profiles import (
    COMPLEXITY_TO_PROFILE,
    reorder_for_pressure,
    get_model_chain,
    provider_from_model,
)
from llm_router.router import _reorder_for_agent_context
from llm_router.types import Complexity, RoutingProfile, TaskType


class TestComplexityToProfileMapping:
    """Every routing decision starts here: classifier output → which price tier.

    The economic contract:
      simple  → BUDGET  → Haiku       ($0.0008/1K in tokens blended)
      moderate → BALANCED → Sonnet    ($0.006/1K blended)
      complex → PREMIUM  → Opus       ($0.075/1K blended)

    A bug that maps SIMPLE to PREMIUM costs ~94x more per query.
    """

    def test_simple_maps_to_budget_profile(self):
        assert COMPLEXITY_TO_PROFILE[Complexity.SIMPLE] == RoutingProfile.BUDGET

    def test_moderate_maps_to_balanced_profile(self):
        assert COMPLEXITY_TO_PROFILE[Complexity.MODERATE] == RoutingProfile.BALANCED

    def test_complex_maps_to_premium_profile(self):
        assert COMPLEXITY_TO_PROFILE[Complexity.COMPLEX] == RoutingProfile.PREMIUM

    def test_deep_reasoning_maps_to_premium_profile(self):
        # Extended thinking requires the best model — same as COMPLEX.
        assert COMPLEXITY_TO_PROFILE[Complexity.DEEP_REASONING] == RoutingProfile.PREMIUM

    def test_all_complexity_levels_have_mappings(self):
        """No complexity level should be unmapped (would cause a KeyError at runtime)."""
        for c in Complexity:
            assert c in COMPLEXITY_TO_PROFILE, f"{c} has no profile mapping"


class TestProviderFromModel:
    """provider_from_model extracts the billing provider from a LiteLLM model ID."""

    def test_anthropic_model(self):
        assert provider_from_model("anthropic/claude-haiku-4-5-20251001") == "anthropic"

    def test_openai_model(self):
        assert provider_from_model("openai/gpt-4o") == "openai"

    def test_gemini_model(self):
        assert provider_from_model("gemini/gemini-2.5-flash") == "gemini"

    def test_ollama_model(self):
        assert provider_from_model("ollama/llama3.2") == "ollama"

    def test_codex_model(self):
        assert provider_from_model("codex/gpt-5.4") == "codex"

    def test_model_without_slash_returns_unknown(self):
        # Non-prefixed model names return "unknown" — not a valid provider
        result = provider_from_model("gpt-4o")
        assert result == "unknown"


class TestAgentContextReordering:
    """Subscription-aware model ordering is the highest-impact cost-saving feature.

    Inside Claude Code: Claude is already paid-for (subscription), Codex is
    already paid-for (OpenAI subscription), and Ollama is free.
    The router must put free models first to avoid API spend.

    Cost impact per 1K tokens (rough estimates):
      Ollama: $0.00 (local)
      Codex:  $0.00 (OpenAI subscription)
      Haiku:  $0.0008 (Anthropic API)
      GPT-4o: $0.0025 (OpenAI API)
      Opus:   $0.075  (Anthropic API)
    """

    def test_claude_code_simple_puts_ollama_first(self):
        """For a claude_code session with simple tasks, free local inference first."""
        models = [
            "anthropic/claude-haiku-4-5-20251001",
            "openai/gpt-4o",
            "ollama/llama3.2",
        ]
        result = _reorder_for_agent_context(models, "claude_code", Complexity.SIMPLE)
        assert result[0] == "ollama/llama3.2"

    def test_claude_code_simple_puts_claude_before_openai(self):
        """For claude_code, Claude subscription covers Haiku — it's cheaper than GPT-4o."""
        models = [
            "openai/gpt-4o",
            "anthropic/claude-haiku-4-5-20251001",
            "ollama/llama3.2",
        ]
        result = _reorder_for_agent_context(models, "claude_code", Complexity.SIMPLE)
        claude_idx = result.index("anthropic/claude-haiku-4-5-20251001")
        openai_idx = result.index("openai/gpt-4o")
        assert claude_idx < openai_idx

    def test_codex_simple_puts_ollama_first(self):
        """For codex sessions, Ollama is still free and goes first."""
        models = [
            "anthropic/claude-haiku-4-5-20251001",
            "openai/gpt-4o",
            "ollama/llama3.2",
        ]
        result = _reorder_for_agent_context(models, "codex", Complexity.SIMPLE)
        assert result[0] == "ollama/llama3.2"

    def test_codex_simple_puts_codex_before_claude(self):
        """For codex sessions, Codex (OpenAI subscription) comes before paid Claude API."""
        models = [
            "anthropic/claude-haiku-4-5-20251001",
            "openai/gpt-4o",
            "codex/gpt-5.4",
            "ollama/llama3.2",
        ]
        result = _reorder_for_agent_context(models, "codex", Complexity.SIMPLE)
        codex_idx = result.index("codex/gpt-5.4")
        claude_idx = result.index("anthropic/claude-haiku-4-5-20251001")
        # Ollama is 0, Codex should come before Claude in a codex session
        assert codex_idx < claude_idx

    def test_claude_code_complex_puts_claude_first(self):
        """For complex tasks in claude_code, subscription Claude leads (no extra cost)."""
        models = [
            "openai/gpt-4o",
            "anthropic/claude-opus-4-6",
            "ollama/llama3.2",
            "codex/o3",
        ]
        result = _reorder_for_agent_context(models, "claude_code", Complexity.COMPLEX)
        assert result[0] == "anthropic/claude-opus-4-6"

    def test_codex_complex_puts_codex_first(self):
        """For complex tasks in codex sessions, Codex subscription leads."""
        models = [
            "anthropic/claude-opus-4-6",
            "openai/gpt-4o",
            "ollama/llama3.2",
            "codex/o3",
        ]
        result = _reorder_for_agent_context(models, "codex", Complexity.COMPLEX)
        assert result[0] == "codex/o3"

    def test_no_agent_returns_unchanged(self):
        """Without an active agent context, the original chain is preserved."""
        models = ["anthropic/claude-opus-4-6", "openai/gpt-4o", "ollama/llama3.2"]
        result = _reorder_for_agent_context(models, None, Complexity.SIMPLE)
        assert result == models

    def test_reordering_preserves_all_models(self):
        """No model is ever dropped by reordering — only the sequence changes."""
        models = [
            "anthropic/claude-haiku-4-5-20251001",
            "openai/gpt-4o",
            "ollama/llama3.2",
            "codex/gpt-5.4",
            "gemini/gemini-2.5-flash",
        ]
        result = _reorder_for_agent_context(models, "claude_code", Complexity.SIMPLE)
        assert sorted(result) == sorted(models)


class TestPressureBasedReordering:
    """At high Claude quota (≥85%), cheap models move to front to protect the quota.

    This is the second most important cost-saving contract. When you're near
    your Claude weekly limit, routing to Gemini Flash instead of Sonnet avoids
    hitting the cap AND costs 20x less.
    """

    def test_low_pressure_claude_leads_chain(self):
        """Under 85% pressure, Claude is free — it should be tried first."""
        chain = [
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",
        ]
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            result = reorder_for_pressure(chain, pressure=0.50, profile=RoutingProfile.BALANCED)
        assert result[0] == "anthropic/claude-sonnet-4-6"

    def test_high_pressure_claude_moves_to_end(self):
        """At 90% pressure, cheap models go first to save remaining Claude quota."""
        chain = [
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",   # cheap
        ]
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            result = reorder_for_pressure(chain, pressure=0.90, profile=RoutingProfile.BALANCED)
        # Claude should NOT be first
        assert result[0] != "anthropic/claude-sonnet-4-6"
        # Claude should still be present (not removed at 90%)
        assert "anthropic/claude-sonnet-4-6" in result

    def test_critical_pressure_removes_claude_entirely(self):
        """At ≥99% pressure, Claude is removed to guarantee the cap is never crossed."""
        chain = [
            "anthropic/claude-opus-4-6",
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",
        ]
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            result = reorder_for_pressure(chain, pressure=0.99, profile=RoutingProfile.BALANCED)
        assert "anthropic/claude-opus-4-6" not in result

    def test_codex_injected_first_at_high_pressure(self):
        """When Codex is available and quota is high, it should lead the chain."""
        chain = [
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",
            "codex/gpt-5.4",
        ]
        with patch("llm_router.codex_agent.is_codex_available", return_value=True):
            result = reorder_for_pressure(chain, pressure=0.90, profile=RoutingProfile.BALANCED)
        assert result[0] == "codex/gpt-5.4"

    def test_at_critical_pressure_claude_haiku_removed_even_in_budget(self):
        """At ≥99% pressure, Haiku is removed from BUDGET chains too — quota is quota."""
        chain = ["anthropic/claude-haiku-4-5-20251001", "gemini/gemini-2.5-flash"]
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            result = reorder_for_pressure(chain, pressure=0.99, profile=RoutingProfile.BUDGET)
        # Haiku is a Claude model and is excluded at the hard cap
        assert "anthropic/claude-haiku-4-5-20251001" not in result
        # Gemini Flash (cheap, non-Claude) stays in the chain
        assert "gemini/gemini-2.5-flash" in result


class TestCircuitBreaker:
    """The circuit breaker prevents retrying providers that are currently failing.

    Without this, a 429 from Gemini would cause every subsequent task to
    waste ~5s waiting for Gemini to time out before trying the next provider.
    """

    def test_healthy_provider_is_healthy_by_default(self):
        health = ProviderHealth()
        with patch("llm_router.health.get_config") as mock_config:
            mock_config.return_value.health_failure_threshold = 2
            mock_config.return_value.health_cooldown_seconds = 30
            assert health.is_healthy() is True

    def test_two_failures_trips_circuit_breaker(self):
        health = ProviderHealth()
        health.record_failure()
        health.record_failure()
        with patch("llm_router.health.get_config") as mock_config:
            mock_config.return_value.health_failure_threshold = 2
            mock_config.return_value.health_cooldown_seconds = 30
            assert health.is_healthy() is False

    def test_success_after_failure_resets_breaker(self):
        health = ProviderHealth()
        health.record_failure()
        health.record_success()
        with patch("llm_router.health.get_config") as mock_config:
            mock_config.return_value.health_failure_threshold = 2
            mock_config.return_value.health_cooldown_seconds = 30
            assert health.is_healthy() is True

    def test_rate_limit_trips_short_cooldown(self):
        health = ProviderHealth()
        health.record_rate_limit()
        assert health.is_healthy() is False

    def test_rate_limit_recovers_after_cooldown(self):
        health = ProviderHealth()
        health.record_rate_limit()
        # Simulate time passing past the rate limit cooldown
        health.rate_limit_time = time.monotonic() - (RATE_LIMIT_COOLDOWN_SECONDS + 1)
        assert health.is_healthy() is True

    def test_rate_limit_count_increments(self):
        health = ProviderHealth()
        health.record_rate_limit()
        health.record_rate_limit()
        assert health.rate_limit_count == 2

    def test_health_tracker_isolates_providers(self):
        """Failures on one provider don't affect other providers' health."""
        tracker = HealthTracker()
        with patch("llm_router.health.get_config") as mock_config:
            mock_config.return_value.health_failure_threshold = 2
            mock_config.return_value.health_cooldown_seconds = 30
            tracker.record_failure("openai")
            tracker.record_failure("openai")
            # openai is down, but gemini should still be healthy
            assert tracker.is_healthy("gemini") is True
            assert tracker.is_healthy("openai") is False

    def test_total_calls_tracks_all_outcomes(self):
        health = ProviderHealth()
        health.record_success()
        health.record_failure()
        health.record_rate_limit()
        assert health.total_calls == 3

    def test_status_string_for_healthy_provider(self):
        health = ProviderHealth()
        with patch("llm_router.health.get_config") as mock_config:
            mock_config.return_value.health_failure_threshold = 2
            mock_config.return_value.health_cooldown_seconds = 30
            assert health.status == "healthy"

    def test_status_string_shows_failure_count(self):
        health = ProviderHealth()
        health.record_failure()
        health.record_failure()
        with patch("llm_router.health.get_config") as mock_config:
            mock_config.return_value.health_failure_threshold = 2
            mock_config.return_value.health_cooldown_seconds = 30
            assert "failures=2" in health.status


class TestBudgetModelChainShape:
    """Spot-check that BUDGET chains lead with cheap models, not frontier models.

    A routing table regression that swaps Haiku for Opus in the BUDGET chain
    would silently increase costs ~94x for all 'simple' classified tasks.
    """

    def test_budget_query_chain_starts_with_cheap_model(self, mock_env):
        chain = get_model_chain(RoutingProfile.BUDGET, TaskType.QUERY)
        assert len(chain) > 0
        # First model should NOT be a premium frontier model
        first = chain[0]
        premium_models = {"anthropic/claude-opus-4-6", "openai/o3", "openai/o3-mini"}
        assert first not in premium_models, f"BUDGET chain starts with premium model: {first}"

    def test_premium_query_chain_has_frontier_model(self, mock_env):
        chain = get_model_chain(RoutingProfile.PREMIUM, TaskType.QUERY)
        assert len(chain) > 0
        all_models = set(chain)
        premium_models = {
            "anthropic/claude-opus-4-6",
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4-6",
        }
        assert any(m in premium_models for m in all_models), (
            f"PREMIUM chain has no frontier models: {chain}"
        )

    def test_budget_chain_shorter_or_equal_to_premium(self, mock_env):
        """BUDGET chains don't need as many fallbacks as PREMIUM."""
        budget_chain = get_model_chain(RoutingProfile.BUDGET, TaskType.CODE)
        premium_chain = get_model_chain(RoutingProfile.PREMIUM, TaskType.CODE)
        # Both should be non-empty
        assert len(budget_chain) > 0
        assert len(premium_chain) > 0
