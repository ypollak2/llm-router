"""Ollama hybrid-reasoning models must not think by default.

Evidence 2026-09-27, this machine: ollama/qwen3.5:latest is a THINKING model.
Called via Ollama's own /api/chat with default settings, a 5-line answer
returned `message.content` of 0 chars and `message.thinking` of 28,663 chars
(minutes). With `"think": false` it returned the exact 55-char answer in 3s.

Through the router (providers.call_llm / call_llm_stream_events — the path
LiteLLM's `ollama/` provider takes, used by the MCP `llm()` tool) this showed
up as a 78s call that hit the fallback (routing_quality.jsonl row 87a13c87,
fallback_reason=timeout) and, separately, a hard `litellm.Timeout` at the
120s ceiling ("All models failed"). PR #167 puts qwen3.5 first in the query
chains, so this hits every local query route.

Every direct-HTTP Ollama caller in this repo (agent_loop.py, auto-route.py,
tool_intercept.py, direct_executor.py, vision_registry.py) already hard-codes
`"think": False`. providers.py — the one LiteLLM-mediated path — did not.
This file locks that in, plus the operator escape hatch (LLM_ROUTER_OLLAMA_THINK=1),
plus the streaming path's separate gap: it had NO non-empty-content check at
all, so a model that streams only reasoning/thinking deltas (never a
`delta.content` chunk) would complete as an empty *successful* answer.
"""
from __future__ import annotations

import types

import pytest

from llm_router.providers import _ollama_think_enabled, call_llm, call_llm_stream_events
from llm_router.inference_robustness import EmptyResponseError


# ── pure config helper ───────────────────────────────────────────────────────

def test_think_enabled_is_false_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_THINK", raising=False)
    assert _ollama_think_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "True", "yes", "on"])
def test_think_enabled_true_values(monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_THINK", val)
    assert _ollama_think_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_think_enabled_false_values(monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_THINK", val)
    assert _ollama_think_enabled() is False


# ── end-to-end (non-streaming): think reaches the litellm call for ollama ────

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
async def test_think_false_sent_by_default_for_ollama(monkeypatch):
    import litellm
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_THINK", raising=False)
    await call_llm("ollama/qwen3.5:latest", [{"role": "user", "content": "hi"}])
    assert captured.get("think") is False


@pytest.mark.asyncio
async def test_think_env_override_reenables_thinking(monkeypatch):
    """LLM_ROUTER_OLLAMA_THINK=1 restores Ollama's own default: no `think`
    key forced into the request at all."""
    import litellm
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_THINK", "1")
    await call_llm("ollama/qwen3.5:latest", [{"role": "user", "content": "hi"}])
    assert "think" not in captured


@pytest.mark.asyncio
async def test_think_never_sent_for_non_ollama(monkeypatch):
    import litellm
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_THINK", raising=False)
    await call_llm("openai/gpt-4o-mini", [{"role": "user", "content": "hi"}])
    assert "think" not in captured


# ── end-to-end (streaming): same request-side fix ────────────────────────────

class _StreamDelta:
    def __init__(self, content: str | None = None, reasoning_content: str | None = None):
        self.content = content
        self.reasoning_content = reasoning_content


class _StreamChoice:
    def __init__(self, delta: _StreamDelta):
        self.delta = delta


class _StreamChunk:
    def __init__(self, delta: _StreamDelta | None = None, usage=None):
        self.choices = [_StreamChoice(delta)] if delta is not None else []
        self.usage = usage


async def _mock_stream(*chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_think_false_sent_by_default_for_ollama_streaming(monkeypatch):
    import litellm
    captured: dict = {}

    async def _acompletion(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _mock_stream(_StreamChunk(_StreamDelta(content="ok")))

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_THINK", raising=False)
    events = [e async for e in call_llm_stream_events(
        "ollama/qwen3.5:latest", [{"role": "user", "content": "hi"}]
    )]
    assert captured.get("think") is False
    assert any(e["type"] == "delta" for e in events)


# ── empty content + present thinking must FAIL, never succeed empty ─────────

@pytest.mark.asyncio
async def test_empty_content_with_thinking_fails_streaming(monkeypatch):
    """A hybrid-reasoning model that streams ONLY reasoning/thinking deltas
    (LiteLLM exposes those as `delta.reasoning_content`, never `delta.content`)
    must be treated as a failed attempt, not a successful empty answer, so
    router.py's dispatch loop falls through to the next model in the chain."""
    import litellm

    async def _acompletion(**kwargs):
        return _mock_stream(
            _StreamChunk(_StreamDelta(content=None, reasoning_content="t" * 28663)),
            _StreamChunk(usage=types.SimpleNamespace(prompt_tokens=12, completion_tokens=0)),
        )

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    with pytest.raises(EmptyResponseError):
        async for _ in call_llm_stream_events(
            "ollama/qwen3.5:latest", [{"role": "user", "content": "hi"}]
        ):
            pass


@pytest.mark.asyncio
async def test_non_empty_content_streaming_still_succeeds(monkeypatch):
    """Guard against over-correction: real content must still stream through."""
    import litellm

    async def _acompletion(**kwargs):
        return _mock_stream(
            _StreamChunk(_StreamDelta(content="hello")),
            _StreamChunk(usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
        )

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    events = [e async for e in call_llm_stream_events(
        "ollama/qwen3.5:latest", [{"role": "user", "content": "hi"}]
    )]
    assert any(e["type"] == "usage" for e in events)
