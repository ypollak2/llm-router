"""LC-8d: dispatch traces and text-job failure accounting."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import capped_log, eligibility_routing as er, failopen, paths
from llm_router import repo_config, router
from llm_router import text_job_ledger as L
from llm_router.local_check import routing as lr
from llm_router.tools import text as T
from llm_router.types import (
    DeadlineExceeded, LLMResponse, RoutingProfile, TaskType, WallClockExceeded,
)

LOCAL = "ollama/fake-a:1b"
OTHER_LOCAL = "ollama/fake-b:1b"
CLOUD = "gemini/fake-cloud"
JOB = "commit_message"
DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n-a\n+b\n"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    for name in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_IGNORE_ELIGIBILITY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "fake-a:1b,fake-b:1b")
    er.reset_cache()
    yield tmp_path / "router_home"
    er.reset_cache()


def response(content, model=LOCAL):
    return LLMResponse(
        content=content, model=model, input_tokens=7, output_tokens=3,
        cost_usd=0.0, latency_ms=5.0, provider=model.split("/", 1)[0],
    )


async def _run(*, chain, call_llm, timeout_kind=None):
    """GH#64 harness: retain real dispatch, capture ordinary route errors."""
    called = []

    async def fake_call_llm(model, *args, **kwargs):
        called.append(model)
        return await call_llm(model, *args, **kwargs)

    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    env = {
        "LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_BANDIT": "off",
        "LLM_ROUTER_QUALITY_SKIP": "off",
    }
    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, env))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        p(patch("llm_router.router.get_repo_config", return_value=repo_config.RepoConfig()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock,
                return_value=(None, True, "k")))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
                return_value=list(chain)))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        kwargs = {}
        if timeout_kind == "wall":
            kwargs["max_wall_clock_seconds"] = 0.05
        elif timeout_kind == "deadline":
            kwargs["deadline_monotonic"] = time.monotonic() + 0.4
        try:
            result = await router.route_and_call(
                TaskType.CODE, "hello", profile=RoutingProfile.BALANCED,
                complexity_hint="moderate", **kwargs,
            )
            return result, None, called
        except Exception as exc:
            return None, exc, called
        finally:
            await router.drain_bg_tasks(2.0)


@pytest.mark.asyncio
async def test_exhausted_chain_preserves_attempt_order():
    call = AsyncMock(side_effect=[RuntimeError("first"), RuntimeError("second")])
    result, error, called = await _run(chain=[LOCAL, OTHER_LOCAL], call_llm=call)
    assert result is None
    assert isinstance(error, RuntimeError)
    assert called == [LOCAL, OTHER_LOCAL]
    assert error.chain_attempts == called


@pytest.mark.asyncio
@pytest.mark.timing
@pytest.mark.parametrize("kind,expected", [
    ("wall", WallClockExceeded), ("deadline", DeadlineExceeded),
])
async def test_timeout_preserves_type_and_attempts(kind, expected):
    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    result, error, called = await _run(
        chain=[LOCAL, OTHER_LOCAL], call_llm=slow, timeout_kind=kind,
    )
    assert result is None
    assert type(error) is expected
    assert called and LOCAL in called
    assert error.chain_attempts == called
    assert isinstance(error.__cause__, asyncio.TimeoutError)
    assert error.__cause__.chain_attempts == called


@pytest.mark.asyncio
async def test_cancellation_propagates():
    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.sleep(5)

    task = asyncio.create_task(_run(chain=[LOCAL, OTHER_LOCAL], call_llm=slow))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        if hasattr(caught.value, "chain_attempts"):
            assert caught.value.chain_attempts == [LOCAL]
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def setup_text_job(monkeypatch, call):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    monkeypatch.setattr(T, "route_and_call", call)
    monkeypatch.setattr(T, "_announce_routing", AsyncMock())
    monkeypatch.setattr(T, "_read_hook_route_directive", lambda: None)
    demote = MagicMock()
    monkeypatch.setattr(lr, "demotion_check", demote)
    return demote


@pytest.mark.asyncio
async def test_rejected_then_failed_job_has_no_outcome():
    # The rejected answer is checked by the real check_output implementation.
    error = RuntimeError("route failed")
    error.chain_attempts = [LOCAL]
    call = AsyncMock(side_effect=[response(""), error])
    with pytest.MonkeyPatch.context() as monkeypatch:
        demote = setup_text_job(monkeypatch, call)
        with pytest.raises(RuntimeError) as caught:
            await T.llm_text_job(JOB, DIFF, object())
    assert caught.value is error
    assert call.await_count == 2
    assert not capped_log.read_dicts(paths.state_path(L.LEDGER_FILENAME))
    rows = capped_log.read_dicts(paths.state_path(L.LOCAL_ATTEMPTS_FILENAME))
    assert [(r["model"], r["outcome"]) for r in rows] == [
        (LOCAL, "rejected"), (LOCAL, "error"),
    ]
    demote.assert_called_once_with(LOCAL, JOB)


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_normal_exit_writes_one_outcome(monkeypatch, accepted):
    answer = response("Update src/a.py handling" if accepted else "", CLOUD)
    setup_text_job(monkeypatch, AsyncMock(return_value=answer))
    result = await T.llm_text_job(JOB, DIFF, object())
    rows = capped_log.read_dicts(paths.state_path(L.LEDGER_FILENAME))
    status = L.STATUS_ACCEPTED if accepted else L.STATUS_REJECTED
    assert f"status={status}" in result
    assert len(rows) == 1
    assert rows[0]["status"] == status
    assert rows[0]["attempts"] == (1 if accepted else 3)


@pytest.mark.asyncio
async def test_outcome_failure_is_recorded_and_job_returns(monkeypatch):
    setup_text_job(monkeypatch, AsyncMock(return_value=response("Update src/a.py handling")))
    error = OSError("ledger unavailable")
    monkeypatch.setattr(L, "record_text_job_outcome", MagicMock(side_effect=error))
    record = MagicMock()
    monkeypatch.setattr(failopen, "record", record)
    result = await T.llm_text_job(JOB, DIFF, object())
    assert "status=accepted" in result
    record.assert_any_call("CHZ-FO-TEXTJOB-OUTCOME", error)
