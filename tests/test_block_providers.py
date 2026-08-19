"""Gate-17: LLM_ROUTER_BLOCK_PROVIDERS — a hard provider block on EVERY routing path.

Context: `LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS` disables only a provider's LOCAL
subprocess CLI and deliberately still lets a session-broker daemon serve it (the
headless gateway's free Codex/Gemini path — see `test_headless_chain_order`). That
left no way to force a provider fully off, so honest cost benchmarks leaked: a
subscription served via broker was recorded at unpriced $0 (unclassified spend).

`LLM_ROUTER_BLOCK_PROVIDERS` closes that: a blocked provider is removed from the chain
no matter how it entered (base policy chain, injection, or broker re-assert). These
tests pin both directions — blocked providers vanish; unblocked broker routing and
the `DISABLE_SUBPROCESS_BACKENDS`-still-allows-broker semantics are untouched.
"""

from unittest.mock import AsyncMock, patch

import pytest

import llm_router.session_broker as sb
from llm_router.router import (
    _blocked_providers,
    _build_and_filter_chain,
    provider_from_model,
    route_and_call,
)
from llm_router.types import Complexity, RoutingProfile, TaskType


def _cfg():
    from llm_router.config import get_config
    return get_config()


async def _chain(monkeypatch, *, block=None, disable=None, broker=frozenset()):
    sb._provider_cache = None
    if block is not None:
        monkeypatch.setenv("LLM_ROUTER_BLOCK_PROVIDERS", block)
    if disable is not None:
        monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", disable)
    with patch("llm_router.session_broker.broker_providers",
               new=AsyncMock(return_value=broker)):
        return await _build_and_filter_chain(
            TaskType.ANALYZE, RoutingProfile.PREMIUM, None, "complex",
            Complexity.COMPLEX, _cfg(),
        )


def test_blocked_providers_parses_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_BLOCK_PROVIDERS", "codex, Gemini_CLI ,")
    assert _blocked_providers() == frozenset({"codex", "gemini_cli"})
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    assert _blocked_providers() == frozenset()


@pytest.mark.asyncio
async def test_blocked_codex_absent_even_when_broker_offers_it(mock_env, monkeypatch):
    """The Gate-17 fix: block codex + broker offers it → codex is not in the chain.
    Fail-before: no code read LLM_ROUTER_BLOCK_PROVIDERS, so the broker re-assert
    fronted codex regardless — this test failed."""
    chain = await _chain(monkeypatch, block="codex", broker=frozenset({"codex"}))
    assert chain, "chain should not be empty (other providers remain)"
    assert not any(provider_from_model(m) == "codex" for m in chain), \
        f"codex is blocked — must not appear on any path, got {chain[:4]}"


@pytest.mark.asyncio
async def test_blocked_gemini_cli_absent_even_when_broker_offers_it(mock_env, monkeypatch):
    chain = await _chain(monkeypatch, block="gemini_cli", broker=frozenset({"gemini_cli"}))
    assert chain
    assert not any(provider_from_model(m) == "gemini_cli" for m in chain), \
        f"gemini_cli is blocked — must not appear, got {chain[:4]}"


@pytest.mark.asyncio
async def test_block_removes_base_chain_provider(mock_env, monkeypatch):
    """The authoritative filter removes providers baked into the BASE policy
    chain (not just injected/broker ones). Env-adaptive: block whichever
    provider the chain actually contains here (openai-only in the hermetic CI
    env; more locally). Emptying the chain is a valid outcome of blocking the
    sole provider — the router surfaces the documented 'all candidates blocked'
    warning — so we assert the blocked provider is GONE, not that a chain
    remains."""
    unblocked = await _chain(monkeypatch, broker=frozenset())
    present = sorted({provider_from_model(m) for m in unblocked})
    assert present, "precondition: unblocked chain is non-empty"
    target = present[0]
    blocked = await _chain(monkeypatch, block=target, broker=frozenset())
    assert not any(provider_from_model(m) == target for m in blocked), \
        f"blocked base-chain provider {target!r} must be filtered, got {blocked}"


@pytest.mark.asyncio
async def test_unblocked_codex_still_broker_injected(mock_env, monkeypatch):
    """No over-correction: without a block, broker-offered codex is still injected
    (the headless free path keeps working)."""
    chain = await _chain(monkeypatch, disable="codex,gemini_cli", broker=frozenset({"codex"}))
    assert chain
    assert chain[0].startswith("codex/"), \
        f"unblocked broker codex should still be fronted, got {chain[:3]}"


@pytest.mark.asyncio
async def test_disable_subprocess_still_allows_broker_codex(mock_env, monkeypatch):
    """Regression guard: BLOCK_PROVIDERS and DISABLE_SUBPROCESS_BACKENDS are
    DISTINCT. Disabling the codex subprocess must STILL allow broker-backed codex
    (the gateway daemon depends on this) — our change did not touch that path."""
    chain = await _chain(monkeypatch, disable="codex,gemini_cli", broker=frozenset({"codex"}))
    assert any(provider_from_model(m) == "codex" for m in chain), \
        "DISABLE_SUBPROCESS_BACKENDS must NOT block the broker path — daemon needs it"


@pytest.mark.asyncio
async def test_block_wins_over_disable_broker_allowance(mock_env, monkeypatch):
    """When BOTH are set for codex, the hard block wins: even though the broker
    would otherwise serve it, a blocked provider is gone."""
    chain = await _chain(
        monkeypatch, block="codex", disable="codex,gemini_cli",
        broker=frozenset({"codex"}),
    )
    assert chain
    assert not any(provider_from_model(m) == "codex" for m in chain), \
        f"block must override the broker allowance, got {chain[:4]}"


@pytest.mark.asyncio
async def test_blocking_all_providers_raises_block_diagnostic(temp_db, mock_env, monkeypatch):
    """#32: when LLM_ROUTER_BLOCK_PROVIDERS empties the chain, the error must name the
    block (so the fix is 'unblock a provider'), not the misleading generic
    'install Ollama / set a key' message."""
    monkeypatch.setenv(
        "LLM_ROUTER_BLOCK_PROVIDERS",
        "openai,ollama,anthropic,codex,gemini_cli,deepseek,openrouter,xai,perplexity,google,fireworks,groq",
    )
    sb._provider_cache = None
    with pytest.raises(ValueError, match="LLM_ROUTER_BLOCK_PROVIDERS"):
        await route_and_call(TaskType.ANALYZE, "analyze this in depth", complexity_hint="complex")
