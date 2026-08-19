"""Tests that the MCP text tools actually pipe through response_router.

The router itself has its own tests (``response_router.py``); this file
pins the *wiring contract* — that every llm_* tool calls the routing
shim on its way out. A future refactor that forgets to wrap one of the
tools won't silently regress this batch's quota savings.
"""

from __future__ import annotations


import pytest

import llm_router.tools.text as text_tools


@pytest.fixture
def captured_routes(monkeypatch):
    """Record every (prompt, response) that passes through the response
    router so individual tests can assert on the call counts + payloads.
    """
    calls: list[str] = []

    async def fake_route_response(response: str) -> str:
        calls.append(response)
        # Strip "ok " prefix to make it obvious the routed version flowed
        # through to the caller — proves the return value is the routed
        # text, not the original.
        return "[routed] " + response

    monkeypatch.setattr(text_tools, "route_response", fake_route_response)
    return calls


@pytest.mark.asyncio
async def test_apply_response_router_routes_non_empty(captured_routes):
    out = await text_tools._apply_response_router("a longer body of text")
    assert out == "[routed] a longer body of text"
    assert captured_routes == ["a longer body of text"]


@pytest.mark.asyncio
async def test_apply_response_router_short_circuits_empty(captured_routes):
    """Empty strings skip routing entirely — no point burning a router
    call when there's nothing to route."""
    out = await text_tools._apply_response_router("")
    assert out == ""
    assert captured_routes == []


@pytest.mark.asyncio
async def test_apply_response_router_swallows_errors(monkeypatch):
    """Any failure inside the router must NOT mask the original answer.

    The user is owed the underlying tool's response even if the
    optimiser falls over. Critical: this is the only path that returns
    something to Claude, and Claude needs SOMETHING coherent to keep
    going.
    """
    async def boom(_response: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(text_tools, "route_response", boom)
    out = await text_tools._apply_response_router("real answer")
    assert out == "real answer"


@pytest.mark.asyncio
async def test_each_mcp_tool_routes_its_return_value(captured_routes,
                                                     monkeypatch):
    """Every llm_* tool in tools.text must pipe through _apply_response_router.

    We stub route_and_call so the test doesn't try to actually call a
    provider, then verify each tool's final return value comes back
    with the routed-prefix marker.
    """
    from unittest.mock import AsyncMock, MagicMock

    fake_resp = MagicMock(
        content="resp", model="haiku", cost_usd=0.0,
        latency_ms=10, citations=[], input_tokens=10,
        output_tokens=5, provider="anthropic", success=True,
        cache_similarity=None, cache_hit=False, classifier_data={},
        classifier_method="heuristic", complexity="simple",
        classification_data={}, reasoning_steps=[],
    )
    monkeypatch.setattr(text_tools, "route_and_call",
                        AsyncMock(return_value=fake_resp))
    monkeypatch.setattr(text_tools, "_cache_result", lambda *a, **kw: None)
    monkeypatch.setattr(text_tools, "_record_quality", lambda *a, **kw: None)
    # Patch the formatter too — we're testing the wiring, not the
    # formatter. A real-shaped string lets the router shim see something
    # to operate on without dragging in every LLMResponse field.
    monkeypatch.setattr(text_tools, "_format_response",
                        lambda resp, task="": f"formatted-{task}")

    ctx = MagicMock()

    out = await text_tools.llm_query(prompt="hi", ctx=ctx)
    assert out.startswith("[routed] "), (
        f"llm_query must route its response; got {out!r}"
    )

    captured_routes.clear()
    out = await text_tools.llm_generate(prompt="hi", ctx=ctx)
    assert out.startswith("[routed] ")

    captured_routes.clear()
    out = await text_tools.llm_analyze(prompt="hi", ctx=ctx)
    assert out.startswith("[routed] ")

    captured_routes.clear()
    out = await text_tools.llm_code(prompt="hi", ctx=ctx)
    assert out.startswith("[routed] ")
