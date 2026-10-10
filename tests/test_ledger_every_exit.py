"""LEDGER-EVERY-EXIT-1: every ``llm()`` / ``llm_act()`` exit leaves exactly one row in ``usage`` and one in
``routing_decisions``, with the caller's session id and provenance runtime.

History: three M0-3 runs each found a new unledgered path (cache hit #381, provider error / timeout / cancel
#398, breaker refusal #409). The fix is one structural guard (``consolidated._ledger_guard``) around the whole
tool body, not a patch per path. This file ENUMERATES the exit paths by forcing each branch through the real
tool function, the real router and the real writers (only the provider is stubbed), and proves the guard is
load-bearing (mutation check) and that no tool body can sit outside it (AST check).
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sqlite3
import textwrap
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import call_identity
from llm_router import quality_breaker as qb
from llm_router.provider_classes import is_non_decision_reason
from llm_router.tools import consolidated
from llm_router.types import BudgetExceededError, LLMResponse
from tests.test_p05_semantic_cache_key import _fake_embedding, _route
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env
SID = "33333333-aaaa-bbbb-cccc-444444444444"


class _Ctx:
    async def info(self, *a, **k):
        pass

    async def report_progress(self, *a, **k):
        pass


@pytest.fixture
def caller(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    tok = call_identity.bind("toolu_every_exit")
    yield
    call_identity.reset(tok)


def _q(db, sql):
    c = sqlite3.connect(str(db))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _ok(model, messages, **kw):
    return LLMResponse(content="print('ok')", model=model, input_tokens=7, output_tokens=3,
                       cost_usd=0.0, latency_ms=5.0, provider=model.split("/", 1)[0])


def _boom(model, messages, **kw):
    raise RuntimeError("provider down")


async def _real_router(entry, provider, *, chain=("ollama/qwen3-coder:30b",), healthy=True):
    """Run ``entry()`` with the real router + writers; only the provider and the chain are stubbed."""
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
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=provider))
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=False))
        p(patch("llm_router.context_injection.inject", side_effect=lambda prompt, **_k: prompt))
        p(patch("llm_router.semantic_cache._get_embedding", side_effect=_fake_embedding))
        try:
            return await entry()
        finally:
            await router.drain_bg_tasks(3.0)


def _llm(task="code"):
    return lambda: consolidated.llm("write a rust fib fn", _Ctx(), task=task)


def _open_breaker(db, lever):
    (db.parent / qb.STATE_FILE).write_text(json.dumps({"classes": {
        qb.class_key(lever, "code" if lever == "mcp_llm" else "agentic"): {
            "state": qb.OPEN, "opened_at": time.time(), "n": 20, "failure_rate": 1.0}}}))


def _assert_one_each(db, reason_usage, reason_rd):
    u = _q(db, "SELECT session_id, success, input_tokens, output_tokens, cost_usd, reason FROM usage")
    r = _q(db, "SELECT session_id, provenance, success, cost_usd, reason_code FROM routing_decisions")
    from llm_router import cost
    assert len(u) == 1 and len(r) == 1, (u, r)
    assert u[0][0] == SID and r[0][0] == SID
    assert r[0][1] == cost._write_provenance()
    assert u[0][5] == reason_usage and r[0][4] == reason_rd
    return u[0], r[0]


# ---- the enumeration: one row per exit path ----------------------------------------------------------

@pytest.mark.asyncio
async def test_served(cache_env, caller):
    await _real_router(_llm(), _ok)
    u, r = _assert_one_each(cache_env, "router_chain", r_reason(cache_env))
    assert u[1] == 1


def r_reason(db):
    return _q(db, "SELECT reason_code FROM routing_decisions")[0][0]


@pytest.mark.asyncio
async def test_cache_hit(cache_env, caller, monkeypatch):
    # the real cache path via the p05 harness, entered through the guarded tool body
    from llm_router import cost
    import tests.test_p05_semantic_cache_key as p05
    real = cost.log_usage
    orig_patch = p05.patch

    class _Patch:
        dict = staticmethod(orig_patch.dict)
        object = staticmethod(orig_patch.object)

        def __call__(self, target, *a, **kw):
            if target == "llm_router.router.cost.log_usage":
                return orig_patch.object(cost, "log_usage", real)
            return orig_patch(target, *a, **kw)

    monkeypatch.setattr(p05, "patch", _Patch())
    calls: list = []

    async def _llm_code(prompt, ctx, **k):
        r = await _route("explain the retry policy", caller_context="c", calls=calls)
        return "cache" if r.cache_hit else "miss"

    monkeypatch.setattr(consolidated, "llm_code", _llm_code)
    assert await consolidated.llm("x", _Ctx(), task="code") == "miss"
    assert await consolidated.llm("x", _Ctx(), task="code") == "cache"
    assert len(calls) == 1
    assert _q(cache_env, "SELECT reason, session_id FROM usage ORDER BY id") == [
        ("router_chain", SID), ("cache_hit", SID)]
    assert [x[0] for x in _q(cache_env, "SELECT session_id FROM routing_decisions")] == [SID, SID]


@pytest.mark.asyncio
async def test_provider_error_all_models_failed(cache_env, caller):
    with pytest.raises(RuntimeError, match="All models failed"):
        await _real_router(_llm(), _boom)
    _assert_one_each(cache_env, "error_all_models_failed", "error_all_models_failed")


@pytest.mark.asyncio
async def test_empty_chain_before_dispatch(cache_env, caller):
    with pytest.raises(Exception):
        await _real_router(_llm(), _ok, chain=())
    u, r = _assert_one_each(cache_env, r_usage(cache_env), r_reason(cache_env))
    assert u[1] == 0 and r[2] == 0 and is_non_decision_reason(r[4])


def r_usage(db):
    return _q(db, "SELECT reason FROM usage")[0][0]


@pytest.mark.asyncio
async def test_pre_dispatch_budget_refusal(cache_env, caller, monkeypatch):
    async def _refuse(*a, **k):
        raise BudgetExceededError("daily cap")

    monkeypatch.setattr(consolidated, "llm_code", _refuse)
    with pytest.raises(BudgetExceededError):
        await consolidated.llm("x", _Ctx(), task="code")
    _assert_one_each(cache_env, "error_budget_exceeded", "error_budget_exceeded")


@pytest.mark.asyncio
async def test_timeout(cache_env, caller, monkeypatch):
    async def _slow(*a, **k):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(consolidated, "llm_code", _slow)
    with pytest.raises(asyncio.TimeoutError):
        await consolidated.llm("x", _Ctx(), task="code")
    _assert_one_each(cache_env, "error_timeout", "error_timeout")


@pytest.mark.asyncio
async def test_cancel_before_any_writer(cache_env, caller, monkeypatch):
    started = asyncio.Event()

    async def _hang(*a, **k):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(consolidated, "llm_code", _hang)
    task = asyncio.ensure_future(consolidated.llm("x", _Ctx(), task="code"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_one_each(cache_env, "error_cancelled", "error_cancelled")


@pytest.mark.asyncio
async def test_unexpected_exception_after_the_router_returned_nothing(cache_env, caller, monkeypatch):
    async def _bug(*a, **k):
        raise KeyError("formatter bug")

    monkeypatch.setattr(consolidated, "llm_query", _bug)
    with pytest.raises(KeyError):
        await consolidated.llm("x", _Ctx(), task="query")
    _assert_one_each(cache_env, "error_exception", "error_exception")


@pytest.mark.asyncio
async def test_normal_return_that_wrote_no_row(cache_env, caller, monkeypatch):
    async def _quiet(*a, **k):
        return "answer from a path that never touched the ledger"

    monkeypatch.setattr(consolidated, "llm_research", _quiet)
    assert (await consolidated.llm("x", _Ctx(), task="research")).startswith("answer")
    _assert_one_each(cache_env, "error_unledgered_exit", "error_unledgered_exit")


@pytest.mark.asyncio
async def test_breaker_refusal(cache_env, caller, monkeypatch):
    _open_breaker(cache_env, "mcp_llm")

    async def _no(*a, **k):
        raise AssertionError("must not dispatch")

    monkeypatch.setattr(consolidated, "llm_code", _no)
    assert (await consolidated.llm("x", _Ctx(), task="code")).startswith("[llm_router] quality_breaker:")
    _assert_one_each(cache_env, "breaker_open", "breaker_open")


@pytest.mark.asyncio
async def test_a_path_that_wrote_only_usage_gets_the_missing_routing_row(cache_env, caller, monkeypatch):
    from llm_router import cost
    from llm_router.types import RoutingProfile, TaskType

    async def _half(*a, **k):
        await cost.log_usage(_ok("ollama/x", []), TaskType.CODE, RoutingProfile.BUDGET,
                             correlation_id="half0001", reason="router_chain")
        return "ok"

    monkeypatch.setattr(consolidated, "llm_code", _half)
    await consolidated.llm("x", _Ctx(), task="code")
    assert [x[0] for x in _q(cache_env, "SELECT reason FROM usage")] == ["router_chain"]  # not doubled
    assert len(_q(cache_env, "SELECT 1 FROM routing_decisions")) == 1


@pytest.mark.asyncio
async def test_llm_act_breaker_refusal(cache_env, caller, monkeypatch):
    _open_breaker(cache_env, "mcp_llm_act")

    async def _no(*a, **k):
        raise AssertionError("must not dispatch")

    monkeypatch.setattr(consolidated, "llm_delegate", _no)
    assert (await consolidated.llm_act("do it")).startswith("[llm_router] quality_breaker:")
    _assert_one_each(cache_env, "breaker_open", "breaker_open")


@pytest.mark.asyncio
async def test_llm_act_error_and_silent_return(cache_env, caller, monkeypatch):
    async def _err(*a, **k):
        raise RuntimeError("delegate failed")

    monkeypatch.setattr(consolidated, "llm_delegate", _err)
    with pytest.raises(RuntimeError):
        await consolidated.llm_act("do it")
    _assert_one_each(cache_env, "error_exception", "error_exception")


@pytest.mark.asyncio
async def test_a_ledger_failure_never_changes_the_reply_or_the_exception(cache_env, caller, monkeypatch):
    from llm_router import cost

    async def _fail(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cost, "log_route_error", _fail)

    async def _bug(*a, **k):
        raise KeyError("original")

    monkeypatch.setattr(consolidated, "llm_query", _bug)
    with pytest.raises(KeyError, match="original"):
        await consolidated.llm("x", _Ctx())

    async def _fine(*a, **k):
        return "reply"

    monkeypatch.setattr(consolidated, "llm_query", _fine)
    assert await consolidated.llm("x", _Ctx()) == "reply"


# ---- mutation check + structure ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutation_without_the_guard_the_unwritten_paths_leave_no_row(cache_env, caller, monkeypatch):
    """Disable the exit writer: the provider-less paths above must then be silent. Proves the rows come
    from the guard, not from some other writer, and that this suite can fail."""
    from llm_router import cost

    await (await cost._get_db()).close()  # create the tables so the zero below is a count, not an error

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(consolidated, "_ledger_exit", _noop)

    async def _bug(*a, **k):
        raise KeyError("x")

    monkeypatch.setattr(consolidated, "llm_query", _bug)
    with pytest.raises(KeyError):
        await consolidated.llm("x", _Ctx())
    assert _q(cache_env, "SELECT COUNT(*) FROM usage")[0][0] == 0
    assert _q(cache_env, "SELECT COUNT(*) FROM routing_decisions")[0][0] == 0


@pytest.mark.parametrize("fn", [consolidated.llm, consolidated.llm_act])
def test_the_whole_tool_body_sits_inside_the_guard(fn):
    """A branch added later cannot escape the guard: the body is the docstring, plain assignments, and ONE
    ``async with _ledger_guard(...)`` that holds every return."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    withs = [n for n in body if isinstance(n, ast.AsyncWith)]
    assert len(withs) == 1
    call = withs[0].items[0].context_expr
    assert isinstance(call, ast.Call) and call.func.id == "_ledger_guard"
    outside = [n for n in body if n is not withs[0]]
    assert not any(isinstance(sub, (ast.Return, ast.Raise, ast.Await))
                   for n in outside for sub in ast.walk(n))


