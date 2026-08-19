"""Regression tests for P2 quality-gated escalation guards.

The escalation feature scores a cheap model's response and, if inadequate,
escalates one hop to the next model. Earlier it escalated a short-but-correct
answer (e.g. "OK") into a slow reasoning model (~60s). These tests lock in the
guards: short-prompt exemption and the one-hop bound.

The candidate chain is pinned to two fast, call_llm-backed local models so the
tests don't depend on codex/subscription injection in the test env.
"""

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import TaskType

# Two fast local models (no slow/reasoning markers) so escalation, when it fires,
# targets a call_llm-backed model deterministically.
_TWO_FAST = ["ollama/fast-a:7b", "ollama/fast-b:7b"]


def _pin_chain(models):
    """Patch the chain builder to return a fixed candidate list."""
    return patch(
        "llm_router.router._build_and_filter_chain",
        new=AsyncMock(return_value=list(models)),
    )


@pytest.mark.asyncio
async def test_escalation_skipped_for_short_prompt(
    temp_db, mock_env, mock_acompletion, monkeypatch
):
    """A short answer to a short prompt is proportionate — do not escalate."""
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "1")
    with _pin_chain(_TWO_FAST):
        await route_and_call(TaskType.QUERY, "say OK", complexity_hint="simple")
    assert mock_acompletion.call_count == 1  # short-prompt guard → one call


@pytest.mark.asyncio
async def test_escalation_fires_once_for_long_prompt(
    temp_db, mock_env, mock_acompletion, monkeypatch
):
    """A low-scoring answer to a substantial prompt escalates exactly one hop."""
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "1")
    long_prompt = "Please analyze this problem in depth and explain: " + "detail " * 40
    with _pin_chain(_TWO_FAST):
        await route_and_call(TaskType.ANALYZE, long_prompt, complexity_hint="moderate")
    # Mock response scores < threshold → escalate once → 2 calls, then stop.
    assert mock_acompletion.call_count == 2


@pytest.mark.asyncio
async def test_escalation_off_makes_single_call(
    temp_db, mock_env, mock_acompletion, monkeypatch
):
    """With escalation off, even a low-scoring long prompt makes a single call."""
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")
    long_prompt = "Please analyze this problem in depth and explain: " + "detail " * 40
    with _pin_chain(_TWO_FAST):
        await route_and_call(TaskType.ANALYZE, long_prompt, complexity_hint="moderate")
    assert mock_acompletion.call_count == 1
