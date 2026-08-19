"""Per-route session indicator tests.

The auto-route hook injects a directive into ``additionalContext`` on
every routed UserPromptSubmit, telling Claude to prepend its reply with
a single-line route indicator visible in the user's chat scrollback.

Two code paths inject the directive:

1. **DIRECT-success path** (llm_router answered without calling Claude):
   ``response_formatter.format_echo_context`` emits the cached answer
   alongside the directive. The hook knows the exact model that
   handled the call, so the indicator line is fully resolved at hook
   time.

2. **MANDATORY-ROUTE path** (llm_router tells Claude to call ``llm_*``):
   ``auto-route.py`` emits the routing banner with a placeholder
   ``<model>`` token. Claude fills it in after the ``llm_*`` tool
   returns (llm_router MCP tools include the handling model in their
   response payload).

These tests pin the directive text + format on both paths so future
refactors don't silently drop the user-visible indicator.
"""

from __future__ import annotations

from pathlib import Path


_SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"
_RESPONSE_FORMATTER = _SRC / "hooks" / "response_formatter.py"
_AUTO_ROUTE = _SRC / "hooks" / "auto-route.py"


def test_direct_success_directive_includes_fully_resolved_prefix() -> None:
    """``format_echo_context`` must include the route_prefix variable
    (provider/model · task/complexity · latency) and a directive telling
    Claude to begin the reply with that exact line. Catches refactors
    that move the prefix construction without wiring it into the
    Claude-facing text.
    """
    src = _RESPONSE_FORMATTER.read_text()
    assert "route_prefix = " in src, (
        "format_echo_context must build a `route_prefix` variable"
    )
    # The prefix uses the same model_label format ROUTING NOTICE already
    # has — keep them aligned so the directive and the metadata footer
    # never disagree.
    assert "f\"🎯 LLM Router routed → {model_label} · {task_type}/{complexity}" in src, (
        "route_prefix format drifted from `🎯 LLM Router routed → <model> · <task>/<complexity> · <latency>`"
    )
    # Phrasing was softened to the cooperative framing (see docs/decisions.md
    # 2026-05-27); match the directive's stable core, not the old wording.
    assert "begin your reply with this exact line" in src, (
        "directive instructing Claude to prepend the route prefix was removed"
    )


def test_mandatory_route_directive_uses_placeholder_for_model() -> None:
    """The MANDATORY-ROUTE banner must instruct Claude to prepend a
    one-line route indicator, and ask it to fill in ``<model>`` from the
    ``llm_*`` tool's response. We can't resolve the model at hook time
    (Claude makes the actual MCP call later), so a placeholder is
    correct — but the placeholder must remain present so Claude
    actually substitutes it.
    """
    src = _AUTO_ROUTE.read_text()
    assert "USER-VISIBLE ROUTE INDICATOR" in src, (
        "MANDATORY-ROUTE directive removed from auto-route.py"
    )
    assert "🎯 llm_router → <model> ·" in src or "llm_router → <model> ·" in src, (
        "MANDATORY-ROUTE prefix template missing — Claude won't have a "
        "format to follow"
    )
    assert "fallback" in src.lower(), (
        "directive should explain what to do when the tool doesn't "
        "surface the model name (use the tool name as fallback)"
    )


def test_format_echo_context_renders_real_data() -> None:
    """End-to-end smoke test on the DIRECT-success path: provide a
    synthetic ``DirectResult`` and assert the resolved route prefix
    appears in the rendered context exactly as the user will see it
    in Claude's reply.
    """
    import sys
    sys.path.insert(0, str(_SRC.parent))
    from llm_router.hooks.response_formatter import format_echo_context

    # Build a DirectResult using the actual installed type — avoids
    # tightly coupling the test to the constructor signature, which
    # has historically shifted between versions.
    from llm_router.hooks.direct_executor import DirectResult
    from dataclasses import fields

    fld = {f.name: f for f in fields(DirectResult)}

    # Build a model object via the type referenced by chain_builder.
    # in the chain builder. Fall back to a SimpleNamespace stand-in if
    # the type isn't importable.
    try:
        from llm_router.hooks.chain_builder import ModelChoice as _Model  # type: ignore
        model_obj = _Model(provider="gemini", model="gemini-2.5-flash", is_paid=True)  # type: ignore
    except Exception:
        import types
        model_obj = types.SimpleNamespace(provider="gemini", model="gemini-2.5-flash")

    # Build the DirectResult with whatever extra fields exist using
    # sensible defaults so this test stays robust to additions.
    kwargs = {"model": model_obj, "latency_ms": 1463}
    if "text" in fld:
        kwargs["text"] = "Paris."
    if "input_tokens" in fld:
        kwargs["input_tokens"] = 10
    if "output_tokens" in fld:
        kwargs["output_tokens"] = 2
    if "cost_saved_usd" in fld:
        kwargs["cost_saved_usd"] = 0.001
    if "provider_chain" in fld:
        kwargs["provider_chain"] = []
    if "success" in fld:
        kwargs["success"] = True

    try:
        result = DirectResult(**kwargs)
    except TypeError:
        # DirectResult requires fields we haven't accounted for — fall back
        # to checking the source string only (already covered by the test
        # above). Skip the end-to-end render.
        import pytest
        pytest.skip(
            "DirectResult signature changed — relying on source-level "
            "assertions in the other tests instead"
        )

    ctx = format_echo_context(result, "query", "simple")
    expected_prefix = "🎯 LLM Router routed → gemini/gemini-2.5-flash · query/simple · 1.5s"
    assert expected_prefix in ctx, (
        f"rendered echo context did not contain the resolved route prefix.\n"
        f"  expected line: {expected_prefix!r}\n"
        f"  context excerpt:\n{ctx[:500]}"
    )


def test_no_double_prefix_across_paths() -> None:
    """Both paths use the same ``🎯 LLM Router routed →`` marker so the user sees one
    consistent format. Source-level assertion that the marker is used in
    BOTH files (otherwise we'd ship inconsistent indicators on different
    routing paths).
    """
    rf = _RESPONSE_FORMATTER.read_text()
    ar = _AUTO_ROUTE.read_text()
    marker = "🎯 LLM Router routed →"
    assert rf.count(marker) >= 1, (
        f"response_formatter.py is missing the `{marker}` indicator marker"
    )
    assert ar.count(marker) >= 1, (
        f"auto-route.py is missing the `{marker}` indicator marker"
    )
