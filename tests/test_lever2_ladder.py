"""Lever ② — escalation-ladder + chain hygiene.

Two defects surfaced by the full-metering benchmark (they had been masked by the
Codex-broker leak), which together caused hard prompts to exhaust to q=1:

1. **Chain hygiene:** an embedding-only Ollama model (`nomic-embed-text`) was tagged
   generation-capable in the discovery cache and injected into generation chains,
   where it always errors (`"does not support generate"`).
2. **Escalation ladder:** the complex/premium chain jumped from `openai/o3` straight
   to a slow local model — when o3 rate-limited, the chain exhausted. A cheap metered
   mid-tier (`gpt-4o-mini → gpt-4o`) is now injected BEFORE o3 (cheapest-capable-first;
   quality-gated escalation promotes to o3 when a cheap answer scores low).
"""

from unittest.mock import AsyncMock, patch

import pytest

import llm_router.session_broker as sb
from llm_router.discover import _is_embedding_model, _update_discovery_cache
from llm_router.router import _build_and_filter_chain, provider_from_model
from llm_router.types import Complexity, RoutingProfile, TaskType


def _cfg():
    from llm_router.config import get_config
    return get_config()


# ── Chain hygiene: embedding models never enter generation chains ────────────

@pytest.mark.parametrize("name", [
    "nomic-embed-text:latest", "mxbai-embed-large", "snowflake-arctic-embed2",
    "bge-large", "gte-base", "e5-mistral", "all-minilm",
])
def test_embedding_names_detected(name):
    assert _is_embedding_model(name)


@pytest.mark.parametrize("name", [
    "qwen3:32b", "qwen2.5-coder:7b", "llama3.3", "hermes3:8b", "devstral",
])
def test_generation_models_not_flagged(name):
    assert not _is_embedding_model(name)


def test_embedding_family_hint_detected():
    assert _is_embedding_model("weird-name", {"details": {"family": "nomic-bert"}})
    assert not _is_embedding_model("weird-name", {"details": {"family": "qwen3"}})


def test_discovery_cache_excludes_embedding_models(tmp_path, monkeypatch):
    """fail-before/pass-after: nomic-embed-text must NOT be cached as a routable
    model; a real generation model alongside it must remain."""
    import json
    cache = tmp_path / "discovery.json"
    monkeypatch.setattr("llm_router.discover._DISCOVERY_CACHE", str(cache))
    _update_discovery_cache([
        {"name": "nomic-embed-text:latest"},
        {"name": "qwen3:32b"},
        {"name": "mxbai-embed-large", "details": {"family": "bert"}},
    ])
    stored = json.loads(cache.read_text())["models"]
    ids = set(stored)
    assert "ollama/qwen3:32b" in ids
    assert "ollama/nomic-embed-text:latest" not in ids, "embedding model must not be cached"
    assert "ollama/mxbai-embed-large" not in ids


def test_read_path_filters_embedding_from_stale_cache(tmp_path, monkeypatch):
    """A cache written BEFORE this fix (stale, 24h TTL) still contains
    nomic-embed-text. The read path must exclude it too, so it can't reach a
    routing chain until the cache refreshes."""
    import json
    import time
    from llm_router.discover import get_cached_ollama_models
    cache = tmp_path / "discovery.json"
    # Hand-write a stale-style cache that predates the write filter.
    def _entry(mid):
        return {"model_id": mid, "provider": "ollama", "provider_tier": "local",
                "task_types": ["query", "generate", "analyze", "code"]}
    cache.write_text(json.dumps({
        "cached_at": time.time(),
        "models": {
            "ollama/nomic-embed-text:latest": _entry("ollama/nomic-embed-text:latest"),
            "ollama/qwen3:32b": _entry("ollama/qwen3:32b"),
        },
    }))
    monkeypatch.setattr("llm_router.discover._DISCOVERY_CACHE", str(cache))
    got = get_cached_ollama_models()
    assert "ollama/qwen3:32b" in got
    assert "ollama/nomic-embed-text:latest" not in got, \
        f"read path must filter embedding models from a stale cache, got {got}"


# ── Escalation ladder: metered mid-tier before o3 ────────────────────────────

async def _premium_chain(monkeypatch, *, block=None, profile=RoutingProfile.PREMIUM,
                         complexity=Complexity.COMPLEX):
    sb._provider_cache = None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    if block is not None:
        monkeypatch.setenv("LLM_ROUTER_BLOCK_PROVIDERS", block)
    with patch("llm_router.session_broker.broker_providers",
               new=AsyncMock(return_value=frozenset())):
        return await _build_and_filter_chain(
            TaskType.ANALYZE, profile, None, "complex", complexity, _cfg(),
        )


@pytest.mark.asyncio
async def test_mid_tier_injected_before_o3(mock_env, monkeypatch):
    """pass-after: gpt-4o-mini and gpt-4o appear, and both sit BEFORE o3 — so a
    cheap reliable metered fallback is tried before the expensive reasoning model.
    Fail-before: no injection existed, so the chain had o3 but no gpt-4o-mini."""
    chain = await _premium_chain(monkeypatch)
    assert "openai/gpt-4o-mini" in chain and "openai/gpt-4o" in chain, chain
    assert "openai/o3" in chain, chain
    i_mini = chain.index("openai/gpt-4o-mini")
    i_4o = chain.index("openai/gpt-4o")
    i_o3 = chain.index("openai/o3")
    assert i_mini < i_o3 and i_4o < i_o3, f"mid-tier must precede o3, got {chain}"
    assert i_mini < i_4o, f"mini should precede 4o, got {chain}"


@pytest.mark.asyncio
async def test_mid_tier_respects_block_providers(mock_env, monkeypatch):
    """If openai is hard-blocked, no metered mid-tier is injected (and o3 is gone too)."""
    chain = await _premium_chain(monkeypatch, block="openai")
    assert not any(provider_from_model(m) == "openai" for m in chain), \
        f"openai blocked — no openai models at all, got {chain}"


@pytest.mark.asyncio
async def test_mid_tier_not_forced_for_budget(mock_env, monkeypatch):
    """BUDGET stays local-first — the premium mid-tier injection must not fire and
    reorder cheap local models behind metered ones. (gpt-4o-mini may still be a
    legitimate base-chain *tail* member for budget; what matters is that every
    local model precedes every metered one — i.e. my injection did NOT run.)"""
    chain = await _premium_chain(monkeypatch, profile=RoutingProfile.BUDGET,
                                 complexity=Complexity.SIMPLE)
    first_openai = next((i for i, m in enumerate(chain)
                         if provider_from_model(m) == "openai"), len(chain))
    last_ollama = max((i for i, m in enumerate(chain)
                       if provider_from_model(m) == "ollama"), default=-1)
    assert last_ollama < first_openai, \
        f"BUDGET must keep local models ahead of metered ones, got {chain}"


@pytest.mark.asyncio
async def test_mid_tier_no_duplicates(mock_env, monkeypatch):
    """Idempotent: gpt-4o-mini/gpt-4o appear at most once even if base-present."""
    chain = await _premium_chain(monkeypatch)
    assert chain.count("openai/gpt-4o-mini") <= 1
    assert chain.count("openai/gpt-4o") <= 1
