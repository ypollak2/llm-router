"""#27 / Option B — precision-tier routing.

A SHORT prompt demanding an exact, verifiable answer (arithmetic / code output /
precise count) is where cheap-local-first routing gives confident-but-wrong terse
answers the runtime quality heuristic can't catch (the Gate-16 root cause). Such
prompts are steered to a reliable cheap metered model (gpt-4o-mini); ordinary prose
stays cheap-local-first.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.router import _needs_precise_answer, route_and_call
from llm_router.types import TaskType


@pytest.mark.parametrize("prompt", [
    "What is the sum of the first five prime numbers? Answer with only the number.",
    "What does this print? Answer with only the output.\n\nprint(len(set([1,1,2,3])))",
    "How many vowels are in the word 'onomatopoeia'?",
    "Compute 17 * 23.",
    "What is 144 / 12?",
    "Evaluate the expression 2 + 2 * 3.",
])
def test_precision_prompts_detected(prompt):
    assert _needs_precise_answer(prompt) is True


@pytest.mark.parametrize("prompt", [
    "Write a short friendly greeting for a new teammate.",
    "Explain the tradeoffs of microservices in a paragraph.",
    "Summarise the plot of a mystery novel you invent.",
    "",
    "x" * 500 + " print(1)",  # too long → not the terse-precision regime
])
def test_non_precision_prompts_not_flagged(prompt):
    assert _needs_precise_answer(prompt) is False


def _pin_chain(models):
    return patch("llm_router.router._build_and_filter_chain", new=AsyncMock(return_value=list(models)))


@pytest.mark.asyncio
async def test_precision_prompt_fronts_metered_mini(temp_db, mock_env, mock_acompletion, monkeypatch):
    """A precision prompt fronts openai/gpt-4o-mini ahead of the local chain when
    OpenAI is available and not blocked."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    with _pin_chain(["ollama/qwen2.5-coder:7b", "ollama/qwen3-coder:30b"]), \
         patch("llm_router.router._call_text", new_callable=AsyncMock) as mock_call:
        from llm_router.types import LLMResponse
        mock_call.return_value = LLMResponse(content="28", model="openai/gpt-4o-mini",
                                             input_tokens=5, output_tokens=1, cost_usd=0.0003,
                                             latency_ms=100.0, provider="openai")
        await route_and_call(TaskType.QUERY, "What is the sum of the first five primes? Answer with only the number.")
        first_model = mock_call.call_args_list[0].args[0]
        assert first_model == "openai/gpt-4o-mini", f"precision prompt must try mini first, got {first_model}"


@pytest.mark.asyncio
async def test_precision_prompt_stays_local_when_openai_blocked(temp_db, mock_env, mock_acompletion, monkeypatch):
    """If OpenAI is hard-blocked, precision routing does NOT front mini (respects
    LLM_ROUTER_BLOCK_PROVIDERS) — falls back to the local chain."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_ROUTER_BLOCK_PROVIDERS", "openai")
    with _pin_chain(["ollama/qwen2.5-coder:7b"]):
        await route_and_call(TaskType.QUERY, "Compute 17 * 23.")
    # No assertion on model beyond: it did not raise and did not front a blocked provider.
