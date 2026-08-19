"""Lever ① — the exhaustion floor.

Root cause found in the clean metered benchmark: on 5 hard prompts LLM Router's
premium chain was *exhausted* and returned `<exhausted>` / q=1 — not because
the models couldn't answer, but because a heuristic gate (STRUCTURE: "needs
Markdown markers") rejected every real answer, and the router then raised
"all models failed" and handed the caller nothing.

Gates catch *garbage*, not *wrong answers*. So when the whole chain is exhausted
purely by gate/quality rejections, the router must return the best rejected answer
as a degraded floor rather than failing the route. These tests lock that in:

- fail-before: with the floor removed, an all-gate-fail chain raises RuntimeError.
- pass-after: the same chain now returns the (gate-rejected) answer.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import LLMResponse, TaskType

# An ANALYZE contract carries [LENGTH, STRUCTURE]. This body PASSES length
# (>200 chars) but FAILS structure: no Markdown markers, no blank-line
# paragraphs, and no sentence-terminating punctuation → a genuine "wall".
_WALL = "spam wall of tokens with no structure or sentence end " * 8  # ~430 chars
_TWO_MODELS = ["ollama/model-a:7b", "ollama/model-b:7b"]


def _pin_chain(models):
    return patch(
        "llm_router.router._build_and_filter_chain",
        new=AsyncMock(return_value=list(models)),
    )


def _wall_response(model: str) -> LLMResponse:
    return LLMResponse(
        content=_WALL, model=model, input_tokens=50, output_tokens=120,
        cost_usd=0.001, latency_ms=100.0, provider="ollama",
    )


@pytest.fixture(autouse=True)
def _gates_on(monkeypatch):
    """Force gates to run under pytest (mirrors real premium routing)."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LLM_ROUTER_GATES", "on")
    # Keep quality-escalation out of the picture — this test is about gate
    # rejection exhausting the chain, not the P2 escalation hop.
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")


@pytest.mark.asyncio
async def test_all_gate_fail_returns_floor_not_raise(temp_db, mock_env):
    """Every model in the chain gate-fails (STRUCTURE) → the router returns the
    best rejected answer as a degraded floor instead of raising."""
    async def _return_wall(model, *a, **k):
        return _wall_response(model)

    with _pin_chain(_TWO_MODELS), patch(
        "llm_router.router._call_text", new=AsyncMock(side_effect=_return_wall)
    ):
        resp = await route_and_call(
            TaskType.ANALYZE,
            "Analyze the tradeoffs of sharding a write-heavy index in depth.",
            complexity_hint="moderate",
        )

    # The floor answer IS the (gate-rejected) content — not an exception, not empty.
    assert resp is not None
    assert resp.content == _WALL
    assert resp.model in _TWO_MODELS


@pytest.mark.asyncio
async def test_floor_emits_structured_event(temp_db, mock_env):
    """The degraded floor return must be observable, never silent — it emits a
    first-class `exhaustion_floor_returned` structured event naming the floor
    model and the chain that was rejected."""
    import structlog

    async def _return_wall(model, *a, **k):
        return _wall_response(model)

    with _pin_chain(_TWO_MODELS), patch(
        "llm_router.router._call_text", new=AsyncMock(side_effect=_return_wall)
    ), structlog.testing.capture_logs() as logs:
        await route_and_call(
            TaskType.ANALYZE,
            "Analyze the tradeoffs of sharding a write-heavy index in depth.",
            complexity_hint="moderate",
        )

    floor_events = [e for e in logs if e.get("event") == "exhaustion_floor_returned"]
    assert floor_events, f"expected exhaustion_floor_returned event, got: {logs}"
    ev = floor_events[0]
    assert ev["floor_model"] in _TWO_MODELS
    assert ev["floor_chars"] == len(_WALL)


@pytest.mark.asyncio
async def test_no_content_still_raises(temp_db, mock_env):
    """A genuine failure — every model errors with NO content ever produced —
    must still raise. The floor only applies when a real answer exists."""
    with _pin_chain(_TWO_MODELS), patch(
        "llm_router.router._call_text",
        new=AsyncMock(side_effect=RuntimeError("provider down")),
    ):
        with pytest.raises(RuntimeError, match="All models failed|provider down"):
            await route_and_call(
                TaskType.ANALYZE,
                "Analyze the tradeoffs of sharding a write-heavy index in depth.",
                complexity_hint="moderate",
            )
