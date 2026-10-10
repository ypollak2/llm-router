"""N22: a floor-served call left only the safety-net usage row, and paid 120 s per extra local model.

M0-3 rerun3 (2026-10-10, ``routing_quality.jsonl``): calls A2/B2/B6 got a qwen3-coder answer that failed the
syntax gate, then walked two more local models that each hit litellm's 120 s timeout (268 s), then served the
rejected answer as the exhaustion floor. The floor path wrote ``routing_decisions`` but never ``usage``, so the
only usage row was the LEDGER-EVERY-EXIT-1 safety net (provider=none, ``error_unledgered_exit``, success=0).
Codex was not involved: ``provider_reset.json`` had it benched until 2026-10-15 and it was not in the chains.
"""
from __future__ import annotations

import litellm
import pytest

from llm_router import router
from llm_router.types import LLMResponse
from tests.test_ledger_every_exit import SID, _llm, _q, _real_router, cache_env, caller  # noqa: F401

CODER = "ollama/qwen3-coder:30b"
SLOW_A = "ollama/llamacpp:aaaa"
SLOW_B = "ollama/llamacpp:bbbb"
REMOTE = "openai/gpt-4o-mini"
BAD = "```python\ndef broken(:\n    pass\n```"  # fails the code syntax gate


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LLM_ROUTER_GATES", "on")
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S", raising=False)
    router._local_timeout_at.clear()
    yield
    router._local_timeout_at.clear()


def _provider(calls, slow=()):
    def _call(model, messages, **kw):
        calls.append(model)
        if model in slow:
            raise litellm.Timeout(message="Timeout passed=120.0", model=model, llm_provider="ollama")
        good = model == REMOTE
        return LLMResponse(content="```python\ndef ok():\n    return 1\n```" if good else BAD, model=model,
                           input_tokens=7, output_tokens=3, cost_usd=0.0, latency_ms=5.0,
                           provider=model.split("/", 1)[0])
    return _call


@pytest.mark.asyncio
async def test_floor_served_call_writes_its_real_usage_row(cache_env, caller):
    calls: list[str] = []
    await _real_router(_llm(), _provider(calls), chain=(CODER,))
    usage = _q(cache_env, "SELECT provider, model, success, input_tokens, output_tokens, reason, session_id FROM usage")
    rd = _q(cache_env, "SELECT final_provider, reason_code FROM routing_decisions")
    assert rd == [("ollama", "router_unhinted")], rd  # the floor path did serve the rejected answer
    assert usage == [("ollama", CODER, 1, 7, 3, "router_chain", SID)], usage  # one real row, no safety net


@pytest.mark.asyncio
async def test_local_timeout_in_call_skips_further_local_models_when_answer_in_hand(cache_env, caller):
    calls: list[str] = []
    await _real_router(_llm(), _provider(calls, slow={SLOW_A, SLOW_B}), chain=(CODER, SLOW_A, SLOW_B))
    assert calls == [CODER, SLOW_A], calls  # SLOW_B would have cost another 120 s


@pytest.mark.asyncio
async def test_model_in_timeout_cooldown_is_skipped_when_answer_in_hand(cache_env, caller):
    import time
    router._local_timeout_at[SLOW_A] = time.monotonic()
    calls: list[str] = []
    await _real_router(_llm(), _provider(calls, slow={SLOW_A}), chain=(CODER, SLOW_A))
    assert calls == [CODER], calls


@pytest.mark.asyncio
async def test_remote_model_still_runs_after_a_local_timeout(cache_env, caller):
    calls: list[str] = []
    out = await _real_router(_llm(), _provider(calls, slow={SLOW_A}), chain=(CODER, SLOW_A, SLOW_B, REMOTE))
    assert calls == [CODER, SLOW_A, REMOTE], calls
    assert "def ok" in out


@pytest.mark.asyncio
async def test_without_an_answer_in_hand_local_models_are_still_tried(cache_env, caller):
    calls: list[str] = []
    await _real_router(_llm(), _provider(calls, slow={SLOW_A, SLOW_B}), chain=(SLOW_A, SLOW_B, CODER))
    assert calls == [SLOW_A, SLOW_B, CODER], calls
