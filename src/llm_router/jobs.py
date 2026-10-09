"""Background jobs for the long agent tools (AGT A.0).

``llm_act`` / ``llm_delegate`` / ``llm_local_task`` with ``wait=False`` start
their run as an asyncio task on the MCP server's event loop and return
``{"job_id": ...}`` at once. ``llm_router_session(action="job", id=...)``
polls it.

Jobs live in this process only: a server restart forgets them, and a poll for
a forgotten id says ``unknown_job`` rather than pretending the job is running.
At most ``_MAX_JOBS`` are kept; the oldest FINISHED ones are dropped first, and
a running job is never dropped.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
import uuid
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("llm_router.jobs")
_MAX_JOBS = 256
# Which job the current task is running, so agent_exec can report queued/running.
_current_job: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_router_current_job", default=None)
_jobs: dict[str, dict[str, Any]] = {}
# Strong references: the event loop keeps only weak ones to tasks, so an
# unreferenced task can be garbage-collected mid-run.
_tasks: dict[str, asyncio.Task] = {}


def _prune() -> None:
    finished = [jid for jid, j in _jobs.items() if j["status"] in ("done", "failed")]
    while len(_jobs) > _MAX_JOBS and finished:
        _jobs.pop(finished.pop(0), None)


def mark_current(status: str, ctx: contextvars.Context | None = None) -> None:
    """Set the status of the job the calling task (or copied *ctx*) belongs to."""
    jid = ctx.get(_current_job) if ctx is not None else _current_job.get()
    job = _jobs.get(jid or "")
    if job is not None and job["status"] in ("queued", "running"):
        job["status"] = status


def _spawn(tool: str, coro: Coroutine[Any, Any, str]) -> tuple[str, asyncio.Task]:
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"job_id": job_id, "tool": tool, "status": "running",
                     "started_at": time.time(), "finished_at": None,
                     "result": None, "error": None}

    async def _run() -> str | None:
        job = _jobs.get(job_id) or {}
        _current_job.set(job_id)
        try:
            raw = await coro
            try:
                job["result"] = json.loads(raw)
            except (TypeError, ValueError):
                job["result"] = raw
            job["status"] = "done"
            return raw
        except BaseException as exc:  # noqa: BLE001 — record every ending, incl. cancel
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, Exception):
                raise
        finally:
            job["finished_at"] = time.time()
            _tasks.pop(job_id, None)

    task = asyncio.get_running_loop().create_task(_run())
    _tasks[job_id] = task
    _prune()
    return job_id, task


async def run_or_detach(tool: str, coro: Coroutine[Any, Any, str]) -> str:
    """Await *coro* as a job. If the caller is cancelled, a run that is already
    executing keeps going as a background job (id logged); a still-queued one is
    cancelled, since nothing has been spent on it."""
    job_id, task = _spawn(tool, coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.done():
            raise
        if _jobs[job_id]["status"] == "queued":
            task.cancel()
            log.warning("%s cancelled while queued (job %s dropped)", tool, job_id)
        else:
            log.warning("%s caller cancelled; run continues as background job %s "
                        "(poll llm_router_session(action='job', id='%s'))",
                        tool, job_id, job_id)
        raise


def start_job(tool: str, coro: Coroutine[Any, Any, str]) -> dict[str, Any]:
    """Schedule *coro* (which returns the tool's JSON string) and return the handle."""
    job_id, _task = _spawn(tool, coro)
    return {"job_id": job_id, "tool": tool, "status": _jobs[job_id]["status"],
            "poll": f"llm_router_session(action='job', id='{job_id}')"}


def get_job(job_id: str) -> dict[str, Any]:
    """Snapshot of a job; ``{"error": "unknown_job"}`` for an id this process never saw."""
    job = _jobs.get(job_id or "")
    if job is None:
        return {"error": "unknown_job", "job_id": job_id}
    out = dict(job)
    end = out["finished_at"] or time.time()
    out["elapsed_s"] = round(end - out["started_at"], 3)
    return out
