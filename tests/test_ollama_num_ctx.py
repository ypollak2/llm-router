"""P2 — configurable Ollama num_ctx.

Ollama's default 4096-token context window silently overflows page-sized
prompts/outputs and returns empty content (surfaced as EmptyResponseError →
chain failover). LLM_ROUTER_OLLAMA_NUM_CTX lets operators raise it. The param is
opt-in (unset = keep Ollama's default) and only applies to ollama/* models.
"""
from __future__ import annotations

import types

import pytest

from llm_router.providers import _ollama_num_ctx, call_llm


# ── pure config helper ───────────────────────────────────────────────────────

def test_num_ctx_unset_returns_none(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_NUM_CTX", raising=False)
    assert _ollama_num_ctx() is None


def test_num_ctx_parses_positive_int(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    assert _ollama_num_ctx() == 8192


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_num_ctx_invalid_returns_none(monkeypatch, bad):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", bad)
    assert _ollama_num_ctx() is None


# ── end-to-end: num_ctx reaches the litellm call for ollama only ──────────────

def _fake_litellm(captured: dict):
    async def _acompletion(**kwargs):
        captured.clear()
        captured.update(kwargs)
        usage = types.SimpleNamespace(
            prompt_tokens=1, completion_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice], usage=usage)
    return _acompletion


@pytest.mark.asyncio
async def test_num_ctx_passed_for_ollama_when_set(monkeypatch):
    import litellm
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    await call_llm("ollama/qwen2.5:7b", [{"role": "user", "content": "hi"}])
    assert captured.get("num_ctx") == 8192


@pytest.mark.asyncio
async def test_num_ctx_absent_for_ollama_when_unset(monkeypatch):
    import litellm
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_NUM_CTX", raising=False)
    await call_llm("ollama/qwen2.5:7b", [{"role": "user", "content": "hi"}])
    assert "num_ctx" not in captured


@pytest.mark.asyncio
async def test_num_ctx_never_sent_for_non_ollama(monkeypatch):
    import litellm
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    await call_llm("openai/gpt-4o-mini", [{"role": "user", "content": "hi"}])
    assert "num_ctx" not in captured
