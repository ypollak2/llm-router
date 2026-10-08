"""Where the long agent runs execute (AGT A.0 review follow-up).

Three rules, one place:

* **Dedicated, bounded executor.** Agent loops block a thread for minutes. On the
  loop's default executor they starved every other ``asyncio.to_thread`` user in
  the server. They get their own pool, sized by ``LLM_ROUTER_AGENT_WORKERS``
  (default 4). A run beyond the bound waits in the pool's queue and its job shows
  ``queued`` until a thread picks it up.
* **One run per project root at a time.** Two runs writing the same tree let each
  run's acceptance check see the other's edits. A per-root asyncio lock (no
  thread held while waiting) serialises them; different roots stay parallel.
* **Cancel-safe.** A client cancelling a ``wait=True`` call cannot stop a worker
  thread. ``run_or_detach`` therefore makes every run a job: if the caller is
  cancelled while the run is executing it is *converted to a background job*
  (id logged, pollable); if it was still queued it is cancelled outright.
  A cancel flag would need cooperation inside the loops, and the delegation
  engine and the ReAct agent have no between-step hook; a detached job works
  for all of them and never loses a run that already spent money.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import weakref
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("llm_router.agent_exec")

DEFAULT_WORKERS = 4
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # loop -> {root: Lock}


def worker_limit() -> int:
    try:
        return max(1, int(os.environ.get("LLM_ROUTER_AGENT_WORKERS", DEFAULT_WORKERS)))
    except ValueError:
        return DEFAULT_WORKERS


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_limit(), thread_name_prefix="agent-run")
    return _executor


def _root_lock(root: str | Path) -> asyncio.Lock:
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(str(Path(root).expanduser().resolve()), asyncio.Lock())


async def run_agent(fn: Callable[..., Any], *args: Any, root: str | Path, **kw: Any) -> Any:
    """Run sync *fn* on the agent pool, one at a time per *root*."""
    from llm_router.jobs import mark_current
    mark_current("queued")

    def _in_thread() -> Any:
        mark_current("running", ctx=ctx)
        return fn(*args, **kw)

    import contextvars
    ctx = contextvars.copy_context()
    async with _root_lock(root):
        return await asyncio.get_running_loop().run_in_executor(
            _pool(), functools.partial(ctx.run, _in_thread))
