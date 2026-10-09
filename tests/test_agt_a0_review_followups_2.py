"""#357 independent-review follow-ups: inode-keyed root locks, honest 'queued'
from start_job, and a findable 'detached' jobs listing."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

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
    def __init__(self) -> None:
        self.lock, self.now, self.peak = threading.Lock(), 0, 0

    def work(self, seconds: float = 0.3) -> str:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(seconds)
        with self.lock:
            self.now -= 1
        return "ok"


async def _run_two(a, b) -> _Probe:
    p = _Probe()
    await asyncio.gather(agent_exec.run_agent(p.work, root=a),
                         agent_exec.run_agent(p.work, root=b))
    return p


def _case_insensitive(path) -> bool:
    return os.path.exists(str(path).swapcase())


async def test_differently_cased_paths_serialise(tmp_path):
    d = tmp_path / "Proj"
    d.mkdir()
    variant = tmp_path / "pROJ"
    if not variant.exists():
        pytest.skip("filesystem is case-sensitive")
    assert (await _run_two(d, variant)).peak == 1


async def test_symlink_to_same_dir_serialises(tmp_path):
    d = tmp_path / "real"
    d.mkdir()
    link = tmp_path / "link"
    link.symlink_to(d)
    assert (await _run_two(d, link)).peak == 1


@pytest.mark.timing
async def test_different_dirs_stay_parallel(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert (await _run_two(a, b)).peak == 2


async def test_lock_key_for_missing_dir_falls_back_to_path(tmp_path):
    missing = tmp_path / "nope"
    assert agent_exec._root_lock(missing) is agent_exec._root_lock(missing)
    assert agent_exec._root_lock(missing) is not agent_exec._root_lock(tmp_path / "other")


@pytest.mark.timing
async def test_start_job_reports_queued_until_the_task_starts():
    async def coro() -> str:
        return json.dumps({"ok": 1})

    handle = jobs.start_job("t", coro())
    assert handle["status"] == "queued"
    for _ in range(100):
        if jobs.get_job(handle["job_id"])["status"] == "done":
            break
        await asyncio.sleep(0.01)
    assert jobs.get_job(handle["job_id"])["status"] == "done"


@pytest.mark.timing
async def test_detached_job_is_listed_for_the_user(tmp_path):
    started, release = threading.Event(), threading.Event()

    def work() -> str:
        started.set()
        release.wait(10)
        return "{}"

    async def go():
        return await agent_exec.run_agent(work, root=tmp_path)

    caller = asyncio.create_task(jobs.run_or_detach("t", go()))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set()
    assert jobs.list_detached() == []          # not detached while the caller waits
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    (job_id,) = jobs._jobs
    listing = jobs.list_detached()
    assert [j["job_id"] for j in listing] == [job_id]
    assert jobs.get_job(job_id)["detached"] is True

    from llm_router.tools.consolidated import llm_router_session
    out = await llm_router_session(action="job", id="detached")
    assert [j["job_id"] for j in out["detached"]] == [job_id]
    release.set()
    for _ in range(100):
        if jobs.get_job(job_id)["status"] == "done":
            break
        await asyncio.sleep(0.02)


async def test_queued_cancel_is_not_listed_as_detached(tmp_path):
    async def slow() -> str:
        await asyncio.sleep(5)
        return "{}"

    caller = asyncio.create_task(jobs.run_or_detach("t", slow()))
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert jobs.list_detached() == []
