"""AGT A.0 repair: queued ``llm_local_task`` runs are serial end to end.

The first A.0 cut ran the loop in a worker thread under ``_AGENT_ENV_LOCK`` but
took the "before" snapshot and started the budget timer on the event loop,
before the lock. A queued run therefore (1) diffed across another run's writes
and reported them as its own ``changed_files``, and (2) had the time it spent
waiting for the lock charged to its budget, so its acceptance check was skipped
or cut short. Both were red on 96596f65 and green on main 4395c3b8, where the
whole coroutine ran without yielding. The fix takes the snapshot, the timer, the
loop, the second snapshot and the check inside the locked worker.

Also here: the ``wait=False`` input check, and ``jobs`` behaviours no other test
pinned (prune keeps running jobs, a failed job reports ``failed``, the
``session_id`` fallback on the ``job`` action).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest


@pytest.mark.timeout(30)
async def test_overlapping_local_tasks_changed_files_attribution(tmp_path, monkeypatch):
    """Job A writes a.txt; job B, queued behind it on the same root, writes nothing."""
    from llm_router.tools import local_task as lt

    def _loop(**kw):
        if kw["prompt"] == "A":
            time.sleep(0.5)
            (tmp_path / "a.txt").write_text("from A\n")
        else:
            time.sleep(0.1)
        return "ok"

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop)

    async def b_after_a_started():
        await asyncio.sleep(0.1)   # A holds the lock, a.txt not yet written
        return await lt.llm_local_task("B", str(tmp_path))

    a, b = await asyncio.gather(lt.llm_local_task("A", str(tmp_path)), b_after_a_started())
    a, b = json.loads(a), json.loads(b)
    print(f"\nA changed {a['changed_files']} B changed {b['changed_files']} "
          f"B queued_s={b.get('queued_s')}")
    assert a["changed_files"] == ["a.txt"]
    assert b["changed_files"] == [], "B reported A's edit as its own"


@pytest.mark.timeout(30)
async def test_queued_local_task_budget_not_eaten_by_lock_wait(tmp_path, monkeypatch):
    """Two 1.0 s runs with budget 1.5 s each: the second waits ~1 s for the lock,
    and that wait must not eat its budget and skip its check."""
    from llm_router.tools import local_task as lt

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop",
                        lambda **kw: (time.sleep(1.0), "ok")[1])
    check = [sys.executable, "-c", "pass"]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    t0 = time.monotonic()
    outs = await asyncio.gather(
        lt.llm_local_task("o", str(tmp_path / "a"), acceptance_check=check, budget_s=1.5),
        lt.llm_local_task("o", str(tmp_path / "b"), acceptance_check=check, budget_s=1.5),
    )
    wall = time.monotonic() - t0
    outs = [json.loads(o) for o in outs]
    print(f"\nqueued budget: wall={wall:.2f}s "
          + " ".join(f"[{o['status']} elapsed={o['elapsed_s']} queued={o.get('queued_s')}]"
                     for o in outs))
    assert wall >= 2.0, "runs were expected to be serial"
    for o in outs:
        assert o["status"] == "verified_complete", o
    assert max(o["queued_s"] for o in outs) >= 0.9


async def test_wait_false_rejects_a_bad_workdir_without_a_job(tmp_path):
    from llm_router import jobs
    from llm_router.tools import local_task as lt

    before = set(jobs._jobs)
    out = json.loads(await lt.llm_local_task("o", str(tmp_path / "missing"), wait=False))
    assert out["status"] == lt.BLOCKED
    assert "job_id" not in out
    assert set(jobs._jobs) == before


# ── jobs.py behaviours ───────────────────────────────────────────────────────

async def test_prune_never_drops_a_running_job(monkeypatch):
    from llm_router import jobs

    monkeypatch.setattr(jobs, "_MAX_JOBS", 2)
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_tasks", {})
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return "{}"

    async def fast():
        return "{}"

    running = jobs.start_job("t", slow())["job_id"]
    done_ids = [jobs.start_job("t", fast())["job_id"] for _ in range(3)]
    await asyncio.sleep(0.05)
    jobs.start_job("t", fast())   # triggers a prune with 3 finished + 1 running + 1 new
    assert running in jobs._jobs and jobs._jobs[running]["status"] == "running"
    assert done_ids[0] not in jobs._jobs, "the oldest finished job should go first"
    assert len(jobs._jobs) <= 3
    gate.set()
    await asyncio.sleep(0.05)
    assert jobs.get_job(running)["status"] == "done"


async def test_a_raising_job_reports_failed_with_the_error():
    from llm_router import jobs
    from llm_router.tools.consolidated import llm_router_session

    async def boom():
        raise RuntimeError("worker fell over")

    jid = jobs.start_job("t", boom())["job_id"]
    await asyncio.sleep(0.05)
    job = await llm_router_session(action="job", session_id=jid)   # session_id fallback
    assert job["status"] == "failed"
    assert job["error"] == "RuntimeError: worker fell over"
    assert job["result"] is None and job["finished_at"] is not None
    assert jid not in jobs._tasks


# ── SLT-2: the router's own state dir is never a task's change ───────────────

@pytest.mark.parametrize("git_repo", [True, False])
async def test_router_state_dir_inside_workdir_is_not_attributed(tmp_path, monkeypatch, git_repo):
    """LLM_ROUTER_HOME inside the workdir (the suite's layout, and a user whose
    project is their home dir): a WAL write by the router mid-run is not the task's."""
    import subprocess
    from llm_router.tools import local_task as lt

    if git_repo:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    state = tmp_path / "home" / "knowledge" / "semantic"
    state.mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))

    def _loop(**kw):
        (state / "index.sqlite-wal").write_bytes(b"router bookkeeping")
        (tmp_path / "a.txt").write_text("from the task\n")
        return "ok"

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _loop)
    out = json.loads(await lt.llm_local_task("A", str(tmp_path)))
    assert out["changed_files"] == ["a.txt"], out["changed_files"]
