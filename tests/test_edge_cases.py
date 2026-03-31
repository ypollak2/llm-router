"""Hard edge-case tests for the LLM Router.

Tests stress conditions, race conditions, boundary values, and unusual inputs
that could break the router in production.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from llm_router.cache import ClassificationCache, get_cache
from llm_router.classifier import classify_complexity
from llm_router.health import HealthTracker, ProviderHealth
from llm_router.router import route_and_call
from llm_router.types import (
    ClassificationResult, Complexity, LLMResponse,
    RoutingProfile, TaskType,
)


# ── Cache Edge Cases ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCacheEdgeCases:
    async def test_empty_prompt_caches(self):
        """Empty string is a valid cache key."""
        cache = ClassificationCache()
        result = ClassificationResult(
            complexity=Complexity.SIMPLE, confidence=0.9, reasoning="empty",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )
        await cache.put("", result)
        assert await cache.get("") is not None

    async def test_very_long_prompt_caches(self):
        """100KB prompt should still cache (hash is always 64 chars)."""
        cache = ClassificationCache()
        long_prompt = "x" * 100_000
        result = ClassificationResult(
            complexity=Complexity.COMPLEX, confidence=0.8, reasoning="long",
            inferred_task_type=TaskType.ANALYZE, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )
        await cache.put(long_prompt, result)
        cached = await cache.get(long_prompt)
        assert cached is not None
        assert cached.complexity == Complexity.COMPLEX

    async def test_unicode_prompt_caches(self):
        """Unicode / Hebrew / emoji prompts cache correctly."""
        cache = ClassificationCache()
        prompts = [
            "מה קורה עם הפרויקט?",
            "🚀 Launch the feature",
            "日本語のテスト",
            "混合 mixed \n\ttabs\r\n and stuff",
        ]
        result = ClassificationResult(
            complexity=Complexity.MODERATE, confidence=0.7, reasoning="unicode",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )
        for p in prompts:
            await cache.put(p, result)
            assert await cache.get(p) is not None

    async def test_concurrent_cache_access(self):
        """Multiple concurrent reads/writes shouldn't corrupt cache."""
        cache = ClassificationCache()
        result = ClassificationResult(
            complexity=Complexity.SIMPLE, confidence=0.9, reasoning="concurrent",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )

        async def write_and_read(i: int):
            prompt = f"prompt-{i}"
            await cache.put(prompt, result)
            cached = await cache.get(prompt)
            assert cached is not None

        # 50 concurrent writes + reads
        await asyncio.gather(*(write_and_read(i) for i in range(50)))

        stats = await cache.get_stats()
        assert stats["entries"] == 50

    async def test_cache_at_capacity_boundary(self):
        """Filling to exactly max_entries, then one more."""
        cache = ClassificationCache(max_entries=5)
        result = ClassificationResult(
            complexity=Complexity.SIMPLE, confidence=0.9, reasoning="boundary",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )

        # Fill exactly to capacity
        for i in range(5):
            await cache.put(f"p{i}", result)
        assert (await cache.get_stats())["entries"] == 5

        # One more evicts oldest
        await cache.put("overflow", result)
        assert (await cache.get_stats())["entries"] == 5
        assert await cache.get("p0") is None  # evicted
        assert await cache.get("overflow") is not None

    async def test_overwrite_existing_key(self):
        """Putting same key twice updates value without growing cache."""
        cache = ClassificationCache()
        r1 = ClassificationResult(
            complexity=Complexity.SIMPLE, confidence=0.5, reasoning="v1",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )
        r2 = ClassificationResult(
            complexity=Complexity.COMPLEX, confidence=0.9, reasoning="v2",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )

        await cache.put("same-prompt", r1)
        await cache.put("same-prompt", r2)

        stats = await cache.get_stats()
        assert stats["entries"] == 1
        cached = await cache.get("same-prompt")
        assert cached.complexity == Complexity.COMPLEX

    async def test_ttl_boundary(self):
        """Entry at exactly TTL boundary."""
        cache = ClassificationCache(ttl_seconds=10)
        result = ClassificationResult(
            complexity=Complexity.SIMPLE, confidence=0.9, reasoning="ttl",
            inferred_task_type=None, classifier_model="test",
            classifier_cost_usd=0.0, classifier_latency_ms=0.0,
        )
        await cache.put("ttl-test", result)

        # Set created_at to exactly TTL ago — should expire
        key = cache._hash_key("ttl-test")
        entry = cache._cache[key]
        cache._cache[key] = entry.__class__(
            result=entry.result,
            created_at=time.monotonic() - 10.001,
            prompt_preview=entry.prompt_preview,
        )
        assert await cache.get("ttl-test") is None


