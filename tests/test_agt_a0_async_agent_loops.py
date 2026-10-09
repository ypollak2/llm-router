"""AGT A.0 (A.0-a, clauses 2-3): the long agent tools stop blocking the MCP loop.

The bug: ``llm_delegate`` / ``llm_act`` called the synchronous ``run_delegation``
directly inside an ``async def`` tool, and ``llm_local_task`` did the same with
``run_agent_loop`` and ``_run_check``. Every other MCP call on the server waited
for the whole run (minutes), and two runs could only happen one after the other.

These tests drive the real tool entry points with the real ``run_delegation``.
Only the work is faked: an adapter (or agent loop) that blocks its thread with
``time.sleep``, which is exactly what a model call or subprocess does. A ticker
coroutine on the event loop measures how late it wakes up; on the old code it is
late by the whole run.

Measured numbers are printed (run with ``-s``): n ticks, max lag, durations.

Each measured run is preceded by a short warm-up call. The tools import some
modules lazily on their first call, and that one-off import runs on the loop;
it is printed as ``cold_max_lag`` and is not what A.0-a measures (the run).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from llm_router.agentic.engine import AgentRunResult

DONE = "AGT_A0_DONE"
TICK_S = 0.02
MAX_LAG_S = 0.100       # A.0-a: loop lag <= 100 ms
PARALLEL_RATIO = 1.3    # A.0-a: 2 parallel calls <= 1.3 x one call


class _SlowAdapter:
    """Blocks its thread for ``seconds``, like a model call, then succeeds."""

    tier = 0

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def run(self, milestone, frozen_context, budget_left):
        time.sleep(self.seconds)
        return AgentRunResult({"output": DONE, "tier": 0}, 0.0)


@pytest.fixture
def slow_act(tmp_path, monkeypatch, temp_db):
    """llm_act with a planner of one canary milestone and a slow tier-0 adapter."""
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    import llm_router.tools.agentic as tool

    def _planner_factory():
        def pm(_goal):
            return [{"id": "M1", "description": "slow work",
                     "acceptance": {"type": "canary", "marker": DONE}}]
        return pm

    seconds = {"value": 1.0}
    monkeypatch.setattr(tool, "planner_factory", _planner_factory)
    monkeypatch.setattr(tool, "adapters_factory",
                        lambda: {0: _SlowAdapter(seconds["value"])})

    async def act(run_s: float, **kw) -> dict[str, Any]:
        from llm_router.tools.agentic import llm_delegate
        from llm_router.tools.consolidated import llm_act
        seconds["value"] = run_s
        if "workdir" in kw:   # distinct project roots run in parallel
            return json.loads(await llm_delegate("do slow work", **kw))
        return json.loads(await llm_act("do slow work", **kw))

    act.project = project.resolve()
    return act


async def _with_lag_ticker(coro):
    """Run *coro* while a ticker measures event-loop lag. Returns (result, lags)."""
    lags: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            t0 = loop.time()
            await asyncio.sleep(TICK_S)
            lags.append(loop.time() - t0 - TICK_S)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, lags


@pytest.mark.timing
@pytest.mark.timeout(120)
async def test_loop_lag_stays_under_100ms_during_a_30s_llm_act(slow_act):
    _, cold = await _with_lag_ticker(slow_act(0.0))
    print(f"\nA.0 warm-up llm_act: cold_max_lag={max(cold) * 1000:.1f}ms")
    started = time.monotonic()
    out, lags = await _with_lag_ticker(slow_act(30.0))
    elapsed = time.monotonic() - started
    print(f"\nA.0-a lag: llm_act run={elapsed:.2f}s ticks n={len(lags)} "
          f"max_lag={max(lags) * 1000:.1f}ms p50_lag={sorted(lags)[len(lags) // 2] * 1000:.1f}ms")
    assert out["outcome"] == "complete", out
    assert elapsed >= 30.0
    # A blocked loop yields ~1 tick for the whole run; a free one ~30 s / 20 ms.
    assert len(lags) >= 1000, len(lags)
    assert max(lags) <= MAX_LAG_S, f"max loop lag {max(lags):.3f}s"


@pytest.mark.timing
@pytest.mark.timeout(120)
async def test_two_parallel_llm_act_calls_on_different_roots_take_max_not_sum(slow_act):
    run_s = 3.0
    await slow_act(0.0)  # warm-up: lazy imports would inflate the single-call time
    t0 = time.monotonic()
    one = await slow_act(run_s)
    t_one = time.monotonic() - t0
    t0 = time.monotonic()
    d1, d2 = slow_act.project / "r1", slow_act.project / "r2"
    d1.mkdir(), d2.mkdir()
    a, b = await asyncio.gather(slow_act(run_s, workdir=str(d1)),
                                slow_act(run_s, workdir=str(d2)))
    t_two = time.monotonic() - t0
    print(f"\nA.0-a parallel: n=2 calls of {run_s}s work; one={t_one:.2f}s "
          f"two_parallel={t_two:.2f}s ratio={t_two / t_one:.3f} (limit {PARALLEL_RATIO})")
    assert one["outcome"] == a["outcome"] == b["outcome"] == "complete"
    assert t_two <= PARALLEL_RATIO * t_one


@pytest.mark.timing
async def test_llm_act_wait_false_returns_job_id_and_job_polls_to_result(slow_act):
    from llm_router.tools.consolidated import llm_router_session

    t0 = time.monotonic()
    handle, lags = await _with_lag_ticker(slow_act(2.0, wait=False))
    returned_in = time.monotonic() - t0
    assert returned_in < 1.0, returned_in
    job_id = handle["job_id"]
    assert handle["status"] == "queued"  # task not started yet (#357 review)

    # queued -> running -> done. "queued" is legitimate for as long as the pool has
    # not started the thread (run_agent marks it queued until then), so the first
    # poll may see either; the contract is that the order never goes backwards and
    # the job reaches a terminal state before a bounded deadline (A0-FLAKE-1).
    rank = {"queued": 0, "running": 1, "done": 2, "failed": 2}
    first = await llm_router_session(action="job", id=job_id)
    assert first["status"] in ("queued", "running") and first["result"] is None
    deadline = time.monotonic() + 30
    job, seen = first, [first["status"]]
    while job["status"] in ("queued", "running") and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        job = await llm_router_session(action="job", id=job_id)
        seen.append(job["status"])
    ranks = [rank[x] for x in seen]
    assert ranks == sorted(ranks), seen
    print(f"\nA.0 wait=False: handle in {returned_in * 1000:.0f}ms, job done in "
          f"{job['elapsed_s']}s, status={job['status']}")
    assert job["status"] == "done", job
    assert job["tool"] == "llm_delegate"
    assert job["result"]["outcome"] == "complete"
    # P0.13: the root was resolved while the call was live, not in the job.
    assert job["result"]["project_root"] == str(slow_act.project)
    assert job["result"]["read_only"] is False


async def test_unknown_job_id_is_reported_not_invented():
    from llm_router.tools.consolidated import llm_router_session

    assert (await llm_router_session(action="job", id="nope"))["error"] == "unknown_job"


# ── llm_local_task: run_agent_loop and _run_check off the loop ───────────────

@pytest.mark.timing
@pytest.mark.timeout(60)
async def test_llm_local_task_loop_and_check_do_not_block_the_loop(tmp_path, monkeypatch):
    from llm_router.tools import local_task as lt

    def _slow_loop(**kw):
        if kw["prompt"] != "warm":
            time.sleep(2.0)
        return "edited"

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _slow_loop)
    check = [sys.executable, "-c", "import time; time.sleep(2)"]
    _, cold = await _with_lag_ticker(lt.llm_local_task("warm", str(tmp_path), budget_s=60))
    print(f"\nA.0 warm-up llm_local_task: cold_max_lag={max(cold) * 1000:.1f}ms")
    out, lags = await _with_lag_ticker(
        lt.llm_local_task("o", str(tmp_path), acceptance_check=check, budget_s=60))
    out = json.loads(out)
    print(f"\nA.0 llm_local_task: run={out['elapsed_s']}s ticks n={len(lags)} "
          f"max_lag={max(lags) * 1000:.1f}ms")
    assert out["status"] == "verified_complete", out
    assert len(lags) >= 100
    assert max(lags) <= MAX_LAG_S


async def _poll(job_id, want, deadline_s=60):
    """Poll until the job's status is in *want* (no wall-clock assumption on speed)."""
    from llm_router.tools.consolidated import llm_router_session
    end = time.monotonic() + deadline_s
    job = await llm_router_session(action="job", id=job_id)
    while job["status"] not in want and time.monotonic() < end:
        await asyncio.sleep(0.02)
        job = await llm_router_session(action="job", id=job_id)
    assert job["status"] in want, job
    return job


