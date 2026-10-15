"""Owner decision D-101: a cloud model's context-pack window is its registry context
window capped at ``context_pack.CLOUD_WINDOW_CAP`` (65,536), not a flat 8,192 / 32,768.
Unknown models keep the old defaults. Synthetic model ids and text only."""
from __future__ import annotations

import pytest

from llm_router import context_pack, model_registry, router, sdk
from llm_router.hooks.direct_executor import ModelSpec
from llm_router.model_registry import ModelMetadata, ModelRegistry
from llm_router.lineage import Tier


def _meta(mid: str, window: int) -> ModelMetadata:
    return ModelMetadata(id=mid, provider=mid.split("/")[0], tier=Tier.MID, quality_score=0.5,
                         price_per_1m_input_usd=1.0, price_per_1m_output_usd=1.0, context_window=window)


@pytest.fixture
def registry(monkeypatch):
    reg = ModelRegistry.from_models([
        _meta("anthropic/claude-sonnet-9", 1_000_000), _meta("openai/gpt-9", 400_000),
        _meta("openai/gpt-9-mini", 128_000), _meta("groq/tiny-8b", 8_192), _meta("gemini/flash-9", 0),
        _meta("anthropic/claude-haiku-9-20990101", 200_000), _meta("anthropic/claude-haiku-8", 300_000),
        _meta("ollama/local-x", 999), _meta("groq/llama-3.1-8b-instant", 8_192),
    ])
    monkeypatch.setattr(model_registry, "_DEFAULT_REGISTRY", reg)
    return reg


@pytest.mark.parametrize("model,want", [
    ("anthropic/claude-sonnet-9", 1_000_000),
    ("anthropic/claude-sonnet-9-5", 1_000_000),       # extends a registry id at "-"
    ("openai/gpt-9-mini", 128_000),                    # exact id beats the shorter prefix
    ("openai/gpt-9-mini-2026", 128_000),               # longest matching prefix
    ("openai/gpt-95", 128_000),                        # no boundary: not gpt-9's 400k, family min
    ("codex/gpt-9", 400_000),                          # provider alias
    ("gemini/flash-9", None),                          # window 0 = unknown
    ("anthropic/claude-sonnet-4-6", 1_000_000),        # family fallback: the routed Sonnet id
    ("anthropic/claude-haiku-4-5", 200_000),           # family fallback takes the SMALLEST window
    ("anthropic/claude-haiku", None),                  # a bare family is not a model
    ("anthropic/claude-mythic-1", None),               # no family in the registry
    ("groq/llama-3.3-70b-versatile", None),            # family spans a window under the cap: unknown
    ("unknown/model", None), ("no-slash", None), (None, None),
])
def test_registry_context_window(registry, model, want):
    assert model_registry.context_window(model) == want


def test_a_broken_registry_is_unknown_not_an_error(monkeypatch):
    def boom():
        raise RuntimeError("synthetic broken registry")

    monkeypatch.setattr(model_registry, "_default_registry", boom)
    assert model_registry.context_window("openai/gpt-9") is None
    assert router._pack_window("openai/gpt-9", "mcp") == router._MCP_NON_LOCAL_WINDOW


@pytest.mark.parametrize("model,door,want", [
    ("anthropic/claude-sonnet-9", "mcp", context_pack.CLOUD_WINDOW_CAP),
    ("anthropic/claude-sonnet-9", "gateway", context_pack.CLOUD_WINDOW_CAP),
    ("groq/tiny-8b", "mcp", 8_192),                    # a smaller real window is kept
    ("unknown/model", "mcp", 8_192),                   # unknown: the old defaults
    ("unknown/model", "gateway", 32_768),
    ("codex", "gateway", 32_768),                      # a bare provider name (CLI dispatch)
])
def test_router_pack_window(registry, model, door, want):
    assert router._pack_window(model, door) == want


def test_a_family_with_a_small_member_never_lowers_an_unlisted_model(registry):
    """Review 514 (D1): groq/llama-3.3-70b-versatile is routed and unlisted; its only family
    member is an 8k model. It keeps the old defaults instead of dropping to 8k."""
    assert router._pack_window("groq/llama-3.3-70b-versatile", "gateway") == 32_768
    assert router._pack_window("groq/llama-3.3-70b-versatile", "mcp") == 8_192


def test_router_pack_window_keeps_the_local_num_ctx(registry):
    from llm_router import local_models

    assert router._pack_window("ollama/qwen3.5:latest", "mcp") == local_models.num_ctx("qwen3.5:latest")
    # a registry row for a local model never overrides its num_ctx (review 514, finding 5)
    assert router._pack_window("ollama/local-x", "mcp") == local_models.num_ctx("local-x") != 999
    assert sdk._pack_window(ModelSpec("ollama", "local-x")) != 999


def test_sdk_pack_window(registry):
    assert sdk._pack_window(ModelSpec("openai", "gpt-9")) == context_pack.CLOUD_WINDOW_CAP
    assert sdk._pack_window(ModelSpec("groq", "tiny-8b")) == 8_192
    from llm_router.hooks.direct_executor import _local_num_ctx

    assert sdk._pack_window(ModelSpec("xai", "grok-95")) == _local_num_ctx("grok-95")       # unknown: as before D-101
    assert sdk._pack_window(ModelSpec("ollama", "qwen3.5:latest")) == _local_num_ctx("qwen3.5:latest")


def test_sdk_builds_the_pack_for_the_first_called_models_window(registry, monkeypatch):
    seen = {}

    def fake_build_pack(request, messages, **kw):
        seen["window"] = kw["target_window"]
        return context_pack.ContextPack(request=request, mode="full")

    monkeypatch.setattr(context_pack, "build_pack", fake_build_pack)
    chain = [ModelSpec("claude", "sonnet"), ModelSpec("openai", "gpt-9"), ModelSpec("ollama", "qwen3.5:latest")]
    sdk._pack_kwargs("synthetic request", [{"role": "user", "content": "hi"}], chain, "s1")
    assert seen["window"] == context_pack.CLOUD_WINDOW_CAP     # claude is never called by the SDK


def test_a_conversation_under_the_cap_goes_whole_to_a_known_cloud_model(registry):
    """~20k tokens: pack mode at the old 8,192 window, the whole conversation at 65,536."""
    turns = []
    for i in range(40):
        turns.append({"role": "user", "content": f"synthetic step {i}: " + "alpha beta gamma delta " * 100})
        turns.append({"role": "assistant", "content": f"done {i}"})
    old = context_pack.build_pack("synthetic final", turns, door="mcp", target_window=8_192)
    new = context_pack.build_pack("synthetic final", turns, door="mcp",
                                  target_window=router._pack_window("openai/gpt-9", "mcp"))
    assert (old.mode, new.mode) == ("pack", "full")
    assert len(new.recent) == len(turns)