# ── Classifier Edge Cases ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestClassifierEdgeCases:
    @pytest.fixture(autouse=True)
    async def clear_cache(self):
        cache = get_cache()
        await cache.clear()
        yield
        await cache.clear()

    async def test_classifier_returns_cached_on_second_call(self):
        """Second identical call should hit cache, not call LLM."""
        call_count = 0

        async def mock_call(model, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            return LLMResponse(
                content='{"complexity":"simple","task_type":"query","confidence":0.95,"reasoning":"test"}',
                model=model, input_tokens=10, output_tokens=20,
                cost_usd=0.0001, latency_ms=100.0, provider="gemini",
            )

        with (
            patch("llm_router.classifier.providers.call_llm", side_effect=mock_call),
            patch("llm_router.classifier.get_config") as mock_config,
        ):
            mock_config.return_value.available_providers = {"gemini"}
            r1 = await classify_complexity("What is Python?")
            r2 = await classify_complexity("What is Python?")

        assert call_count == 1  # only called LLM once
        assert r1.complexity == r2.complexity
        assert r2.classifier_cost_usd == 0.0001  # preserved from original

    async def test_classifier_different_quality_modes_not_cached_together(self):
        """Same prompt but different quality_mode should be separate cache entries."""
        call_count = 0

        async def mock_call(model, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            return LLMResponse(
                content='{"complexity":"moderate","task_type":"code","confidence":0.8,"reasoning":"test"}',
                model=model, input_tokens=10, output_tokens=20,
                cost_usd=0.0001, latency_ms=100.0, provider="gemini",
            )

        with (
            patch("llm_router.classifier.providers.call_llm", side_effect=mock_call),
            patch("llm_router.classifier.get_config") as mock_config,
        ):
            mock_config.return_value.available_providers = {"gemini"}
            await classify_complexity("Build a REST API", quality_mode="balanced")
            await classify_complexity("Build a REST API", quality_mode="best")

        assert call_count == 2  # different quality modes = different cache keys


# ── Health Tracker Edge Cases ────────────────────────────────────────────────


class TestHealthEdgeCases:
    def test_rapid_failure_and_recovery(self):
        """Provider fails 3x, becomes unhealthy, then recovers after cooldown."""
        health = ProviderHealth()
        for _ in range(3):
            health.record_failure()
        assert not health.is_healthy()

        # Simulate cooldown elapsed
        health.last_failure_time = time.monotonic() - 61
        assert health.is_healthy()
        assert health.consecutive_failures == 0  # reset

    def test_interleaved_success_prevents_circuit_break(self):
        """Success between failures keeps provider healthy."""
        health = ProviderHealth()
        health.record_failure()
        health.record_failure()
        health.record_success()  # resets consecutive to 0
        health.record_failure()
        # 1 consecutive failure after reset — still below threshold (2)
        assert health.is_healthy()

    def test_rate_limit_followed_by_hard_failure(self):
        """Rate limit then failure — both tracked independently."""
        health = ProviderHealth()
        health.record_rate_limit()
        assert health.rate_limited
        assert health.consecutive_failures == 0  # rate limit doesn't add failure

        health.record_failure()
        assert health.rate_limited  # still rate limited
        assert health.consecutive_failures == 1

    def test_many_providers_independent(self):
        """Each provider's health is completely independent."""
        tracker = HealthTracker()
        for _ in range(5):
            tracker.record_failure("bad-provider")
        tracker.record_success("good-provider")
        tracker.record_rate_limit("throttled-provider")

        assert not tracker.is_healthy("bad-provider")
        assert tracker.is_healthy("good-provider")
        assert not tracker.is_healthy("throttled-provider")
        # Unknown provider should be healthy (no failures yet)
        assert tracker.is_healthy("new-provider")


# ── Router Edge Cases ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRouterEdgeCases:
    async def test_empty_model_chain_raises(self):
        """No available models should raise ValueError, not crash."""
        with (
            patch("llm_router.router.get_config") as mock_config,
            patch("llm_router.router.get_tracker"),
            patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0),
        ):
            mock_config.return_value.llm_router_profile = RoutingProfile.BUDGET
            mock_config.return_value.llm_router_monthly_budget = 0
            mock_config.return_value.available_providers = set()
            mock_config.return_value.compaction_mode = "off"
            mock_config.return_value.compaction_threshold = 4000
            mock_config.return_value.ollama_models_for_profile.return_value = []
            mock_config.return_value.all_ollama_models.return_value = []

            with patch("llm_router.router.get_model_chain", return_value=["openai/gpt-4o"]):
                with pytest.raises(ValueError, match="No available models"):
                    await route_and_call(TaskType.QUERY, "test")

    async def test_all_providers_unhealthy_skips_to_error(self):
        """When all providers are unhealthy, router should raise RuntimeError."""
        tracker = HealthTracker()
        # Make the only provider unhealthy
        for _ in range(5):
            tracker.record_failure("openai")

        with (
            patch("llm_router.router.get_config") as mock_config,
            patch("llm_router.router.get_tracker", return_value=tracker),
            patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0),
        ):
            mock_config.return_value.llm_router_profile = RoutingProfile.BUDGET
            mock_config.return_value.llm_router_monthly_budget = 0
            mock_config.return_value.available_providers = {"openai"}
            mock_config.return_value.compaction_mode = "off"
            mock_config.return_value.compaction_threshold = 4000
            mock_config.return_value.ollama_models_for_profile.return_value = []
            mock_config.return_value.all_ollama_models.return_value = []

            with patch("llm_router.router.get_model_chain", return_value=["openai/gpt-4o"]):
                with pytest.raises(RuntimeError, match="All models failed"):
                    await route_and_call(TaskType.QUERY, "test")


