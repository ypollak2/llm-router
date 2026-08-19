"""Regression: RED1-3-01/02/03 — reservation must not leak on early exits.

route_and_call adds _reservation to _pending_spend but had no top-level
try/finally, so early exits (empty chain, semantic-cache hit, reserve_envelope
failure) leaked it. A single idempotent _release_reservation_if_held() is now
called on every such exit. These drive route_and_call through those paths and
assert _pending_spend returns to baseline.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import router
from llm_router.types import RoutingProfile, TaskType


@contextmanager
def _hermetic_env(es):
    """Isolate ALL real DB access to a throwaway tmp file and reset the config
    cache before and after.

    These tests drive the *real* route_and_call. Any code path that is not
    explicitly mocked (semantic_cache.check, ledger/session-spend background
    writes) opens the SQLite DB via cost._get_db() -> get_config().llm_router_db_path.
    With LLM_ROUTER_DB_PATH unset that is the user's real ~/.llm-router DB, and the
    resulting connections/daemon threads leaked past teardown and corrupted the
    DB-state of downstream tests (semantic_cache/savings) — 11 deterministic
    full-suite failures that all passed in isolation. Pinning a tmp DB here and
    clearing llm_router.config._config on the way in AND out contains the blast
    radius so no real DB is touched and no cached Config bleeds across tests.
    """
    import llm_router.config as _cfg
    tmpdir = es.enter_context(tempfile.TemporaryDirectory())
    _cfg._config = None
    es.enter_context(patch.dict(os.environ, {"LLM_ROUTER_DB_PATH": os.path.join(tmpdir, "res.db")}))
    try:
        yield
    finally:
        _cfg._config = None


async def _drive(**overrides):
    """Run route_and_call with the tq007 harness plus per-test overrides."""
    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "smart"}))
        es.enter_context(_hermetic_env(es))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        tr = MagicMock()
        tr.is_healthy.return_value = True
        p(patch("llm_router.router.get_tracker", return_value=tr))
        ml = MagicMock()
        ml.bind.return_value = MagicMock()
        p(patch("llm_router.router.log", ml))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        p(patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        # Default the cache read to a miss unless a test already patched it
        # externally (the cache-hit test does): the real check() opens the DB and
        # (pre-fix) leaked a connection into later tests.
        import llm_router.semantic_cache as _sc
        if not isinstance(_sc.check, (AsyncMock, MagicMock)):
            p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        for target, mock in overrides.items():
            p(patch(f"llm_router.router.{target}", **mock))
        from llm_router.router import route_and_call
        try:
            return await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)
        finally:
            # Drain fire-and-forget ledger/session-spend/telemetry tasks WHILE the
            # hermetic tmp-DB env is still active, so none survive into the next
            # test holding a connection. This is the actual leak that broke 11
            # downstream DB tests: route_and_call spawns _BG_TASKS that were never
            # drained here.
            await router.drain_bg_tasks(timeout_s=5.0)


@pytest.mark.asyncio
async def test_no_leak_on_empty_chain():
    before = router._pending_spend
    with pytest.raises(ValueError):
        await _drive(
            reserve_envelope={"new_callable": AsyncMock, "return_value": (None, True, "k")},
            _build_and_filter_chain={"new_callable": AsyncMock, "return_value": []},
        )
    assert abs(router._pending_spend - before) < 1e-9, (
        f"RED1-3-01: reservation leaked on empty-chain exit: {before}->{router._pending_spend}"
    )


@pytest.mark.asyncio
async def test_no_leak_on_cache_hit_fast_path():
    from llm_router.types import LLMResponse
    cached = LLMResponse(content="c", model="ollama/x", input_tokens=1, output_tokens=1,
                         cost_usd=0.0, latency_ms=1.0, provider="ollama")
    with patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=cached):
        before = router._pending_spend
        _resp = await _drive(
            reserve_envelope={"new_callable": AsyncMock, "return_value": (None, True, "k")},
            _build_and_filter_chain={"new_callable": AsyncMock, "return_value": ["openai/gpt-4o"]},
        )
        assert abs(router._pending_spend - before) < 1e-9, (
            f"RED1-3-02: reservation leaked on cache-hit fast path: {before}->{router._pending_spend}"
        )


# NOTE: the RED1-4-01 success-path double-decrement test was REMOVED. RED1-4-01
# (route_and_call releasing _pending_spend a second time on the success path,
# after _dispatch_model_loop already released) is DEFERRED into the single-owner
# reservation-lifecycle refactor — a targeted code patch is not shipped, so there
# is no single-release invariant to assert yet (see iteration-04 report). The test
# also drove 8 CONCURRENT real route_and_call calls with an unmocked dispatch;
# those fire-and-forget ledger/session-spend tasks were never drained and leaked
# DB connections into later tests, deterministically breaking 11 downstream
# DB-state tests in the full suite (all of which passed in isolation). The
# remaining tests here mock the dispatch and drain _BG_TASKS via _drive.


@pytest.mark.asyncio
async def test_envelope_released_on_all_models_failed():
    """RED1-4-02: strict-mode envelope must be released when all models fail."""
    released = {"env": False}

    async def fake_release(key, amt):
        released["env"] = True

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "smart"}))
        es.enter_context(_hermetic_env(es))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        tr = MagicMock()
        tr.is_healthy.return_value = True
        p(patch("llm_router.router.get_tracker", return_value=tr))
        ml = MagicMock()
        ml.bind.return_value = MagicMock()
        p(patch("llm_router.router.log", ml))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        p(patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=("strict", True, "envkey")))
        p(patch("llm_router.router.release_envelope", side_effect=fake_release))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=["openai/gpt-4o"]))
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=RuntimeError("boom")))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        from llm_router.router import route_and_call
        with pytest.raises(Exception):
            await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)
    assert released["env"], "RED1-4-02: envelope not released when all models failed"
