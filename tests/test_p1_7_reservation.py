"""P1-7 — the in-process token reservation is symmetric and provider-correct.

Previously the dispatch reserved a hardcoded ``("anthropic", 500)`` up front and
released it asymmetrically: double-released on success, released only half on
chain exhaustion (a leak), and always attributed to "anthropic" regardless of
the model actually called. The reservation now lives per-attempt in the dispatch
loop, keyed to the real provider and released in a ``finally`` — so it can never
leak and the pressure oracle reflects the right provider.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_router import budget as budget_mod
from llm_router.types import Complexity, RoutingProfile, TaskType
from llm_router.router import _dispatch_model_loop


@pytest.fixture(autouse=True)
def _clean_pending():
    budget_mod._pending_tokens.clear()
    yield
    budget_mod._pending_tokens.clear()


def _kwargs(models):
    import structlog
    return dict(
        models_to_try=models, task_type=TaskType.GENERATE,
        profile=RoutingProfile.BUDGET,  # BUDGET → no emergency fallback chain
        prompt="hello world", system_prompt=None, temperature=None,
        max_tokens=256, media_params=None, ctx=None, classification_data={},
        caller_context=None, use_thinking=False, correlation_id="cid-p1-7",
        complexity_hint=None, c=Complexity.SIMPLE, config=MagicMock(),
        route_span=None, route_log=structlog.get_logger("test"),
        _reservation=0.0, effective_complexity="simple",
    )


@pytest.mark.asyncio
async def test_reservation_does_not_leak_on_chain_exhaustion(monkeypatch):
    """Every candidate fails → chain exhausts → no provider keeps a reservation,
    and nothing was ever attributed to a hardcoded 'anthropic'."""
    seen_providers = []

    async def _raising_call_text(model, *a, **k):
        # While in-flight, this provider holds a reservation keyed to ITSELF.
        provider = model.split("/", 1)[0]
        seen_providers.append(provider)
        assert budget_mod._pending_tokens.get(provider, 0) > 0
        assert "anthropic" not in budget_mod._pending_tokens
        raise RuntimeError("provider down")

    monkeypatch.setattr("llm_router.router._call_text", _raising_call_text)

    with pytest.raises(Exception):
        await _dispatch_model_loop(**_kwargs(["openai/gpt-4o", "gemini/gemini-2.5-flash"]))

    # The actual providers were exercised (not anthropic), and every reservation
    # was released — no residual pressure leaks into the oracle.
    assert seen_providers == ["openai", "gemini"]
    assert all(v == 0 for v in budget_mod._pending_tokens.values()), budget_mod._pending_tokens
