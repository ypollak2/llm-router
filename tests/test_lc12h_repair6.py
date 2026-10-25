"""LC-12h repair 4: a LiteLLM logging task carried over from one test's event loop must not outlive its model-info entry.

Root cause of the flaky ``test_b_secret_route_is_local_only_without_cache_or_judge`` (a POST ``{"name":"m"}`` at the
recording proxy, a model the test never uses): LiteLLM's ``LoggingWorker`` hands logging tasks that had not started when
a test's ``asyncio.run`` ended to the NEXT event loop ("carried over / revived onto the new loop",
``litellm_core_utils/logging_worker.py::_ensure_queue``). The success handler calls ``get_model_cost_information`` ->
``get_model_info`` -> ``OllamaModelInfo.get_runtime_model_info`` (a proxy-honouring ``POST /api/show``) when the model
has no ``litellm.model_cost`` entry. The ``_reset_local_privacy`` fixture removed the entry at the previous test's
teardown, so the carried task ran in a later test and did its own lookup against that test's proxy.

Here the carried-over task is made on purpose (enqueued, loop closed before the worker runs), so the race is gone.
"""
from __future__ import annotations

import asyncio

import litellm
from litellm.litellm_core_utils.litellm_logging import StandardLoggingPayloadSetup
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

from llm_router import local_privacy
from tests import litellm_isolation
from tests.test_lc12g import servers  # noqa: F401  (fixture)

MODEL = "ollama/lc12h-r4-fx:1b"


async def _enqueue_late_logging_task(api_base: str) -> None:
    """What LiteLLM's success handler does for a finished call: look the model up for the standard logging payload.
    Returning at once leaves the item in the worker's queue (the worker task has not run yet)."""
    async def late():
        StandardLoggingPayloadSetup.get_model_cost_information(
            base_model=MODEL, custom_pricing=False, custom_llm_provider="ollama", init_response_obj={},
            api_base=api_base)
    GLOBAL_LOGGING_WORKER.start()                           # (enqueue() drops the item while no queue exists)
    GLOBAL_LOGGING_WORKER.enqueue(late())


def test_a_late_logging_task_finds_the_entry_even_after_the_test_teardown(servers):
    proxy, ollama = servers
    bare = MODEL.split("/", 1)[1]
    local_privacy._install_model_info(litellm, bare, local_privacy._model_info_entry(bare, None))
    assert f"ollama/{bare}" in litellm.model_cost
    asyncio.run(_enqueue_late_logging_task(ollama.url))     # test N: the loop ends with the task still queued
    litellm_isolation.reset_local_privacy()                 # test N teardown
    assert f"ollama/{bare}" not in litellm.model_cost       # (the entry is gone, as the fixture intends)

    async def next_test():                                  # test N+1: any asyncio.run; the worker revives the task
        GLOBAL_LOGGING_WORKER.start()
        await asyncio.sleep(0.5)
    asyncio.run(next_test())
    assert proxy.seen == [] and [p for _m, p, _b in ollama.seen if p == "/api/show"] == []