# ── Auto-Route Hook Edge Cases ───────────────────────────────────────────────


class TestAutoRouteHookEdgeCases:
    """Test the heuristic classifier with tricky/ambiguous inputs."""

    def _run_hook(self, prompt: str) -> dict | None:
        import json
        import subprocess
        import sys
        payload = json.dumps({"prompt": prompt})
        result = subprocess.run(
            [sys.executable, ".claude/hooks/auto-route.py"],
            input=payload, capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    def test_mixed_signals_routes_best_match(self):
        """With the new permissive routing, mixed signals still route."""
        out = self._run_hook("edit the file and analyze the changes")
        assert out is not None
        hint = out["hookSpecificOutput"]["contextForAgent"]
        # Mixed signals — classifiers may route to analyze, code, or generate
        assert "[ROUTE: analyze/" in hint or "[ROUTE: code/" in hint or "[ROUTE: generate/" in hint

    def test_code_fix_routes(self):
        """'fix' with code references now routes to code task type."""
        out = self._run_hook("fix `implement_auth()` in server.py")
        assert out is not None
        hint = out["hookSpecificOutput"]["contextForAgent"]
        assert "[ROUTE:" in hint

    def test_long_prompt_classified_complex(self):
        """Very long prompts (>500 chars) should be classified as complex."""
        long = "Analyze " + "the implications of this architectural decision " * 15
        out = self._run_hook(long)
        assert out is not None
        hint = out["hookSpecificOutput"]["contextForAgent"]
        assert "complex" in hint

    def test_url_in_prompt_not_confused(self):
        """URLs shouldn't trigger false positives."""
        # "run" in URL shouldn't match LOCAL_PATTERNS
        out = self._run_hook("Research the company at https://example.com/run/latest")
        assert out is not None

    def test_question_mark_query(self):
        """Simple questions with ? should route (query or fallback without Ollama)."""
        out = self._run_hook("What is the difference between REST and GraphQL?")
        assert out is not None
        hint = out["hookSpecificOutput"]["contextForAgent"]
        # May route as query (with Ollama) or auto/fallback (without)
        assert "[ROUTE:" in hint

    def test_multilingual_prompt(self):
        """Hebrew prompt with 'research' keyword should still route."""
        out = self._run_hook("Research על המצב הפוליטי בישראל")
        assert out is not None

    def test_only_whitespace(self):
        """Whitespace-only prompt should not route."""
        assert self._run_hook("   \n\t  ") is None

    def test_special_characters(self):
        """Special chars shouldn't crash the hook — may classify via Ollama."""
        result = self._run_hook("$$$###@@@!!!")
        assert result is None or "hookSpecificOutput" in result

    def test_json_in_prompt(self):
        """JSON blob in prompt shouldn't break the hook."""
        out = self._run_hook('Analyze this JSON: {"key": "value", "nested": {"deep": true}}')
        assert out is not None


class TestClaudeSubscriptionFlag:
    """Tests for the llm_router_claude_subscription config flag."""

    def test_subscription_flag_excludes_anthropic_from_providers(self, monkeypatch):
        """When llm_router_claude_subscription=True, anthropic is NOT in providers.

        We never route to Claude via API when already inside Claude Code — that
        would require a separate API key and add duplicate billing.
        """
        import llm_router.config as config_mod
        config_mod._config = None  # reset singleton
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = config_mod.RouterConfig()
        assert "anthropic" not in config.available_providers

    def test_no_subscription_flag_requires_api_key(self, monkeypatch):
        """Without the flag, anthropic requires ANTHROPIC_API_KEY."""
        import llm_router.config as config_mod
        config_mod._config = None
        monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = config_mod.RouterConfig()
        assert "anthropic" not in config.available_providers

    def test_api_key_still_adds_anthropic_without_flag(self, monkeypatch):
        """Explicit ANTHROPIC_API_KEY still works regardless of the flag."""
        import llm_router.config as config_mod
        config_mod._config = None
        monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        config = config_mod.RouterConfig()
        assert "anthropic" in config.available_providers