@pytest.mark.asyncio
async def test_concurrent_calls_are_not_cross_credited(cache_env, monkeypatch):
    """4 concurrent llm() calls, distinct tool_use ids: 2 write their own usage row, 2 write none. Each call
    ends with exactly one usage and one routing_decisions row (the guard is per call, never shared)."""
    from llm_router import cost
    from llm_router.types import RoutingProfile, TaskType

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)

    async def _fake(prompt, ctx, **k):
        tid = call_identity.tool_use_id()
        await asyncio.sleep(0.01 * int(tid[-1]))  # interleave
        if int(tid[-1]) % 2 == 0:
            await cost.log_usage(_ok("ollama/x", []), TaskType.CODE, RoutingProfile.BUDGET,
                                 correlation_id=f"cc{tid[-1]}00000", reason="router_chain")
        return "r" + tid

    monkeypatch.setattr(consolidated, "llm_code", _fake)

    async def one(i):
        tok = call_identity.bind(f"toolu_conc_{i}")
        try:
            return await consolidated.llm("x", _Ctx(), task="code")
        finally:
            call_identity.reset(tok)

    outs = await asyncio.gather(*(one(i) for i in range(4)))
    assert outs == [f"rtoolu_conc_{i}" for i in range(4)]
    assert sorted(_q(cache_env, "SELECT reason FROM usage")) == [
        ("error_unledgered_exit",), ("error_unledgered_exit",), ("router_chain",), ("router_chain",)]
    assert len(_q(cache_env, "SELECT 1 FROM routing_decisions")) == 4
    assert sorted(x[0] for x in _q(cache_env, "SELECT tool_use_id FROM routing_decisions")) == [
        f"toolu_conc_{i}" for i in range(4)]
    assert {x[0] for x in _q(cache_env, "SELECT session_id FROM usage")} == {SID}
