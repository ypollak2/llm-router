"""LLM_ROUTER_CAP_GUARD_RECONCILE: admission refuses what the backend window guard would refuse.

The proxy's prompt cap (``local_mode.over_prompt_cap``) and the backend's ``local_context_guard`` use
different token estimates. A request between the two used to be admitted local, raise ContextOverflow in
the backend and fall back to Claude (pinning the conversation there). Flag on, admission asks the same
guard function; flag off, nothing changes. The mocked backend never runs the guard, so a refusal shows
as the request reaching the upstream.
"""

from __future__ import annotations

import copy

import pytest

from llm_router import local_context_guard as lcg
from llm_router.proxy import local_mode
from llm_router.proxy.translate import to_ollama
from tests.test_proxy_local_mode import (  # noqa: F401  (env is the fixture of that module)
    MODEL, _backend, _first_call, _overrides_readable, _post, env)

NUM_CTX = 32768
FLAG = "LLM_ROUTER_CAP_GUARD_RECONCILE"


def _sized(chars: int) -> dict:
    body = _first_call()
    msg = body["messages"][0]
    filler = "lorem ipsum dolor sit amet " * (chars // 27)
    if isinstance(msg["content"], str):
        msg["content"] += filler
    else:
        msg["content"] = list(msg["content"]) + [{"type": "text", "text": filler}]
    return body


def _estimates(body: dict) -> tuple[int, int]:
    payload = to_ollama(copy.deepcopy(body), MODEL.split("/", 1)[1], num_ctx=NUM_CTX, max_predict=1)
    return local_mode.estimate_prompt_tokens(payload), lcg.estimate_payload_tokens(payload)


@pytest.fixture(autouse=True)
def _wide_cap(monkeypatch):
    # The default 25k cap sits below the 32k guard; the band exists when the cap is raised past it.
    monkeypatch.setattr(local_mode, "PROMPT_CAP_TOKENS", 40_000)
    monkeypatch.delenv("LLM_ROUTER_WINDOW_STEP", raising=False)
    monkeypatch.delenv(FLAG, raising=False)


def _in_band() -> dict:
    body = _sized(105_000)
    cap_est, guard_est = _estimates(body)
    assert cap_est <= 40_000 and guard_est > NUM_CTX - lcg.DEFAULT_RESERVED_OUTPUT_TOKENS, (cap_est, guard_est)
    return body


async def test_band_payload_is_admitted_with_flag_off(env):  # noqa: F811
    backend = _backend()
    await _post(env.app(backend, num_ctx=NUM_CTX), _in_band())
    assert len(backend.calls) == 1 and env.up.requests == []


async def test_band_payload_is_refused_at_admission_with_flag_on(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv(FLAG, "1")
    backend = _backend()
    await _post(env.app(backend, num_ctx=NUM_CTX), _in_band())
    assert backend.calls == [] and len(env.up.requests) == 1
    row = env.rows()[-1]
    assert row["reason"] == local_mode.REASON_PROMPT_GUARD != local_mode.REASON_PROMPT_CAP
    assert row["local_mode"]["local"] is False and row["local_mode"]["reason"] == local_mode.REASON_PROMPT_GUARD


async def test_guard_refusal_sets_a_claude_pin_like_the_cap_with_flag_on(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv(FLAG, "1")
    await _post(env.app(_backend(), num_ctx=NUM_CTX), _in_band())
    lm = env.rows()[-1]["local_mode"]
    # structural reason: the session is pinned to Claude, as for prompt_over_cap (P7 unpin lifts only prompt_over_cap)
    assert lm["reason"] == local_mode.REASON_PROMPT_GUARD and lm["pin_change"].endswith("->claude")


@pytest.mark.parametrize("flag", [None, "1"])
async def test_small_payload_is_admitted_either_way(env, monkeypatch, flag):  # noqa: F811
    if flag:
        monkeypatch.setenv(FLAG, flag)
    body = _sized(20_000)
    cap_est, guard_est = _estimates(body)
    assert cap_est < 40_000 and guard_est < NUM_CTX - lcg.DEFAULT_RESERVED_OUTPUT_TOKENS
    backend = _backend()
    await _post(env.app(backend, num_ctx=NUM_CTX), body)
    assert len(backend.calls) == 1 and env.up.requests == []


@pytest.mark.parametrize("flag", [None, "1"])
async def test_payload_over_the_cap_is_refused_either_way_as_the_cap(env, monkeypatch, flag):  # noqa: F811
    if flag:
        monkeypatch.setenv(FLAG, flag)
    body = _sized(200_000)
    assert _estimates(body)[0] > 40_000
    backend = _backend()
    await _post(env.app(backend, num_ctx=NUM_CTX), body)
    assert backend.calls == [] and len(env.up.requests) == 1
    assert env.rows()[-1]["reason"] == local_mode.REASON_PROMPT_CAP


async def test_flag_off_ledger_row_gains_no_keys(env, monkeypatch):  # noqa: F811
    backend = _backend()
    await _post(env.app(backend, num_ctx=NUM_CTX), _sized(20_000))
    off = env.rows()[-1]
    assert local_mode.REASON_PROMPT_GUARD not in str(off)
    monkeypatch.setenv(FLAG, "1")
    await _post(env.app(_backend(), num_ctx=NUM_CTX), _sized(20_000))
    on = env.rows()[-1]
    # a request the guard admits writes the same row shape with the flag on or off
    assert set(off) == set(on) and set(off["local_mode"]) == set(on["local_mode"])


def test_overflow_estimate_and_check_overflow_agree():
    payload = {"model": "m", "messages": [{"role": "user", "content": "x" * 120_000}]}
    assert lcg.overflow_estimate(payload, num_ctx=NUM_CTX) is not None
    with pytest.raises(lcg.ContextOverflow):
        lcg.check_overflow(payload, num_ctx=NUM_CTX)
    small = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert lcg.overflow_estimate(small, num_ctx=NUM_CTX) is None
    lcg.check_overflow(small, num_ctx=NUM_CTX)
