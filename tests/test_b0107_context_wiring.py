"""Regression: CHZ-AUD-B-07 (streaming context call always raised) + CHZ-AUD-B-01
(caller_context falls back to the live prompt so keyword retrieval can fire)."""
import ast
import inspect
import sys
from pathlib import Path

from llm_router import context, router

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ast_assert import assert_not_calls


def test_build_context_messages_is_async_keyword_only():
    sig = inspect.signature(context.build_context_messages)
    assert inspect.iscoroutinefunction(context.build_context_messages)
    # All params keyword-only → the old positional streaming call could only raise.
    assert all(p.kind == p.KEYWORD_ONLY for p in sig.parameters.values())


def test_streaming_no_longer_calls_positionally():
    """The route_and_stream source must not call build_context_messages
    positionally/synchronously (the B-07 always-raises bug).

    Was two `"phrase" in inspect.getsource(...)` substring checks. A comment
    quoting either phrase near the real call would satisfy them while the
    call itself regressed — the exact A-10 evasion. `assert_not_calls`
    matches the OLD positional call shape against `ast.unparse` of real CALL
    nodes (comments aren't in the AST), and the "must be awaited" half now
    requires a real `ast.Await` wrapping a call to `build_context_messages`,
    not just the word "await" appearing nearby in text.
    """
    assert_not_calls(
        router.route_and_stream,
        "build_context_messages(prompt, system_prompt, caller_context)",
    )
    tree = ast.parse(inspect.getsource(router.route_and_stream))
    awaited = any(
        isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
        and ast.unparse(n.value.func) == "build_context_messages"
        for n in ast.walk(tree)
    )
    assert awaited, (
        "route_and_stream no longer awaits build_context_messages(...) — the "
        "B-07 bug (a synchronous positional call to an async keyword-only "
        "function, which always raised) would be reintroduced"
    )


def test_primary_paths_fall_back_to_prompt():
    """B-01: the primary build_context_messages call sites use `caller_context or
    prompt` so keyword-relevance retrieval fires even without explicit context.

    Was `"caller_context=caller_context or prompt" in inspect.getsource(...)`
    — a comment repeating that exact text would satisfy it with no real call
    passing that fallback. This instead collects the `caller_context=`
    keyword VALUE from every real call to `build_context_messages` in the
    module and requires at least one to unparse to exactly
    `caller_context or prompt`.
    """
    tree = ast.parse(inspect.getsource(router))
    caller_context_values = [
        kw.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id == "build_context_messages"
        for kw in n.keywords if kw.arg == "caller_context"
    ]
    assert caller_context_values, (
        "no call to build_context_messages passes caller_context="
    )
    assert any(
        ast.unparse(v) == "caller_context or prompt" for v in caller_context_values
    ), (
        "no build_context_messages call site falls back via "
        "`caller_context=caller_context or prompt` — keyword-relevance "
        "retrieval would not fire when the caller passes no explicit context"
    )
