"""Tests for budget aggregation behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_spend_aggregation_max_mode():
    from llm_router.budget import _api_provider_state

    cfg = SimpleNamespace(llm_router_spend_aggregation="max")
    with (
        patch("llm_router.budget.get_config", return_value=cfg),
        patch("llm_router.budget._get_cap", return_value=10.0),
        patch("llm_router.budget._get_provider_monthly_spend", new_callable=AsyncMock, return_value=1.0),
        patch("llm_router.integrations.helicone.get_helicone_spend",
              new_callable=AsyncMock, return_value={"openai": 2.0}),
        patch("llm_router.integrations.litellm_budget.is_litellm_budget_enabled", return_value=True),
        patch("llm_router.integrations.litellm_budget.get_litellm_spend",
              new_callable=AsyncMock, return_value={"openai": 3.0}),
    ):
        state = await _api_provider_state("openai")

    assert state.spend_usd == 3.0
    assert state.pressure == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_spend_aggregation_sum_mode():
    from llm_router.budget import _api_provider_state

    cfg = SimpleNamespace(llm_router_spend_aggregation="sum")
    with (
        patch("llm_router.budget.get_config", return_value=cfg),
        patch("llm_router.budget._get_cap", return_value=10.0),
        patch("llm_router.budget._get_provider_monthly_spend", new_callable=AsyncMock, return_value=1.0),
        patch("llm_router.integrations.helicone.get_helicone_spend",
              new_callable=AsyncMock, return_value={"openai": 2.0}),
        patch("llm_router.integrations.litellm_budget.is_litellm_budget_enabled", return_value=True),
        patch("llm_router.integrations.litellm_budget.get_litellm_spend",
              new_callable=AsyncMock, return_value={"openai": 3.0}),
    ):
        state = await _api_provider_state("openai")

    assert state.spend_usd == 6.0
    assert state.pressure == pytest.approx(0.6)
