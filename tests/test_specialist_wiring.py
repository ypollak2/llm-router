"""Plan 07 Phase 3 B.2b — integration test for end-to-end specialist override.

Proves the wiring at `router.py:1454-1485` is live: when the active
RoutingPolicy declares a subject specialist and the classifier emits a
matching subject, the specialist is the first model attempted regardless
of what `_build_and_filter_chain` returned.

The earlier B.2a tests cover the pure transformation; this file covers
the integration boundary — that route_and_call actually consults the
policy and applies the override.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.policy import RoutingPolicy
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, TaskType


def _ok_response(model: str) -> LLMResponse:
    """Build a minimal LLMResponse for stubbing _call_text."""
    return LLMResponse(
        content="test response",
        model=model,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.0,
        latency_ms=42.0,
        provider=model.split("/")[0] if "/" in model else "unknown",
    )


@pytest.mark.asyncio
async def test_specialist_attempted_first_when_subject_matches(temp_db) -> None:
    """When policy.specialists['code']='openrouter/qwen-coder' and
    classification_data['subject']='code', qwen-coder is the model called
    first — not the first entry from _build_and_filter_chain."""

    captured_models: list[str] = []

    async def capture_call(model: str, *args, **kwargs) -> LLMResponse:
        captured_models.append(model)
        return _ok_response(model)

    policy_with_specialist = RoutingPolicy(
        name="specialist_test",
        description="",
        specialists={"code": "openrouter/qwen-coder"},
    )

    with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock):
        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock):
            with patch("llm_router.router.get_config") as mock_config:
                config = MagicMock()
                config.llm_router_monthly_budget = 0.0
                config.llm_router_daily_spend_limit = 0.0
                config.available_providers = ["openai", "openrouter"]
                mock_config.return_value = config

                with patch(
                    "llm_router.router._build_and_filter_chain"
                ) as mock_chain:
                    # Chain returned WITHOUT the specialist — the override
                    # must inject it at position 0.
                    mock_chain.return_value = [
                        "openai/gpt-4o-mini",
                        "openai/gpt-4o",
                    ]

                    with patch(
                        "llm_router.policy.get_active_policy",
                        return_value=policy_with_specialist,
                    ):
                        with patch(
                            "llm_router.router._call_text",
                            new=AsyncMock(side_effect=capture_call),
                        ):
                            with patch(
                                "llm_router.router.get_tracker"
                            ) as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker

                                await route_and_call(
                                    TaskType.CODE,
                                    "Refactor this function",
                                    classification_data={"subject": "code"},
                                )

    assert captured_models, "_call_text was never invoked"
    assert captured_models[0] == "openrouter/qwen-coder", (
        f"Expected specialist first, got chain order: {captured_models}"
    )


@pytest.mark.asyncio
async def test_no_override_when_specialists_empty(temp_db) -> None:
    """If policy has no specialist for the subject, original chain order
    is preserved (no specialist injected)."""

    captured_models: list[str] = []

    async def capture_call(model: str, *args, **kwargs) -> LLMResponse:
        captured_models.append(model)
        return _ok_response(model)

    policy_no_specialist = RoutingPolicy(
        name="no_spec",
        description="",
        specialists={},  # empty — no override should happen
    )

    with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock):
        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock):
            with patch("llm_router.router.get_config") as mock_config:
                config = MagicMock()
                config.llm_router_monthly_budget = 0.0
                config.llm_router_daily_spend_limit = 0.0
                config.available_providers = ["openai"]
                mock_config.return_value = config

                with patch(
                    "llm_router.router._build_and_filter_chain"
                ) as mock_chain:
                    mock_chain.return_value = ["openai/gpt-4o-mini"]

                    with patch(
                        "llm_router.policy.get_active_policy",
                        return_value=policy_no_specialist,
                    ):
                        with patch(
                            "llm_router.router._call_text",
                            new=AsyncMock(side_effect=capture_call),
                        ):
                            with patch(
                                "llm_router.router.get_tracker"
                            ) as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker

                                await route_and_call(
                                    TaskType.CODE,
                                    "test",
                                    classification_data={"subject": "code"},
                                )

    assert captured_models[0] == "openai/gpt-4o-mini", (
        f"Expected original chain order, got: {captured_models}"
    )


@pytest.mark.asyncio
async def test_no_override_when_classification_data_missing_subject(
    temp_db,
) -> None:
    """Backwards compat: pre-Phase-3 callers passing classification_data
    without a `subject` key (or no classification_data at all) get the
    original chain unchanged. Important because router.py is called from
    many CLI/MCP entry points that haven't been updated."""

    captured_models: list[str] = []

    async def capture_call(model: str, *args, **kwargs) -> LLMResponse:
        captured_models.append(model)
        return _ok_response(model)

    # Even a policy WITH specialists shouldn't kick in if subject is missing.
    policy_with_specialist = RoutingPolicy(
        name="t",
        description="",
        specialists={"code": "openrouter/qwen-coder"},
    )

    with patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock):
        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock):
            with patch("llm_router.router.get_config") as mock_config:
                config = MagicMock()
                config.llm_router_monthly_budget = 0.0
                config.llm_router_daily_spend_limit = 0.0
                config.available_providers = ["openai"]
                mock_config.return_value = config

                with patch(
                    "llm_router.router._build_and_filter_chain"
                ) as mock_chain:
                    mock_chain.return_value = ["openai/gpt-4o-mini"]

                    with patch(
                        "llm_router.policy.get_active_policy",
                        return_value=policy_with_specialist,
                    ):
                        with patch(
                            "llm_router.router._call_text",
                            new=AsyncMock(side_effect=capture_call),
                        ):
                            with patch(
                                "llm_router.router.get_tracker"
                            ) as mock_tracker:
                                tracker = MagicMock()
                                tracker.is_healthy.return_value = True
                                mock_tracker.return_value = tracker

                                # No classification_data — pre-Phase-3 caller.
                                await route_and_call(
                                    TaskType.CODE,
                                    "test",
                                )

    assert captured_models[0] == "openai/gpt-4o-mini", (
        "Specialist must not be injected when classification_data is None — "
        f"got: {captured_models}"
    )
