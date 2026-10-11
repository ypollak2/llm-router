"""LEDGER-ERR-1: a call that reaches dispatch and fails leaves exactly one session-attributed row.

M0-3 rerun (2026-10-10): 3 of 20 ``llm(task="code")`` calls ended in an MCP tool error and
wrote no ``usage`` / ``routing_decisions`` row, only a cache-lookup row. The writers ran on
success only. These tests run the real ``route_and_call`` and the real ``cost.log_usage``
against a real SQLite file; only the provider is stubbed.

Exceptions that escape the dispatch loop: a provider failure on every candidate (the live
A5/B10 shape), no candidate healthy (B7), a wall-clock timeout, an external cancel.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import call_identity
from llm_router.types import LLMResponse, RoutingProfile, TaskType
from tests.test_p05_semantic_cache_key import _fake_embedding
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env  # re-exported fixture

SID = "11111111-aaaa-bbbb-cccc-222222222222"


@pytest.fixture
def caller(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    tok = call_identity.bind("toolu_test_err1")
    yield
    call_identity.reset(tok)


def _q(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


_ROW = ("SELECT session_id, task_type, provider, model, reason, success, input_tokens, "
        "output_tokens, cost_usd, latency_ms FROM usage ORDER BY id")


async def _call(provider_effect, *, healthy=True, chain=("ollama/qwen3-coder:30b",), started=None,
                **route_kwargs):
    import tests.test_tq007_daily_cap_downgrade as t
    from llm_router import router

    tracker = MagicMock()
    tracker.is_healthy.return_value = healthy
    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()
    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off"}))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
                return_value=list(chain)))
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=provider_effect))
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=False))
        p(patch("llm_router.context_injection.inject", side_effect=lambda prompt, **_k: prompt))
        p(patch("llm_router.semantic_cache._get_embedding", side_effect=_fake_embedding))
        coro = router.route_and_call(TaskType.CODE, "write a rust fib fn", profile=RoutingProfile.BUDGET,
                                     caller_context="ctx", **route_kwargs)
        if started is None:
            try:
                return await coro
            finally:
                await router.drain_bg_tasks(3.0)
        task = asyncio.ensure_future(coro)
        await started.wait()
        task.cancel()
        try:
            await task
        finally:
            await router.drain_bg_tasks(3.0)


def _ok(model, messages, **kw):
    return LLMResponse(content="print('ok')", model=model, input_tokens=7, output_tokens=3,
                       cost_usd=0.0, latency_ms=5.0, provider=model.split("/", 1)[0])


@pytest.mark.asyncio
async def test_provider_exception_writes_one_error_row(cache_env, caller):
    def boom(model, messages, **kw):
        raise RuntimeError("Connection timed out. Timeout passed=120.0")

    with pytest.raises(RuntimeError, match="All models failed"):
        await _call(boom, chain=("ollama/nimble:9b", "ollama/qwen3-coder:30b"))
    rows = _q(cache_env, _ROW)
    assert len(rows) == 1, rows
    sid, task, provider, model, reason, success, tin, tout, cost_usd, latency = rows[0]
    assert (sid, task, provider, model, reason, success, tin, tout, cost_usd) == (
        SID, "code", "ollama", "ollama/qwen3-coder:30b", "error_all_models_failed", 0, 0, 0, 0.0)
    assert latency is not None and latency >= 0


@pytest.mark.asyncio
async def test_no_healthy_candidate_writes_one_error_row_naming_the_first_candidate(cache_env, caller):
    """The live B7 shape: every candidate skipped as unhealthy, nothing attempted, 3 s."""
    with pytest.raises(RuntimeError, match="All models failed"):
        await _call(_ok, healthy=False, chain=("ollama/qwen3-coder:30b", "ollama/qwen3.8:latest"))
    rows = _q(cache_env, _ROW)
    assert [(r[0], r[3], r[4], r[5]) for r in rows] == [
        (SID, "ollama/qwen3-coder:30b", "error_all_models_failed", 0)]


@pytest.mark.asyncio
async def test_timeout_writes_one_error_row(cache_env, caller):
    async def slow(model, messages, **kw):
        await asyncio.sleep(5)

    from llm_router.types import WallClockExceeded

    with pytest.raises(WallClockExceeded):
        await _call(slow, max_wall_clock_seconds=0.05)
    rows = _q(cache_env, _ROW)
    assert len(rows) == 1, rows
    assert (rows[0][0], rows[0][3], rows[0][4], rows[0][5]) == (
        SID, "ollama/qwen3-coder:30b", "error_timeout", 0)
    assert 30 <= rows[0][9] < 2000  # the elapsed dispatch time, not 0 and not the 5 s sleep


@pytest.mark.asyncio
async def test_cancellation_writes_one_error_row(cache_env, caller):
    started = asyncio.Event()

    async def hang(model, messages, **kw):
        started.set()
        await asyncio.sleep(30)

    with pytest.raises(asyncio.CancelledError):
        await _call(hang, started=started)
    rows = _q(cache_env, _ROW)
    assert len(rows) == 1, rows
    assert (rows[0][0], rows[0][3], rows[0][4], rows[0][5]) == (
        SID, "ollama/qwen3-coder:30b", "error_cancelled", 0)


@pytest.mark.asyncio
async def test_success_writes_its_one_row_and_no_error_row(cache_env, caller):
    await _call(_ok)
    rows = _q(cache_env, _ROW)
    assert [(r[0], r[4], r[5]) for r in rows] == [(SID, "router_chain", 1)]


@pytest.mark.asyncio
async def test_a_retry_after_a_failure_is_two_calls_two_rows(cache_env, caller):
    def boom(model, messages, **kw):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await _call(boom)
    await _call(_ok)
    rows = _q(cache_env, "SELECT reason, success FROM usage ORDER BY id")
    assert rows == [("error_all_models_failed", 0), ("router_chain", 1)]


@pytest.mark.asyncio
async def test_a_row_already_written_for_the_call_is_not_doubled(cache_env, caller):
    """A cancel landing after the answer was recorded must not add an error row."""
    from llm_router import cost

    resp = LLMResponse(content="x", model="ollama/qwen3.8:latest", input_tokens=1, output_tokens=1,
                       cost_usd=0.0, latency_ms=1.0, provider="ollama")
    await cost.log_usage(resp, TaskType.CODE, RoutingProfile.BUDGET, correlation_id="abc12345",
                         reason=cost.REASON_ROUTER_CHAIN)
    wrote = await cost.log_route_error(TaskType.CODE, RoutingProfile.BUDGET,
                                       reason=cost.REASON_ERROR_CANCELLED, correlation_id="abc12345")
    assert wrote is False
    assert _q(cache_env, "SELECT COUNT(*) FROM usage") == [(1,)]
    # ...and the same failure recorded twice is still one row.
    assert await cost.log_route_error(TaskType.CODE, RoutingProfile.BUDGET,
                                      reason=cost.REASON_ERROR_TIMEOUT, correlation_id="zzz99999") is True
    assert await cost.log_route_error(TaskType.CODE, RoutingProfile.BUDGET,
                                      reason=cost.REASON_ERROR_TIMEOUT, correlation_id="zzz99999") is False
    assert _q(cache_env, "SELECT COUNT(*) FROM usage") == [(2,)]


@pytest.mark.asyncio
async def test_error_row_has_no_prompt_or_exception_text(cache_env, caller):
    def boom(model, messages, **kw):
        raise RuntimeError("secret-fragment-xyz in provider text")

    with pytest.raises(RuntimeError):
        await _call(boom)
    dump = repr(_q(cache_env, "SELECT * FROM usage"))
    assert "secret-fragment" not in dump and "rust fib" not in dump


@pytest.mark.asyncio
async def test_outside_an_mcp_call_the_error_row_session_is_null(cache_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)  # in env, but no tool_use bound

    def boom(model, messages, **kw):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await _call(boom)
    assert _q(cache_env, "SELECT session_id, reason FROM usage") == [(None, "error_all_models_failed")]


# ── owner decision 2026-10-10: the same calls also leave one routing_decisions row ──────────

_RD = ("SELECT session_id, task_type, final_provider, final_model, reason_code, provenance, success, "
       "cost_usd FROM routing_decisions ORDER BY id")


def _both(db):
    return _q(db, "SELECT COUNT(*) FROM usage")[0][0], _q(db, "SELECT COUNT(*) FROM routing_decisions")[0][0]


@pytest.mark.asyncio
async def test_provider_exception_writes_one_routing_decision_row(cache_env, caller):
    def boom(model, messages, **kw):
        raise RuntimeError("down")

    from llm_router import cost

    with pytest.raises(RuntimeError):
        await _call(boom)
    assert _both(cache_env) == (1, 1)
    assert _q(cache_env, _RD) == [(SID, "code", "ollama", "ollama/qwen3-coder:30b",
                                   "error_all_models_failed", cost._write_provenance(), 0, 0.0)]


@pytest.mark.asyncio
async def test_no_healthy_candidate_writes_one_row_in_each_table(cache_env, caller):
    with pytest.raises(RuntimeError):
        await _call(_ok, healthy=False, chain=("ollama/qwen3-coder:30b", "ollama/qwen3.8:latest"))
    assert _both(cache_env) == (1, 1)
    assert _q(cache_env, _RD)[0][:5] == (SID, "code", "ollama", "ollama/qwen3-coder:30b", "error_all_models_failed")


@pytest.mark.asyncio
async def test_timeout_writes_one_row_in_each_table(cache_env, caller):
    async def slow(model, messages, **kw):
        await asyncio.sleep(5)

    from llm_router.types import WallClockExceeded

    with pytest.raises(WallClockExceeded):
        await _call(slow, max_wall_clock_seconds=0.05)
    assert _both(cache_env) == (1, 1)
    assert _q(cache_env, _RD)[0][0] == SID and _q(cache_env, _RD)[0][4] == "error_timeout"


@pytest.mark.asyncio
async def test_cancellation_writes_one_row_in_each_table(cache_env, caller):
    started = asyncio.Event()

    async def hang(model, messages, **kw):
        started.set()
        await asyncio.sleep(30)

    with pytest.raises(asyncio.CancelledError):
        await _call(hang, started=started)
    assert _both(cache_env) == (1, 1)
    assert _q(cache_env, _RD)[0][4] == "error_cancelled"


@pytest.mark.asyncio
async def test_a_success_writes_one_row_in_each_table_and_no_error_row(cache_env, caller):
    await _call(_ok)
    assert _both(cache_env) == (1, 1)
    assert _q(cache_env, "SELECT reason_code FROM routing_decisions") == [("router_unhinted",)]


@pytest.mark.asyncio
async def test_error_dedup_spans_both_tables(cache_env, caller):
    from llm_router import cost

    await cost.log_route_error(TaskType.CODE, RoutingProfile.BUDGET, reason=cost.REASON_ERROR_TIMEOUT,
                               correlation_id="dup00001", attempted_model="ollama/x")
    assert await cost.log_route_error(TaskType.CODE, RoutingProfile.BUDGET, reason=cost.REASON_ERROR_TIMEOUT,
                                      correlation_id="dup00001", attempted_model="ollama/x") is False
    assert _both(cache_env) == (1, 1)
