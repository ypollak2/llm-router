"""P0.15 call site in `route_and_call`: the specialist and the bandit's explore step run AFTER the
chain is built and can move any entry to the front. An unvalidated local model must end up last."""
from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import LLMResponse, TaskType

NEW = "ollama/brand-new:7b"
REMOTE = "openai/gpt-4o-mini"
_PROMPTS = (f"p015 route prompt number {n}" for n in itertools.count())


def _ok(model: str) -> LLMResponse:
    return LLMResponse(content="def add(a, b):\n    return a + b\n", model=model, input_tokens=20,
                       output_tokens=12, cost_usd=0.0, latency_ms=50.0, provider=model.split("/", 1)[0])


async def _first_call(monkeypatch, *, gate: str | None) -> list[str]:
    calls: list[str] = []

    async def call(model, *a, **k):
        calls.append(model)
        return _ok(model)

    async def explore(self, models, **k):          # the bandit's explore step: a non-best entry goes first
        return [NEW] + [m for m in models if m != NEW]

    if gate is None:
        monkeypatch.delenv("LLM_ROUTER_PROMOTE_GATE", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", gate)
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "on")
    with patch("llm_router.router._build_and_filter_chain", new=AsyncMock(return_value=[REMOTE, NEW])), \
            patch("llm_router.bandit.EpsilonGreedyBandit.reorder", new=explore), \
            patch("llm_router.router._call_text", new=AsyncMock(side_effect=call)):
        await route_and_call(TaskType.CODE, next(_PROMPTS), complexity_hint="simple")
    return calls


@pytest.mark.asyncio
async def test_the_bandit_cannot_make_an_unvalidated_model_answer_first(temp_db, mock_env, monkeypatch):
    assert (await _first_call(monkeypatch, gate=None))[0] == REMOTE


@pytest.mark.asyncio
async def test_with_the_gate_off_the_bandit_still_decides(temp_db, mock_env, monkeypatch):
    """Negative control: the test above is red only because the sink runs."""
    assert (await _first_call(monkeypatch, gate="off"))[0] == NEW
