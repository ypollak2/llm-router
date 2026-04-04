"""Tests for the semantic dedup cache (semantic_cache.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from llm_router.types import LLMResponse, TaskType


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_embedding(dim: int = 768, val: float = 1.0) -> list[float]:
    """Return a unit vector of length dim."""
    import math
    v = [val] * dim
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v]


def _make_response(content: str = "cached answer") -> LLMResponse:
    return LLMResponse(
        content=content, model="openai/gpt-4o",
        input_tokens=10, output_tokens=5,
        cost_usd=0.001, latency_ms=100, provider="openai",
    )


# ── cosine similarity ────────────────────────────────────────────────────────

def test_cosine_identical_vectors():
    from llm_router.semantic_cache import _cosine_similarity
    v = _make_embedding()
    assert abs(_cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_vectors():
    from llm_router.semantic_cache import _cosine_similarity
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(_cosine_similarity(a, b)) < 1e-9


def test_cosine_zero_vector():
    from llm_router.semantic_cache import _cosine_similarity
    assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ── _get_embedding ───────────────────────────────────────────────────────────

def test_get_embedding_returns_none_on_error():
    from llm_router.semantic_cache import _get_embedding
    result = _get_embedding("hello", "http://localhost:99999")
    assert result is None


def test_get_embedding_parses_response():
    import json
    from llm_router.semantic_cache import _get_embedding

    embedding = _make_embedding(4)
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(return_value=json.dumps({"embedding": embedding}).encode())

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _get_embedding("hello", "http://localhost:11434")

    assert result == embedding


# ── check (cache miss when Ollama disabled) ──────────────────────────────────

@pytest.mark.asyncio
async def test_check_returns_none_when_ollama_disabled(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    # Reset config singleton
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    from llm_router.semantic_cache import check
    result = await check("hello world", TaskType.QUERY)
    assert result is None


@pytest.mark.asyncio
async def test_check_returns_none_when_embedding_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    with patch("llm_router.semantic_cache._get_embedding", return_value=None):
        from llm_router.semantic_cache import check
        result = await check("hello world", TaskType.QUERY)
    assert result is None


# ── check + store round-trip ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_store_then_check_hit(monkeypatch, tmp_path):
    """A stored response is returned as a hit for a near-identical prompt."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb = _make_embedding()
    response = _make_response("the answer is 42")

    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import check, store
        # Store
        await store("what is the meaning of life?", TaskType.QUERY, response)
        # Check same embedding → should hit
        hit = await check("what is the meaning of life?", TaskType.QUERY)

    assert hit is not None
    assert hit.content == "the answer is 42"
    assert hit.provider == "cache"
    assert hit.cost_usd == 0.0


@pytest.mark.asyncio
async def test_check_miss_when_similarity_below_threshold(monkeypatch, tmp_path):
    """Low-similarity embeddings do not produce a cache hit."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb_a = _make_embedding(4, val=1.0)   # [0.5, 0.5, 0.5, 0.5]
    emb_b = _make_embedding(4, val=-1.0)  # opposite direction

    response = _make_response("stored answer")

    from llm_router.semantic_cache import check, store

    with patch("llm_router.semantic_cache._get_embedding", return_value=emb_a):
        await store("prompt A", TaskType.QUERY, response)

    with patch("llm_router.semantic_cache._get_embedding", return_value=emb_b):
        hit = await check("totally different prompt", TaskType.QUERY, threshold=0.95)

    assert hit is None


@pytest.mark.asyncio
async def test_task_type_scope_isolation(monkeypatch, tmp_path):
    """A code cache entry does not match a query lookup."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb = _make_embedding()
    response = _make_response("code answer")

    from llm_router.semantic_cache import check, store

    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        await store("write a function", TaskType.CODE, response)
        hit = await check("write a function", TaskType.QUERY, threshold=0.95)

    assert hit is None  # different task_type → no match


@pytest.mark.asyncio
async def test_store_skips_cache_provider_responses(monkeypatch, tmp_path):
    """Responses from the cache itself are never re-stored (no double-caching)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    cache_resp = LLMResponse(
        content="cached", model="cache/gpt-4o",
        input_tokens=0, output_tokens=0,
        cost_usd=0.0, latency_ms=0, provider="cache",
    )

    emb = _make_embedding()
    mock_embed = MagicMock(return_value=emb)

    with patch("llm_router.semantic_cache._get_embedding", mock_embed):
        from llm_router.semantic_cache import store
        await store("prompt", TaskType.QUERY, cache_resp)

    # _get_embedding should not be called for cache-provider responses
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_store_skips_empty_content(monkeypatch, tmp_path):
    """Responses with empty content are not stored."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    empty_resp = LLMResponse(
        content="", model="openai/gpt-4o",
        input_tokens=10, output_tokens=0,
        cost_usd=0.001, latency_ms=100, provider="openai",
    )
    mock_embed = MagicMock(return_value=_make_embedding())

    with patch("llm_router.semantic_cache._get_embedding", mock_embed):
        from llm_router.semantic_cache import store
        await store("prompt", TaskType.QUERY, empty_resp)

    mock_embed.assert_not_called()
