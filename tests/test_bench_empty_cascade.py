"""Regression test: bench routers must cascade on empty-content responses.

Background — June 6 2026 smoke run
-----------------------------------
``bench/results/20260606-150229.md`` shows 3 of 5 easy prompts (easy-02
YAML→JSON, easy-04 Python one-liner, easy-05 email extraction) where
every router returned an empty string from ``ollama/qwen3.5:latest`` and
the bench scored them ``q=1`` instead of cascading to the next model in
the chain. The production router handles this correctly via
``inference_robustness.ensure_non_empty_content`` (Plan 07 §D.3) but the
bench was calling ``litellm.acompletion`` directly and bypassing it.

This test mocks the litellm call so the first model returns empty and
the second returns real content — and asserts the router picked the
second model. Without the fix, the router silently returns the first
model's empty string.

Update (#5, real LLMRouterRouter): ``LLMRouterRouter`` no longer runs its own
litellm cascade — it delegates to ``llm_router.router.route_and_call``, which owns
the empty-response handling via ``inference_robustness.ensure_non_empty_content``
(the production path this docstring already credited). So the LLMRouterRouter-specific
bench cascade test was retired: the behavior now lives in the real router it calls
and is covered by the production router's own tests. ``StaticChainRouter`` still
runs the local cascade, so its regression stays here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def _fake_completions():
    """Patch litellm.acompletion to drive the per-model responses.

    First call returns an empty completion (mimics qwen3.5 doing nothing
    useful for code/extraction prompts); second call returns real text.
    Tracks call count so the test can verify both models were tried.
    """
    from types import SimpleNamespace

    state = {"call_count": 0, "models_called": []}

    async def fake_acompletion(*, model, messages, **_kwargs):
        state["call_count"] += 1
        state["models_called"].append(model)
        # First model: empty content
        # Second model onwards: useful content
        if state["call_count"] == 1:
            content = ""
            in_tok, out_tok = 10, 0
        else:
            content = "Paris"
            in_tok, out_tok = 10, 5
        choice = SimpleNamespace(message=SimpleNamespace(content=content))
        return SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok),
        )

    with patch("litellm.acompletion", new=fake_acompletion):
        yield state


@pytest.mark.asyncio
async def test_static_chain_cascades_on_empty_response(_fake_completions):
    """Same contract for the StaticChainRouter — both must behave the same."""
    from bench.routers import StaticChainRouter

    router = StaticChainRouter()
    result = await router.route("What is the capital of France?")

    assert _fake_completions["call_count"] >= 2
    assert result.response.strip() == "Paris"
    assert result.error == ""


@pytest.mark.asyncio
async def test_empty_response_error_is_runtime_error():
    """The contract is: EmptyResponseError extends RuntimeError so the
    bench routers' bare ``except Exception`` catches it. If anyone narrows
    that exception base, the cascade silently breaks."""
    from llm_router.inference_robustness import EmptyResponseError

    assert issubclass(EmptyResponseError, RuntimeError)
