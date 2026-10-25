"""LC-12h repair 4: test-isolation helpers for LiteLLM's process-wide state (used by conftest ``_reset_local_privacy``)."""
from __future__ import annotations

import asyncio
import sys


def flush_litellm_logging(timeout_s: float = 10.0) -> None:
    """Run every LiteLLM logging task still queued or revivable, on a private loop, before returning.

    LiteLLM's ``LoggingWorker`` carries tasks that had not started when a test's ``asyncio.run`` ended onto the next
    event loop; their success handler looks the model up (``get_model_info``) and, with no ``model_cost`` entry, does
    a proxy-honouring ``POST /api/show``. Draining them while the entry is still there makes that lookup a no-op."""
    mod = sys.modules.get("litellm.litellm_core_utils.logging_worker")
    worker = getattr(mod, "GLOBAL_LOGGING_WORKER", None)
    if worker is None:
        return
    try:
        asyncio.get_running_loop()
        return  # called from inside a running loop (an async test's own teardown): flush() cannot be awaited here
    except RuntimeError:
        pass
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(worker.flush(), timeout_s))
    except asyncio.TimeoutError:
        pass  # a hung callback: carry on with the reset exactly as before this helper existed
    finally:
        loop.close()


def reset_local_privacy() -> None:
    """Forget what ``local_privacy`` wrote into ``litellm.model_cost`` (see conftest ``_reset_local_privacy``), after
    draining the logging tasks that may still look those entries up."""
    flush_litellm_logging()
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
