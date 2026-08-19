"""Regression: CHZ-AUD-B-07 (streaming context call always raised) + CHZ-AUD-B-01
(caller_context falls back to the live prompt so keyword retrieval can fire)."""
import inspect
from llm_router import context, router


def test_build_context_messages_is_async_keyword_only():
    sig = inspect.signature(context.build_context_messages)
    assert inspect.iscoroutinefunction(context.build_context_messages)
    # All params keyword-only → the old positional streaming call could only raise.
    assert all(p.kind == p.KEYWORD_ONLY for p in sig.parameters.values())


def test_streaming_no_longer_calls_positionally():
    """The route_and_stream source must not call build_context_messages
    positionally/synchronously (the B-07 always-raises bug)."""
    src = inspect.getsource(router.route_and_stream)
    assert "build_context_messages(prompt, system_prompt, caller_context)" not in src
    assert "await build_context_messages(" in src


def test_primary_paths_fall_back_to_prompt():
    """B-01: the primary build_context_messages call sites use `caller_context or
    prompt` so keyword-relevance retrieval fires even without explicit context."""
    src = inspect.getsource(router)
    assert "caller_context=caller_context or prompt" in src
