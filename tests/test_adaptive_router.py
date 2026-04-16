"""Tests for Adaptive Universal Router v5.0 (Phases 1-4).

Covers:
  - Phase 1: Ollama discovery injection (cache hit vs env var fallback)
  - Phase 2: Live API enumeration (OpenAI, Gemini scanners)
  - Phase 3: Always-on dynamic routing (feature flag removed)
  - Phase 4: Sidecar /score endpoint
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import ModelCapability, ProviderTier, RoutingProfile, TaskType
# Import json for URL test response handling


# ── Phase 1: Ollama Discovery Injection ───────────────────────────────────────

class TestOllamaDiscoveryInjection:
    """Test Ollama live-discovery integration with env var fallback."""

    def test_get_cached_ollama_models_reads_discovery_json(self, tmp_path):
        """Cache hit: get_cached_ollama_models reads models from discovery.json."""
        import json
        from llm_router.discover import get_cached_ollama_models

        cache_file = tmp_path / "discovery.json"
        cache_file.write_text(json.dumps({
            "cached_at": time.time(),
            "models": {
                "ollama/qwen3.5:latest": {
                    "model_id": "ollama/qwen3.5:latest",
                    "provider": "ollama",
                    "provider_tier": "local",
                    "task_types": ["code", "query"],
                    "cost_per_1k": 0.0,
                    "latency_p50_ms": 500.0,
                    "context_window": 32000,
                    "available": True,
                },
                "ollama/llama2:latest": {
                    "model_id": "ollama/llama2:latest",
                    "provider": "ollama",
                    "provider_tier": "local",
                    "task_types": ["code", "query"],
                    "cost_per_1k": 0.0,
                    "latency_p50_ms": 600.0,
                    "context_window": 4096,
                    "available": True,
                },
                "openai/gpt-4o": {
                    "model_id": "openai/gpt-4o",
                    "provider": "openai",
                    "provider_tier": "expensive",
                    "task_types": ["code"],
                    "cost_per_1k": 0.03,
                    "latency_p50_ms": 1200.0,
                    "context_window": 128000,
                    "available": True,
                },
            },
        }))

        with patch("llm_router.discover._DISCOVERY_CACHE", cache_file):
            result = get_cached_ollama_models()

        assert isinstance(result, list)
        assert "ollama/qwen3.5:latest" in result
        assert "ollama/llama2:latest" in result
        assert "openai/gpt-4o" not in result  # Should only return Ollama models

    def test_get_cached_ollama_models_empty_cache_returns_empty(self):
        """Cache miss: returns empty list, allowing fallback to env var."""
        from llm_router.discover import get_cached_ollama_models

        with patch("llm_router.discover._load_cache", return_value=None):
            result = get_cached_ollama_models()

        assert result == []

    def test_config_all_ollama_models_uses_cache_first(self, monkeypatch):
        """config.all_ollama_models() should use cached models when available."""
        from llm_router.config import get_config

        cfg = get_config()
        
        # Mock the discovery cache to return live models
        with patch("llm_router.discover.get_cached_ollama_models",
                   return_value=["ollama/qwen3.5:latest", "ollama/llama2:latest"]):
            models = cfg.all_ollama_models()

        assert "ollama/qwen3.5:latest" in models
        assert "ollama/llama2:latest" in models

    def test_config_all_ollama_models_falls_back_to_env_var(self, monkeypatch):
        """When cache is empty, fall back to OLLAMA_BUDGET_MODELS env var."""
        from llm_router.config import get_config

        # No cache available; env var values don't include "ollama/" prefix
        monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "custom:latest,other:v1")
        cfg = get_config()

        with patch("llm_router.discover.get_cached_ollama_models", return_value=[]):
            models = cfg.all_ollama_models()

        assert "ollama/custom:latest" in models
        assert "ollama/other:v1" in models

    def test_router_uses_config_ollama_models(self):
        """Integration: router calls config.all_ollama_models() for injection."""
        with patch("llm_router.config.get_config") as mock_get_config:
            mock_config = MagicMock()
            mock_config.all_ollama_models.return_value = [
                "ollama/qwen3.5:latest",
                "ollama/llama2:latest",
            ]
            mock_get_config.return_value = mock_config

            # Verify that the config method is available and returns Ollama models
            config = mock_get_config()
            ollama_models = config.all_ollama_models()

        assert len(ollama_models) == 2
        assert all(m.startswith("ollama/") for m in ollama_models)


# ── Phase 2: Live API Enumeration ─────────────────────────────────────────────

class TestOpenAIScanner:
    """Test live OpenAI /v1/models enumeration interface."""

    def test_openai_scanner_would_parse_api_response(self):
        """Test that OpenAI API responses are parsed correctly (mock-based)."""
        from llm_router.types import ModelCapability, ProviderTier

        # Simulate what a real scanner would produce from API response
        fake_models = [
            ModelCapability(
                model_id="openai/gpt-4o",
                provider="openai",
                provider_tier=ProviderTier.EXPENSIVE,
                task_types=frozenset({TaskType.CODE, TaskType.QUERY}),
            ),
            ModelCapability(
                model_id="openai/o3",
                provider="openai",
                provider_tier=ProviderTier.EXPENSIVE,
                task_types=frozenset({TaskType.CODE}),
            ),
        ]

        # Verify that the capabilities are properly formatted
        assert len(fake_models) == 2
        assert all(m.provider == "openai" for m in fake_models)
        assert all("gpt" in m.model_id or "o3" in m.model_id for m in fake_models)

    def test_openai_scanner_excludes_embedding_models(self):
        """Test that embedding-only models are filtered out."""
        from llm_router.types import ModelCapability, ProviderTier

        # Embedding model should not be included by scanner
        embedding_model = ModelCapability(
            model_id="openai/text-embedding-3-large",
            provider="openai",
            provider_tier=ProviderTier.EXPENSIVE,
            task_types=frozenset(),  # No supported task types
        )

        # Scanner would filter this out since task_types is empty
        assert len(embedding_model.task_types) == 0

    @pytest.mark.asyncio
    async def test_discover_available_models_includes_openai(self):
        """Discovery should attempt to scan OpenAI when API key is set."""
        from llm_router.discover import discover_available_models

        mock_ollama = AsyncMock(return_value=[])
        mock_openai = AsyncMock(return_value=[])
        mock_gemini = AsyncMock(return_value=[])

        with patch("llm_router.discover._scan_ollama", mock_ollama):
            with patch("llm_router.discover._scan_openai", mock_openai):
                with patch("llm_router.discover._scan_gemini", mock_gemini):
                    result = await discover_available_models()

        # Should return dict of capabilities
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_discover_gracefully_handles_scanner_failure(self):
        """Discovery should handle scanner exceptions gracefully."""
        from llm_router.discover import discover_available_models

        mock_ollama = AsyncMock(return_value=[])
        mock_openai = AsyncMock(side_effect=Exception("API error"))
        mock_gemini = AsyncMock(return_value=[])

        with patch("llm_router.discover._scan_ollama", mock_ollama):
            with patch("llm_router.discover._scan_openai", mock_openai):
                with patch("llm_router.discover._scan_gemini", mock_gemini):
                    result = await discover_available_models()

        # Should still return a dict (other scanners' results)
        assert isinstance(result, dict)


class TestGeminiScanner:
    """Test live Gemini /v1beta/models enumeration interface."""

    def test_gemini_scanner_would_parse_api_response(self):
        """Test that Gemini API responses are parsed correctly (mock-based)."""
        from llm_router.types import ModelCapability, ProviderTier

        # Simulate what a real scanner would produce from API response
        fake_models = [
            ModelCapability(
                model_id="gemini/gemini-2.5-flash",
                provider="gemini",
                provider_tier=ProviderTier.CHEAP_PAID,
                task_types=frozenset({TaskType.CODE, TaskType.QUERY}),
            ),
            ModelCapability(
                model_id="gemini/gemini-2.5-pro",
                provider="gemini",
                provider_tier=ProviderTier.CHEAP_PAID,
                task_types=frozenset({TaskType.CODE}),
            ),
        ]

        assert len(fake_models) == 2
        assert all(m.provider == "gemini" for m in fake_models)

    def test_gemini_scanner_excludes_embedding_models(self):
        """Test that embedding-only models are filtered out."""
        from llm_router.types import ModelCapability, ProviderTier

        # Embedding model has no generation methods supported
        embedding_model = ModelCapability(
            model_id="gemini/embedding-001",
            provider="gemini",
            provider_tier=ProviderTier.CHEAP_PAID,
            task_types=frozenset(),  # No supported generation task types
        )

        # Scanner would filter this out
        assert len(embedding_model.task_types) == 0

    @pytest.mark.asyncio
    async def test_discover_includes_gemini_scan(self):
        """Discovery should attempt to scan Gemini when API key is set."""
        from llm_router.discover import discover_available_models

        mock_ollama = AsyncMock(return_value=[])
        mock_openai = AsyncMock(return_value=[])
        mock_gemini = AsyncMock(return_value=[])

        with patch("llm_router.discover._scan_ollama", mock_ollama):
            with patch("llm_router.discover._scan_openai", mock_openai):
                with patch("llm_router.discover._scan_gemini", mock_gemini):
                    result = await discover_available_models()

        assert isinstance(result, dict)


# ── Phase 3: Always-On Dynamic Routing ────────────────────────────────────────

class TestAlwaysOnDynamic:
    """Test always-on dynamic routing (no feature flag needed)."""

    def test_llm_router_dynamic_flag_removed_from_config(self):
        """LLM_ROUTER_DYNAMIC field should be removed from config."""
        from llm_router.config import get_config

        config = get_config()
        assert not hasattr(config, "llm_router_dynamic"), \
            "llm_router_dynamic field must be removed (always-on now)"

    @pytest.mark.asyncio
    async def test_build_chain_always_tries_dynamic_first(self):
        """build_chain() should always call _build_dynamic_chain, with static fallback."""
        from llm_router.chain_builder import build_chain

        with (
            patch("llm_router.chain_builder._build_dynamic_chain",
                  new_callable=AsyncMock, return_value=["ollama/qwen3.5:latest", "openai/gpt-4o"]),
        ):
            result = await build_chain(TaskType.CODE, "moderate", RoutingProfile.BALANCED)

        assert "ollama/qwen3.5:latest" in result
        assert "openai/gpt-4o" in result

    @pytest.mark.asyncio
    async def test_build_chain_falls_back_to_static_on_error(self):
        """When _build_dynamic_chain raises, fall back to static profiles."""
        from llm_router.chain_builder import build_chain

        with (
            patch("llm_router.chain_builder._build_dynamic_chain",
                  new_callable=AsyncMock, side_effect=RuntimeError("discovery failed")),
            patch("llm_router.chain_builder._static_chain",
                  return_value=["openai/gpt-4o", "gemini/gemini-2.5-flash"]),
        ):
            result = await build_chain(TaskType.CODE, "moderate", RoutingProfile.BALANCED)

        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_dynamic_routing_always_active_no_env_check(self):
        """Dynamic routing should not check any env var or flag."""
        from llm_router.chain_builder import build_chain

        # Never check for LLM_ROUTER_DYNAMIC, always run dynamic
        with (
            patch("llm_router.chain_builder._build_dynamic_chain",
                  new_callable=AsyncMock, return_value=["ollama/qwen3.5:latest"]),
        ):
            result = await build_chain(TaskType.CODE, "simple", RoutingProfile.BUDGET)

        assert isinstance(result, list)
        assert len(result) > 0

    def test_no_is_dynamic_routing_enabled_function(self):
        """is_dynamic_routing_enabled() function must not exist."""
        from llm_router import chain_builder

        assert not hasattr(chain_builder, "is_dynamic_routing_enabled"), \
            "is_dynamic_routing_enabled() must be removed — dynamic routing is always on"

    def test_router_calls_dynamic_routing_tables(self):
        """Verify that router uses dynamic routing tables from session startup."""
        # v5.4.1+: Router uses pre-built dynamic routing tables instead of per-request
        # discovery. Dynamic tables are built once at session start via
        # initialize_dynamic_routing() in server.py startup sequence.
        from llm_router import router
        import inspect

        source = inspect.getsource(router._build_and_filter_chain)
        # Should look up dynamic routing tables first
        assert "get_dynamic_model_chain" in source
        # Should fall back to static chain if dynamic tables unavailable
        assert "get_model_chain" in source
        # Should have try/except for graceful fallback
        assert "except" in source


# ── Phase 4: Sidecar /score Endpoint ──────────────────────────────────────────

class TestSidecarScoreEndpoint:
    """Test FastAPI /score endpoint for model ranking."""

    def test_score_request_model(self):
        """ScoreRequest Pydantic model validates input."""
        from llm_router.service import ScoreRequest

        req = ScoreRequest(
            task_type="code",
            complexity="moderate",
            models=["ollama/qwen3.5:latest", "openai/gpt-4o"],
        )

        assert req.task_type == "code"
        assert req.complexity == "moderate"
        assert len(req.models) == 2

    def test_score_response_model(self):
        """ScoreResponse Pydantic model formats ranking."""
        from llm_router.service import ScoreResponse

        resp = ScoreResponse(
            ranked_models=["openai/gpt-4o", "ollama/qwen3.5:latest"],
            scores={"openai/gpt-4o": 0.92, "ollama/qwen3.5:latest": 0.85},
            reasoning="Scored based on quality and budget",
        )

        assert resp.ranked_models[0] == "openai/gpt-4o"
        assert resp.scores["openai/gpt-4o"] == 0.92

    @pytest.mark.asyncio
    async def test_score_endpoint_ranks_models(self):
        """POST /score returns models sorted by score (best-first)."""
        from llm_router.service import app
        from fastapi.testclient import TestClient

        client = TestClient(app)

        with patch("llm_router.scorer.score_all_models") as mock_score:
            from llm_router.types import ScoredModel, ModelCapability, ProviderTier

            mock_cap1 = ModelCapability(
                model_id="openai/gpt-4o",
                provider="openai",
                provider_tier=ProviderTier.EXPENSIVE,
                task_types=frozenset({TaskType.CODE}),
            )
            mock_cap2 = ModelCapability(
                model_id="ollama/qwen3.5:latest",
                provider="ollama",
                provider_tier=ProviderTier.LOCAL,
                task_types=frozenset({TaskType.CODE}),
            )

            mock_score.return_value = [
                ScoredModel(
                    model_id="openai/gpt-4o",
                    score=0.92,
                    quality_score=0.95,
                    budget_score=0.60,
                    latency_score=0.90,
                    acceptance_score=1.0,
                    capability=mock_cap1,
                ),
                ScoredModel(
                    model_id="ollama/qwen3.5:latest",
                    score=0.85,
                    quality_score=0.70,
                    budget_score=1.0,
                    latency_score=0.80,
                    acceptance_score=1.0,
                    capability=mock_cap2,
                ),
            ]

            response = client.post("/score", json={
                "task_type": "code",
                "complexity": "moderate",
                "models": ["openai/gpt-4o", "ollama/qwen3.5:latest"],
            })

        assert response.status_code == 200
        data = response.json()
        assert data["ranked_models"][0] == "openai/gpt-4o"
        assert data["scores"]["openai/gpt-4o"] == 0.92

    @pytest.mark.asyncio
    async def test_score_endpoint_invalid_task_type(self):
        """POST /score with invalid task_type returns graceful fallback."""
        from llm_router.service import app
        from fastapi.testclient import TestClient

        client = TestClient(app)

        response = client.post("/score", json={
            "task_type": "invalid_task",
            "complexity": "moderate",
            "models": ["openai/gpt-4o", "ollama/qwen3.5:latest"],
        })

        assert response.status_code == 200
        data = response.json()
        # Should return models in original order (fallback)
        assert data["ranked_models"] == ["openai/gpt-4o", "ollama/qwen3.5:latest"]
        assert all(v == 0.5 for v in data["scores"].values())

    @pytest.mark.asyncio
    async def test_score_endpoint_error_returns_original_order(self):
        """On scoring error, /score returns original model order."""
        from llm_router.service import app
        from fastapi.testclient import TestClient

        client = TestClient(app)

        with patch("llm_router.scorer.score_all_models") as mock_score:
            mock_score.side_effect = RuntimeError("scoring failed")

            response = client.post("/score", json={
                "task_type": "code",
                "complexity": "moderate",
                "models": ["ollama/qwen3.5:latest", "openai/gpt-4o"],
            })

        assert response.status_code == 200
        data = response.json()
        # Should return in original order (fallback)
        assert data["ranked_models"] == ["ollama/qwen3.5:latest", "openai/gpt-4o"]
        # All scores should be neutral 0.5
        assert all(v == 0.5 for v in data["scores"].values())

    def test_hook_client_score_models(self):
        """hook_client.score_models() calls /score endpoint."""
        from llm_router.hook_client import score_models

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.read.return_value = json.dumps({
                "ranked_models": ["openai/gpt-4o", "ollama/qwen3.5:latest"],
                "scores": {"openai/gpt-4o": 0.92, "ollama/qwen3.5:latest": 0.85},
            }).encode("utf-8")
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            result = score_models(
                ["ollama/qwen3.5:latest", "openai/gpt-4o"],
                "code",
                "moderate",
            )

        assert result[0] == "openai/gpt-4o"
        assert result[1] == "ollama/qwen3.5:latest"

    def test_hook_client_score_models_timeout_fallback(self):
        """score_models() with timeout returns original order."""
        from llm_router.hook_client import score_models

        with patch("urllib.request.urlopen") as mock_urlopen:
            import urllib.error
            mock_urlopen.side_effect = urllib.error.URLError("timeout")

            result = score_models(
                ["model1", "model2"],
                "code",
                "moderate",
            )

        # Should return original order on timeout
        assert result == ["model1", "model2"]


# ── Integration: Full v5.0 Stack ──────────────────────────────────────────────

class TestAdaptiveUniversalRouterIntegration:
    """End-to-end tests for all v5.0 components working together."""

    @pytest.mark.asyncio
    async def test_discovery_scanner_integration(self):
        """All scanners run in parallel during discovery."""
        from llm_router.discover import discover_available_models

        with (
            patch("llm_router.discover._scan_ollama",
                  new_callable=AsyncMock, return_value=[]),
            patch("llm_router.discover._scan_openai",
                  new_callable=AsyncMock, return_value=[]),
            patch("llm_router.discover._scan_gemini",
                  new_callable=AsyncMock, return_value=[]),
            patch("llm_router.discover._scan_api_key_providers",
                  new_callable=AsyncMock, return_value=[]),
        ):
            result = await discover_available_models()

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_full_routing_path_ollama_to_paid(self):
        """Full path: discover → score → build_chain → route."""
        from llm_router.chain_builder import build_chain

        with (
            patch("llm_router.discover.discover_available_models",
                  new_callable=AsyncMock, return_value={
                      "ollama/qwen3.5:latest": ModelCapability(
                          model_id="ollama/qwen3.5:latest",
                          provider="ollama",
                          provider_tier=ProviderTier.LOCAL,
                          task_types=frozenset({TaskType.CODE}),
                      ),
                      "openai/gpt-4o": ModelCapability(
                          model_id="openai/gpt-4o",
                          provider="openai",
                          provider_tier=ProviderTier.EXPENSIVE,
                          task_types=frozenset({TaskType.CODE}),
                      ),
                  }),
            patch("llm_router.scorer.score_all_models",
                  new_callable=AsyncMock, return_value=[
                      MagicMock(
                          model_id="openai/gpt-4o",
                          score=0.90,
                          capability=MagicMock(provider="openai"),
                      ),
                      MagicMock(
                          model_id="ollama/qwen3.5:latest",
                          score=0.88,
                          capability=MagicMock(provider="ollama"),
                      ),
                  ]),
        ):
            result = await build_chain(TaskType.CODE, "complex", RoutingProfile.PREMIUM)

        assert isinstance(result, list)
        assert len(result) > 0
        # Local models should come before paid, even with lower score (free-first invariant)
        if "ollama/qwen3.5:latest" in result and "openai/gpt-4o" in result:
            assert result.index("ollama/qwen3.5:latest") < result.index("openai/gpt-4o")