async def test_llm_local_task_wait_false_returns_job(tmp_path, monkeypatch):
    """Deterministic queued -> running -> done: a 1-worker pool is held busy by a
    blocker, so the job provably waits in the queue; nothing here is timing-based."""
    import concurrent.futures
    import threading
    from llm_router import agent_exec
    from llm_router.tools import local_task as lt

    monkeypatch.setattr(agent_exec, "_executor",
                        concurrent.futures.ThreadPoolExecutor(max_workers=1))
    blocker_in, release_blocker = threading.Event(), threading.Event()
    pool_blocker = agent_exec._pool().submit(
        lambda: (blocker_in.set(), release_blocker.wait(60)))
    assert blocker_in.wait(10)
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop",
                        lambda **kw: "ok")
    try:
        handle = json.loads(await lt.llm_local_task("o", str(tmp_path), wait=False))
        job = await _poll(handle["job_id"], ("queued",))
        await asyncio.sleep(0.2)  # still queued: the only worker is busy
        assert (await _poll(handle["job_id"], ("queued",)))["status"] == "queued"
        release_blocker.set()
        job = await _poll(handle["job_id"], ("done", "failed"))
    finally:
        release_blocker.set()
        pool_blocker.result(10)
        ex, agent_exec._executor = agent_exec._executor, None
        ex.shutdown(wait=True)
    assert job["status"] == "done" and job["tool"] == "llm_local_task", job
    assert job["result"]["status"] == "proposed"


async def test_overlapping_local_tasks_never_leak_apply_writes(tmp_path, monkeypatch):
    """Off the loop, two runs can overlap. A propose-only run must never see the
    apply mode another run set, and the variable must be gone after both."""
    from llm_router.tools import local_task as lt

    monkeypatch.delenv("LLM_ROUTER_AGENT_WRITES", raising=False)
    seen: dict[str, Any] = {}

    def _loop(**kw):
        name = Path(kw["project_root"]).name
        seen.setdefault(name, []).append(os.environ.get("LLM_ROUTER_AGENT_WRITES"))
        time.sleep(0.5)
        seen[name].append(os.environ.get("LLM_ROUTER_AGENT_WRITES"))
        return "ok"

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop)
    (tmp_path / "apply").mkdir()
    (tmp_path / "propose").mkdir()
    await asyncio.gather(
        lt.llm_local_task("o", str(tmp_path / "apply"), apply_writes=True),
        lt.llm_local_task("o", str(tmp_path / "propose"), apply_writes=False),
    )
    assert seen["apply"] == ["apply", "apply"]
    assert seen["propose"] == [None, None]
    assert "LLM_ROUTER_AGENT_WRITES" not in os.environ
