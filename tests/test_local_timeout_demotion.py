"""LOCAL-TIMEOUT-1: a local model that just timed out must not lead the next route.

Live 2026-10-10 (routing_quality.jsonl, 08:34:22Z-09:30:14Z): ollama/qwen3-coder:30b
led the code chain and hit litellm's 120 s timeout on 21 of the 21 routes that tried
it, while ollama/qwen3.8:latest answered right after it. Ollama's scheduler was stuck
on a model it could not load, so only already-loaded models answered. The router kept
no memory of the previous call, so every call paid the full 120 s again.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from llm_router import router
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType

CODER = "ollama/qwen3-coder:30b"
GENERAL = "ollama/qwen3.8:latest"
REMOTE = "openai/gpt-4o-mini"  # non-local, and goes through _call_text like Ollama
CHAIN = [CODER, GENERAL, REMOTE]
_PROMPTS = iter(f"Write a Python function named add_n18_{i} that returns a + b" for i in range(1000))


@pytest.fixture(autouse=True)
def _fresh_cooldowns(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S", raising=False)
    router._local_timeout_at.clear()
    yield
    router._local_timeout_at.clear()


def _pin_chain(models):
    return patch("llm_router.router._build_and_filter_chain",
                 new=AsyncMock(return_value=list(models)))


def _ok(model: str) -> LLMResponse:
    return LLMResponse(content="def add_n18(a, b):\n    return a + b\n", model=model,
                       input_tokens=20, output_tokens=12, cost_usd=0.0,
                       latency_ms=50.0, provider=model.split("/", 1)[0])


def _fake_call(calls: list[str], failing: dict[str, Exception]):
    async def _call(model, *a, **k):
        calls.append(model)
        if model in failing:
            raise failing[model]
        return _ok(model)
    return _call


def _timeout(model: str) -> Exception:
    return litellm.Timeout(message="Connection timed out. Timeout passed=120.0",
                           model=model, llm_provider="ollama")


async def _route(calls, failing, chain=CHAIN, **kw):
    # A fresh prompt per route: a repeated one is answered by the semantic cache.
    with _pin_chain(chain), patch("llm_router.router._call_text",
                                  new=AsyncMock(side_effect=_fake_call(calls, failing))):
        return await route_and_call(TaskType.CODE, next(_PROMPTS), complexity_hint="simple", **kw)


@pytest.mark.asyncio
async def test_timed_out_local_model_does_not_lead_the_next_route(temp_db, mock_env):
    calls: list[str] = []
    failing = {CODER: _timeout(CODER)}

    first = await _route(calls, failing)
    assert calls == [CODER, GENERAL]
    assert first.model == GENERAL

    calls.clear()
    second = await _route(calls, failing)
    # Before the fix the second route also started with the coder (another 120 s).
    assert calls == [GENERAL]
    assert second.model == GENERAL


@pytest.mark.asyncio
async def test_demoted_model_is_still_tried_when_everything_before_it_fails(temp_db, mock_env):
    calls: list[str] = []
    await _route(calls, {CODER: _timeout(CODER)})

    calls.clear()
    resp = await _route(calls, {GENERAL: RuntimeError("down"), REMOTE: RuntimeError("down")})
    assert calls == [GENERAL, REMOTE, CODER]
    assert resp.model == CODER


@pytest.mark.asyncio
async def test_demotion_does_not_rerun_the_same_chain_as_an_emergency_fallback(temp_db, mock_env):
    # The emergency BUDGET pass is skipped when it would be the same chain. Demotion
    # reorders the primary chain, and that must not make the same chain look new.
    calls: list[str] = []
    await _route(calls, {CODER: _timeout(CODER)})

    calls.clear()
    down = {m: RuntimeError("down") for m in CHAIN}
    with pytest.raises(RuntimeError):
        await _route(calls, down, profile=RoutingProfile.BALANCED)
    assert calls == [GENERAL, REMOTE, CODER]


@pytest.mark.asyncio
async def test_the_emergency_chain_is_demoted_too(temp_db, mock_env):
    calls: list[str] = []
    await _route(calls, {CODER: _timeout(CODER)})

    calls.clear()
    primary, emergency = ["openai/gpt-4o"], [CODER, GENERAL]
    with patch("llm_router.router._build_and_filter_chain",
               new=AsyncMock(side_effect=[list(primary), list(emergency)])), \
         patch("llm_router.router._call_text", new=AsyncMock(side_effect=_fake_call(
             calls, {"openai/gpt-4o": RuntimeError("down")}))):
        resp = await route_and_call(TaskType.CODE, next(_PROMPTS), complexity_hint="simple",
                                    profile=RoutingProfile.BALANCED)
    assert calls == ["openai/gpt-4o", GENERAL]
    assert resp.model == GENERAL


@pytest.mark.asyncio
async def test_model_leads_again_once_the_cooldown_has_passed(temp_db, mock_env):
    calls: list[str] = []
    await _route(calls, {CODER: _timeout(CODER)})
    router._local_timeout_at[CODER] = time.monotonic() - 601  # default cooldown is 600 s

    calls.clear()
    resp = await _route(calls, {})
    assert calls == [CODER]
    assert resp.model == CODER


@pytest.mark.asyncio
async def test_cooldown_zero_turns_demotion_off(temp_db, mock_env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S", "0")
    calls: list[str] = []
    await _route(calls, {CODER: _timeout(CODER)})

    calls.clear()
    await _route(calls, {CODER: _timeout(CODER)})
    assert calls == [CODER, GENERAL]


@pytest.mark.asyncio
async def test_a_non_timeout_error_does_not_demote(temp_db, mock_env):
    calls: list[str] = []
    await _route(calls, {CODER: RuntimeError("model failed to load")})

    calls.clear()
    await _route(calls, {})
    assert calls == [CODER]


def test_only_local_timeouts_are_recorded():
    router._note_local_timeout(REMOTE, _timeout(REMOTE))
    router._note_local_timeout(CODER, RuntimeError("boom"))
    assert router._local_timeout_at == {}
    router._note_local_timeout(CODER, TimeoutError())
    assert set(router._local_timeout_at) == {CODER}


def test_order_is_kept_when_every_model_is_cooling():
    now = time.monotonic()
    router._local_timeout_at.update({CODER: now, GENERAL: now})
    assert router._demote_timed_out_local([CODER, GENERAL]) == [CODER, GENERAL]
    assert router._demote_timed_out_local([CODER, GENERAL, REMOTE]) == [REMOTE, CODER, GENERAL]


def test_an_explicit_pin_keeps_its_place():
    router._local_timeout_at[CODER] = time.monotonic()
    assert router._demote_timed_out_local([CODER, GENERAL], exempt=CODER) == [CODER, GENERAL]
    assert router._demote_timed_out_local([CODER, GENERAL], exempt=None) == [GENERAL, CODER]


@pytest.mark.parametrize("raw, expected", [("", 600.0), ("30", 30.0), ("-5", 0.0), ("abc", 600.0)])
def test_cooldown_setting_is_parsed_safely(monkeypatch, raw, expected):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TIMEOUT_COOLDOWN_S", raw)
    assert router._local_timeout_cooldown_s() == expected


# ── Follow-up (#400 review): the emergency (backup) chain records timeouts too ──


async def _route_with_emergency(calls, failing, emergency):
    with patch("llm_router.router._build_and_filter_chain",
               new=AsyncMock(side_effect=[["openai/gpt-4o"], list(emergency)])), \
         patch("llm_router.router._call_text", new=AsyncMock(side_effect=_fake_call(
             calls, {"openai/gpt-4o": RuntimeError("down"), **failing}))):
        return await route_and_call(TaskType.CODE, next(_PROMPTS), complexity_hint="simple",
                                    profile=RoutingProfile.BALANCED)


@pytest.mark.asyncio
async def test_a_timeout_in_the_emergency_chain_starts_the_cooldown(temp_db, mock_env):
    calls: list[str] = []
    resp = await _route_with_emergency(calls, {CODER: _timeout(CODER)}, [CODER, GENERAL])
    assert calls == ["openai/gpt-4o", CODER, GENERAL]
    assert resp.model == GENERAL
    # Before the fix the emergency loop's except never noted the timeout.
    assert set(router._local_timeout_at) == {CODER}

    calls.clear()
    await _route_with_emergency(calls, {CODER: _timeout(CODER)}, [CODER, GENERAL])
    assert calls == ["openai/gpt-4o", GENERAL]


@pytest.mark.asyncio
async def test_a_timeout_in_the_emergency_chain_refreshes_the_cooldown(temp_db, mock_env):
    stale = time.monotonic() - 500
    router._local_timeout_at[CODER] = stale
    calls: list[str] = []
    # CODER is demoted to the back; everything before it fails, so it runs and times out again.
    with pytest.raises(RuntimeError):
        await _route_with_emergency(
            calls, {GENERAL: RuntimeError("down"), REMOTE: RuntimeError("down"), CODER: _timeout(CODER)},
            [CODER, GENERAL, REMOTE])
    assert calls == ["openai/gpt-4o", GENERAL, REMOTE, CODER]
    assert router._local_timeout_at[CODER] > stale


@pytest.mark.asyncio
async def test_emergency_chain_non_timeout_errors_do_not_demote(temp_db, mock_env):
    calls: list[str] = []
    bad_request = litellm.BadRequestError(message="bad request", model=CODER, llm_provider="ollama")
    resp = await _route_with_emergency(calls, {CODER: bad_request}, [CODER, GENERAL])
    assert resp.model == GENERAL
    assert router._local_timeout_at == {}


@pytest.mark.asyncio
async def test_emergency_chain_cancellation_does_not_demote(temp_db, mock_env):
    import asyncio

    calls: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await _route_with_emergency(calls, {CODER: asyncio.CancelledError()}, [CODER, GENERAL])
    assert router._local_timeout_at == {}
