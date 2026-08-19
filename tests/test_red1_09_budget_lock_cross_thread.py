"""Regression: RED1-09 — the budget lock must serialize across threads/loops.

`_budget_lock()` was a per-event-loop asyncio.Lock. The gateway
(ThreadingHTTPServer + asyncio.run() per request) runs every concurrent request
on a distinct short-lived loop, so each got a distinct lock → zero cross-request
mutual exclusion, and `_pending_spend` suffered lost updates. This reproduces the
exact deployment shape (N OS threads, each its own asyncio.run) and asserts the
critical section is now serialized: no lost increments to a shared counter.
"""

from __future__ import annotations

import asyncio
import threading

from llm_router import router


def test_budget_lock_serializes_across_threads_and_loops():
    N = 12
    ITERS = 40
    counter = {"v": 0}  # a plain, unsynchronized RMW guarded only by _budget_lock

    async def critical_once():
        async with router._budget_lock():
            # Read-modify-write with an await inside the section: if the lock
            # were per-loop (not shared), concurrent threads would interleave
            # here and lose increments.
            v = counter["v"]
            await asyncio.sleep(0)  # yield inside the section
            counter["v"] = v + 1

    async def worker():
        for _ in range(ITERS):
            await critical_once()

    def thread_body():
        asyncio.run(worker())  # NEW loop per thread — exactly the gateway shape

    threads = [threading.Thread(target=thread_body) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter["v"] == N * ITERS, (
        f"RED1-09: lost updates under cross-thread contention — "
        f"got {counter['v']}, expected {N * ITERS} (budget lock did not serialize)"
    )


def test_budget_lock_is_process_wide_single_instance():
    """Two acquisitions from different loops must contend for the SAME lock."""
    seen_locked = {"v": False}

    async def hold_and_probe():
        async with router._budget_lock():
            # While held, a non-blocking acquire from this same process must fail
            # (proving one shared underlying threading.Lock, not per-loop).
            seen_locked["v"] = not router._budget_proc_lock.acquire(blocking=False)
            if not seen_locked["v"]:
                router._budget_proc_lock.release()

    asyncio.run(hold_and_probe())
    assert seen_locked["v"], "budget lock is not a single process-wide lock"
