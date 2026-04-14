"""Tests for Phase 6: Dynamic Chain Builder (chain_builder.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.types import ModelCapability, ProviderTier, RoutingProfile, TaskType


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_cap(model_id: str, provider: str, tier: ProviderTier) -> ModelCapability:
    return ModelCapability(
        model_id=model_id,
        provider=provider,
        provider_tier=tier,
        task_types=frozenset({TaskType.CODE, TaskType.QUERY}),
    )


_FAKE_CAPS: dict[str, ModelCapability] = {
    "ollama/qwen3:32b": _make_cap("ollama/qwen3:32b", "ollama", ProviderTier.LOCAL),
    "openai/gpt-4o":    _make_cap("openai/gpt-4o",    "openai", ProviderTier.EXPENSIVE),
}


# ── is_dynamic_routing_enabled ────────────────────────────────────────────────

class TestIsDynamicRoutingEnabled:
    def test_false_by_default(self):
        from llm_router.chain_builder import is_dynamic_routing_enabled
        with patch("llm_router.config.get_config") as mock_cfg:
            mock_cfg.return_value.llm_router_dynamic = False
            assert is_dynamic_routing_enabled() is False

    def test_true_when_enabled(self):
        from llm_router.chain_builder import is_dynamic_routing_enabled
        with patch("llm_router.config.get_config") as mock_cfg:
            mock_cfg.return_value.llm_router_dynamic = True
            assert is_dynamic_routing_enabled() is True

    def test_returns_false_on_exception(self):
        from llm_router.chain_builder import is_dynamic_routing_enabled
        with patch("llm_router.config.get_config", side_effect=RuntimeError("cfg missing")):
            assert is_dynamic_routing_enabled() is False


# ── build_chain (static path) ─────────────────────────────────────────────────

class TestBuildChainStatic:
    @pytest.mark.asyncio
    async def test_returns_static_chain_when_dynamic_disabled(self):
        from llm_router.chain_builder import build_chain
        with patch("llm_router.chain_builder.is_dynamic_routing_enabled", return_value=False):
            result = await build_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)
        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_falls_back_to_static_on_exception(self):
        """Dynamic chain failure must fall back to static, never raise."""
        from llm_router.chain_builder import build_chain
        with (
            patch("llm_router.chain_builder.is_dynamic_routing_enabled", return_value=True),
            patch("llm_router.chain_builder._build_dynamic_chain", side_effect=RuntimeError("boom")),
        ):
            result = await build_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)
        assert isinstance(result, list)
        assert len(result) > 0


# ── _build_dynamic_chain — critical dict.values() regression ──────────────────

class TestBuildDynamicChain:
    """Regression tests for the dict.values() bug (iterating dict yields keys, not caps)."""

    _DISCOVER_PATCH = "llm_router.discover.discover_available_models"
    _SCORE_PATCH = "llm_router.scorer.score_all_models"

    @pytest.mark.asyncio
    async def test_dict_values_not_keys_are_iterated(self):
        """Regression: capabilities.values() must be used — not capabilities directly.

        Without the fix, iterating over the dict yields string keys, and accessing
        cap.task_types raises AttributeError: 'str' has no attribute 'task_types'.
        """
        from llm_router.chain_builder import _build_dynamic_chain
        from llm_router.types import BudgetState, ScoredModel

        fake_scored = [
            ScoredModel(
                model_id="ollama/qwen3:32b",
                score=0.85,
                quality_score=0.70,
                budget_score=1.0,
                latency_score=0.80,
                acceptance_score=1.0,
                capability=_FAKE_CAPS["ollama/qwen3:32b"],
            ),
        ]

        with (
            patch("llm_router.discover.discover_available_models",
                  new_callable=AsyncMock, return_value=_FAKE_CAPS),
            patch("llm_router.scorer.score_all_models",
                  new_callable=AsyncMock, return_value=fake_scored),
        ):
            # This should NOT raise AttributeError: 'str' has no attribute 'task_types'
            result = await _build_dynamic_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)

        assert isinstance(result, list)
        assert "ollama/qwen3:32b" in result

    @pytest.mark.asyncio
    async def test_empty_discovery_falls_back_to_static(self):
        from llm_router.chain_builder import _build_dynamic_chain
        with patch("llm_router.discover.discover_available_models",
                   new_callable=AsyncMock, return_value={}):
            result = await _build_dynamic_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)
        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_local_models_precede_paid_in_chain(self):
        """Free-first invariant: LOCAL tier models must appear before paid-API models."""
        from llm_router.chain_builder import _build_dynamic_chain
        from llm_router.types import ScoredModel

        # Paid model scores higher but local should still come first
        fake_scored = [
            ScoredModel(
                model_id="openai/gpt-4o",
                score=0.95,
                quality_score=0.90,
                budget_score=0.60,
                latency_score=0.80,
                acceptance_score=1.0,
                capability=_FAKE_CAPS["openai/gpt-4o"],
            ),
            ScoredModel(
                model_id="ollama/qwen3:32b",
                score=0.82,
                quality_score=0.70,
                budget_score=1.0,
                latency_score=0.80,
                acceptance_score=1.0,
                capability=_FAKE_CAPS["ollama/qwen3:32b"],
            ),
        ]

        with (
            patch("llm_router.discover.discover_available_models",
                  new_callable=AsyncMock, return_value=_FAKE_CAPS),
            patch("llm_router.scorer.score_all_models",
                  new_callable=AsyncMock, return_value=fake_scored),
        ):
            result = await _build_dynamic_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)

        assert result[0] == "ollama/qwen3:32b", "local model must be first (free-first invariant)"

    @pytest.mark.asyncio
    async def test_fallback_caps_are_list_not_dict(self):
        """Regression: when no task_caps match, fallback must be list[ModelCapability] not dict."""
        from llm_router.chain_builder import _build_dynamic_chain
        from llm_router.types import ScoredModel

        # Caps with task type that doesn't match requested task
        no_match_caps = {
            "ollama/qwen3:32b": ModelCapability(
                model_id="ollama/qwen3:32b",
                provider="ollama",
                provider_tier=ProviderTier.LOCAL,
                task_types=frozenset({TaskType.IMAGE}),  # won't match CODE — triggers fallback
            ),
        }

        fake_scored = [
            ScoredModel(
                model_id="ollama/qwen3:32b",
                score=0.85,
                quality_score=0.70,
                budget_score=1.0,
                latency_score=0.80,
                acceptance_score=1.0,
                capability=no_match_caps["ollama/qwen3:32b"],
            ),
        ]

        with (
            patch("llm_router.discover.discover_available_models",
                  new_callable=AsyncMock, return_value=no_match_caps),
            patch("llm_router.scorer.score_all_models",
                  new_callable=AsyncMock, return_value=fake_scored),
        ):
            # Without the fix, task_caps fallback would be a dict and scorer would
            # iterate over string keys, producing wrong model IDs or crashing.
            result = await _build_dynamic_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)

        assert isinstance(result, list)


# ── Cache corruption regression (discover.py) ─────────────────────────────────

class TestDiscoverCacheCorruption:
    """Regression tests for corrupted discovery.json not crashing the router."""

    def test_unknown_provider_tier_returns_none(self, tmp_path):
        """ValueError from ProviderTier(unknown_value) must be caught, not propagate."""
        import json
        import time
        from llm_router.discover import _load_cache

        cache_file = tmp_path / "discovery.json"
        cache_file.write_text(json.dumps({
            "cached_at": time.time(),
            "models": {
                "ollama/qwen3:32b": {
                    "model_id": "ollama/qwen3:32b",
                    "provider": "ollama",
                    "provider_tier": "future_tier_unknown",  # invalid enum value
                    "task_types": ["code"],
                    "cost_per_1k": 0.0,
                    "latency_p50_ms": 400.0,
                    "context_window": 128000,
                    "available": True,
                }
            },
        }))

        with patch("llm_router.discover._DISCOVERY_CACHE", cache_file):
            result = _load_cache(ttl=3600)

        assert result is None, "corrupted cache should return None, not raise ValueError"

    def test_missing_required_field_returns_none(self, tmp_path):
        """KeyError from missing model_id must be caught, not propagate."""
        import json
        import time
        from llm_router.discover import _load_cache

        cache_file = tmp_path / "discovery.json"
        cache_file.write_text(json.dumps({
            "cached_at": time.time(),
            "models": {
                "ollama/qwen3:32b": {
                    # model_id intentionally omitted
                    "provider": "ollama",
                    "provider_tier": "local",
                    "task_types": ["code"],
                }
            },
        }))

        with patch("llm_router.discover._DISCOVERY_CACHE", cache_file):
            result = _load_cache(ttl=3600)

        assert result is None

    def test_unknown_task_type_returns_none(self, tmp_path):
        """ValueError from TaskType(unknown_value) must be caught."""
        import json
        import time
        from llm_router.discover import _load_cache

        cache_file = tmp_path / "discovery.json"
        cache_file.write_text(json.dumps({
            "cached_at": time.time(),
            "models": {
                "ollama/qwen3:32b": {
                    "model_id": "ollama/qwen3:32b",
                    "provider": "ollama",
                    "provider_tier": "local",
                    "task_types": ["future_task_not_in_enum"],  # invalid TaskType
                    "cost_per_1k": 0.0,
                    "latency_p50_ms": 400.0,
                    "context_window": 128000,
                    "available": True,
                }
            },
        }))

        with patch("llm_router.discover._DISCOVERY_CACHE", cache_file):
            result = _load_cache(ttl=3600)

        assert result is None

    def test_valid_cache_loads_correctly(self, tmp_path):
        """Sanity check: a well-formed cache should load without errors."""
        import json
        import time
        from llm_router.discover import _load_cache

        cache_file = tmp_path / "discovery.json"
        cache_file.write_text(json.dumps({
            "cached_at": time.time(),
            "models": {
                "ollama/qwen3:32b": {
                    "model_id": "ollama/qwen3:32b",
                    "provider": "ollama",
                    "provider_tier": "local",
                    "task_types": ["code", "query"],
                    "cost_per_1k": 0.0,
                    "latency_p50_ms": 400.0,
                    "context_window": 128000,
                    "available": True,
                }
            },
        }))

        with patch("llm_router.discover._DISCOVERY_CACHE", cache_file):
            result = _load_cache(ttl=3600)

        assert result is not None
        assert "ollama/qwen3:32b" in result


# ── End-to-end: dynamic routing through build_chain ───────────────────────────

class TestEndToEndDynamicRouting:
    """Verify the full dynamic routing path: build_chain → discover → score → chain."""

    @pytest.mark.asyncio
    async def test_dynamic_enabled_uses_scored_chain(self):
        """When LLM_ROUTER_DYNAMIC=true, build_chain returns scored model IDs."""
        from llm_router.chain_builder import build_chain
        from llm_router.types import ScoredModel

        fake_scored = [
            ScoredModel(
                model_id="ollama/qwen3:32b",
                score=0.88,
                quality_score=0.70,
                budget_score=1.0,
                latency_score=0.80,
                acceptance_score=1.0,
                capability=_FAKE_CAPS["ollama/qwen3:32b"],
            ),
            ScoredModel(
                model_id="openai/gpt-4o",
                score=0.75,
                quality_score=0.90,
                budget_score=0.50,
                latency_score=0.80,
                acceptance_score=1.0,
                capability=_FAKE_CAPS["openai/gpt-4o"],
            ),
        ]

        with (
            patch("llm_router.chain_builder.is_dynamic_routing_enabled", return_value=True),
            patch("llm_router.discover.discover_available_models",
                  new_callable=AsyncMock, return_value=_FAKE_CAPS),
            patch("llm_router.scorer.score_all_models",
                  new_callable=AsyncMock, return_value=fake_scored),
        ):
            result = await build_chain(TaskType.CODE, "moderate", RoutingProfile.BALANCED)

        assert "ollama/qwen3:32b" in result
        assert result[0] == "ollama/qwen3:32b", "local model must lead chain"

    @pytest.mark.asyncio
    async def test_dynamic_chain_never_empty(self):
        """Even with zero discovered models, build_chain returns a non-empty static fallback."""
        from llm_router.chain_builder import build_chain

        with (
            patch("llm_router.chain_builder.is_dynamic_routing_enabled", return_value=True),
            patch("llm_router.discover.discover_available_models",
                  new_callable=AsyncMock, return_value={}),
        ):
            result = await build_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)

        assert isinstance(result, list)
        assert len(result) > 0, "static fallback must always return models"
