import asyncio


def test_budget_lock_is_event_loop_local():
    from llm_router import router

    # Return the lock *object* (not its id) and keep both alive while
    # comparing. Comparing bare id()s was flaky: after the first loop's lock
    # was garbage-collected, the allocator could reuse the same address for
    # the second loop's lock, making id(first) == id(second) spuriously.
    async def get_lock():
        async with router._budget_lock():
            return router._budget_lock()

    first = asyncio.run(get_lock())
    second = asyncio.run(get_lock())

    assert first is not second


def test_disabled_subprocess_backends_from_env(monkeypatch):
    from llm_router import router

    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex, gemini_cli")

    assert router._disabled_subprocess_backends() == {"codex", "gemini_cli"}
