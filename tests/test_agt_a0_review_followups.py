"""AGT A.0 review follow-ups: per-root serialisation, bounded pool + 'queued',
cancellation, the task GC guard, zip-import of agents.yaml.

Each test was run against the pre-fix code and failed there (see the PR body).
"""
from __future__ import annotations

import asyncio
import gc
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from llm_router import agent_exec, jobs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(agent_exec, "_executor", None)
    jobs._jobs.clear()
    yield
    ex, agent_exec._executor = agent_exec._executor, None
    if ex is not None:
        ex.shutdown(wait=True)


class _Probe:
    """Records how many runs are inside it at once, per instance."""

    def __init__(self) -> None:
        self.lock, self.now, self.peak = threading.Lock(), 0, 0

    def work(self, seconds: float = 0.4) -> str:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(seconds)
        with self.lock:
            self.now -= 1
        return "ok"


# 1 ── same root serial, different roots parallel ───────────────────────────
async def test_same_root_runs_are_serialised(tmp_path):
    p = _Probe()
    t0 = time.monotonic()
    await asyncio.gather(*(agent_exec.run_agent(p.work, 0.4, root=tmp_path) for _ in range(2)))
    assert p.peak == 1
    assert time.monotonic() - t0 >= 0.78


async def test_same_root_via_symlink_is_the_same_root(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    p = _Probe()
    await asyncio.gather(agent_exec.run_agent(p.work, 0.3, root=tmp_path / "real"),
                         agent_exec.run_agent(p.work, 0.3, root=tmp_path / "link"))
    assert p.peak == 1


@pytest.mark.timing
async def test_different_roots_stay_parallel(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    p = _Probe()
    t0 = time.monotonic()
    await asyncio.gather(agent_exec.run_agent(p.work, 0.6, root=tmp_path / "a"),
                         agent_exec.run_agent(p.work, 0.6, root=tmp_path / "b"))
    assert p.peak == 2
    assert time.monotonic() - t0 <= 1.3 * 0.6 + 0.1


async def test_llm_act_same_root_does_not_overlap(tmp_path, monkeypatch, temp_db):
    """End to end through the tool: two llm_act calls, one project root."""
    from llm_router.agentic.engine import AgentRunResult
    import llm_router.tools.agentic as tool
    from llm_router.tools.consolidated import llm_act

    for var in ("CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    p = _Probe()

    class A:
        tier = 0

        def run(self, m, ctx, budget):
            p.work(0.4)
            return AgentRunResult({"output": "AGT_REVIEW_DONE", "tier": 0}, 0.0)

    monkeypatch.setattr(tool, "planner_factory", lambda: (lambda g: [
        {"id": "M1", "description": "w", "acceptance": {"type": "canary", "marker": "AGT_REVIEW_DONE"}}]))
    monkeypatch.setattr(tool, "adapters_factory", lambda: {0: A()})
    outs = await asyncio.gather(llm_act("t"), llm_act("t"))
    assert all(json.loads(o)["outcome"] == "complete" for o in outs), outs
    assert p.peak == 1


# 2 ── bounded pool, visible 'queued' ───────────────────────────────────────
@pytest.mark.timing
async def test_pool_is_bounded_and_queued_jobs_say_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WORKERS", "2")
    roots = []
    for i in range(4):
        (tmp_path / str(i)).mkdir()
        roots.append(tmp_path / str(i))
    p = _Probe()
    gate = threading.Event()

    def work():
        p.work(0.0)
        return "{}"

    def blocked() -> str:
        with p.lock:
            p.now += 1
            p.peak = max(p.peak, p.now)
        gate.wait(10)
        with p.lock:
            p.now -= 1
        return "{}"

    handles = [jobs.start_job("t", agent_exec_wrap(blocked, r)) for r in roots]
    await asyncio.sleep(0.3)
    statuses = sorted(jobs.get_job(h["job_id"])["status"] for h in handles)
    assert p.peak == 2
    assert statuses == ["queued", "queued", "running", "running"], statuses
    gate.set()
    for _ in range(100):
        if all(jobs.get_job(h["job_id"])["status"] == "done" for h in handles):
            break
        await asyncio.sleep(0.05)
    assert all(jobs.get_job(h["job_id"])["status"] == "done" for h in handles)


def agent_exec_wrap(fn, root):
    async def go():
        return await agent_exec.run_agent(fn, root=root)
    return go()


def test_workers_env_is_registered():
    from llm_router.env_registry import registered_names
    assert "LLM_ROUTER_AGENT_WORKERS" in registered_names()


async def test_agent_loops_do_not_use_the_default_executor(tmp_path):
    """The MCP server's other to_thread users must not share threads with agents."""
    names = []
    await agent_exec.run_agent(lambda: names.append(threading.current_thread().name), root=tmp_path)
    assert names[0].startswith("agent-run"), names


# 3 ── cancellation ─────────────────────────────────────────────────────────
@pytest.mark.timing
async def test_cancelled_wait_true_call_continues_as_a_pollable_job(tmp_path, caplog):
    started, release = threading.Event(), threading.Event()

    def work() -> str:
        started.set()
        release.wait(10)
        return json.dumps({"fin": True})

    caller = asyncio.create_task(jobs.run_or_detach("t", agent_exec_wrap(work, tmp_path)))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set()
    with caplog.at_level("WARNING", logger="llm_router.jobs"):
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    (job_id,) = jobs._jobs
    assert job_id in caplog.text                       # id is logged
    assert jobs.get_job(job_id)["status"] == "running"  # worker still alive, pollable
    release.set()
    for _ in range(100):
        if jobs.get_job(job_id)["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert jobs.get_job(job_id)["status"] == "done"
    assert jobs.get_job(job_id)["result"] == {"fin": True}


async def test_cancel_while_queued_drops_the_run(tmp_path):
    release, ran = threading.Event(), []

    def hold() -> str:
        release.wait(10)
        return "{}"

    first = asyncio.create_task(jobs.run_or_detach("t", agent_exec_wrap(hold, tmp_path)))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(jobs.run_or_detach(
        "t", agent_exec_wrap(lambda: ran.append(1) or "{}", tmp_path)))  # same root: queued
    await asyncio.sleep(0.2)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    await first
    await asyncio.sleep(0.2)
    assert ran == []


# 4 ── GC guard ─────────────────────────────────────────────────────────────
@pytest.mark.timing
async def test_job_task_is_strongly_referenced_until_it_finishes():
    gate = asyncio.Event()

    async def coro():
        await gate.wait()
        return json.dumps({"ok": 1})

    h = jobs.start_job("t", coro())
    assert h["job_id"] in jobs._tasks
    # What the guard defends against: only the loop's weak reference left.
    gc.collect()
    await asyncio.sleep(0.05)
    gc.collect()
    gate.set()
    for _ in range(50):
        if jobs.get_job(h["job_id"])["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert jobs.get_job(h["job_id"])["status"] == "done"
    assert h["job_id"] not in jobs._tasks


# 5 ── agents.yaml from a zipped package ────────────────────────────────────
def test_agents_yaml_resolves_from_a_zipped_package(tmp_path):
    import subprocess
    import sys
    repo_src = Path(jobs.__file__).resolve().parent
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in repo_src.rglob("*"):
            if f.is_file() and f.suffix in {".py", ".yaml"} and "__pycache__" not in f.parts:
                zf.write(f, Path("llm_router") / f.relative_to(repo_src))
    probe = (
        "import llm_router, json\n"
        "from llm_router.tools import agents\n"
        "p = agents._default_config_path()\n"
        "print(json.dumps({'file': llm_router.__file__, 'p': str(p), 'exists': p.exists(),"
        " 'ids': agents.get_registry().list_ids()}))\n"
    )
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LLM_ROUTER_AGENTS_CONFIG")}
    env.update(PYTHONPATH=str(z), HOME=str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    # run from an empty dir so no project config/agents.yaml is found
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                       cwd=str(tmp_path / "h"), env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert str(z) in out["file"], out          # really imported from the zip
    assert out["exists"] is True
    assert "code-reviewer" in out["ids"]
