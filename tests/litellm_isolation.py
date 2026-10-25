"""LC-12h repair 4: test-isolation helpers for LiteLLM's process-wide state (used by conftest ``_reset_local_privacy``)."""
from __future__ import annotations

import sys


def discard_litellm_logging() -> None:
    """Throw away every LiteLLM logging task still queued or revivable, without running or awaiting anything.

    LiteLLM's ``LoggingWorker`` carries tasks that had not started when a test's ``asyncio.run`` ended onto the next
    event loop; their success handler looks the model up (``get_model_info``) and, with no ``model_cost`` entry, does
    a proxy-honouring ``POST /api/show``. Draining the worker on a private loop (round 4) bound its ``asyncio.Queue``
    and ``Event`` to that loop and broke later tests under xdist ("bound to a different event loop"). Nothing here
    touches a loop: pending coroutines are closed, and the loop-bound members go back to their initial state, which
    ``LoggingWorker._ensure_queue``/``start`` recreate on the next use."""
    mod = sys.modules.get("litellm.litellm_core_utils.logging_worker")
    worker = getattr(mod, "GLOBAL_LOGGING_WORKER", None)
    if worker is None:
        return
    pending = [t["coroutine"] for t in worker._dequeued_tasks.values()]
    queue = worker._queue
    if queue is not None:
        while True:
            try:
                pending.append(queue.get_nowait()["coroutine"])
            except Exception:  # asyncio.QueueEmpty
                break
    for coro in pending:
        try:
            coro.close()
        except Exception:
            pass
    worker._dequeued_tasks.clear()
    worker._running_tasks.clear()
    worker._queue = None
    worker._sem = None
    worker._worker_task = None
    worker._timeout_summary_task = None
    worker._bound_loop = None


def reset_local_privacy() -> None:
    """Forget what ``local_privacy`` wrote into ``litellm.model_cost`` (see conftest ``_reset_local_privacy``), after
    draining the logging tasks that may still look those entries up."""
    discard_litellm_logging()
    lp = sys.modules.get("llm_router.local_privacy")
    if lp is None:
        return
    cost = getattr(sys.modules.get("litellm"), "model_cost", None)
    for key in list(lp._info_written):
        if isinstance(cost, dict):
            cost.pop(key, None)
    lp._info_written.clear()
    lp._info_attempted.clear()
    lp._info_inflight.clear()
    lp._info_entries.clear()
