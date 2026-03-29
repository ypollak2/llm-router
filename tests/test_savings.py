"""Tests for savings tracking and calculation."""

from __future__ import annotations

import pytest

from llm_router.cost import calc_savings, log_claude_usage, get_savings_summary
from llm_router.types import MODEL_COST_PER_1K, MODEL_SPEED_TPS


class TestCalcSavings:
    def test_haiku_saves_most(self):
        cost_saved, time_saved = calc_savings("haiku", 10_000)
        assert cost_saved > 0
        assert time_saved > 0
        # haiku is 45x cheaper than opus per token
        expected_cost = (10_000 / 1000) * (MODEL_COST_PER_1K["opus"] - MODEL_COST_PER_1K["haiku"])
        assert abs(cost_saved - expected_cost) < 0.0001

    def test_sonnet_saves_some(self):
        cost_saved, time_saved = calc_savings("sonnet", 10_000)
        assert cost_saved > 0
        assert time_saved > 0

    def test_opus_saves_nothing(self):
        cost_saved, time_saved = calc_savings("opus", 10_000)
        assert cost_saved == 0.0
        assert time_saved == 0.0

    def test_haiku_saves_more_than_sonnet(self):
        haiku_cost, haiku_time = calc_savings("haiku", 10_000)
        sonnet_cost, sonnet_time = calc_savings("sonnet", 10_000)
        assert haiku_cost > sonnet_cost
        assert haiku_time > sonnet_time

    def test_savings_scale_with_tokens(self):
        small_cost, _ = calc_savings("haiku", 1_000)
        big_cost, _ = calc_savings("haiku", 10_000)
        assert abs(big_cost - small_cost * 10) < 0.0001

    def test_time_savings_positive(self):
        _, time_saved = calc_savings("haiku", 10_000)
        # haiku at 200 tps vs opus at 60 tps: 50s vs 166s = ~116s saved
        expected_time = (10_000 / MODEL_SPEED_TPS["opus"]) - (10_000 / MODEL_SPEED_TPS["haiku"])
        assert abs(time_saved - expected_time) < 0.1

    def test_zero_tokens(self):
        cost_saved, time_saved = calc_savings("haiku", 0)
        assert cost_saved == 0.0
        assert time_saved == 0.0


class TestLogClaudeUsageReturns:
    @pytest.mark.asyncio
    async def test_returns_savings_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        result = await log_claude_usage("haiku", 5000, "simple")
        assert "cost_saved_usd" in result
        assert "time_saved_sec" in result
        assert result["cost_saved_usd"] > 0
        assert result["time_saved_sec"] > 0

    @pytest.mark.asyncio
    async def test_opus_returns_zero_savings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        result = await log_claude_usage("opus", 5000, "complex")
        assert result["cost_saved_usd"] == 0.0
        assert result["time_saved_sec"] == 0.0


class TestSavingsSummary:
    @pytest.mark.asyncio
    async def test_empty_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        summary = await get_savings_summary("today")
        assert summary["total_calls"] == 0
        assert summary["cost_saved_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_cumulative_savings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))

        await log_claude_usage("haiku", 5000, "simple")
        await log_claude_usage("sonnet", 10000, "moderate")
        await log_claude_usage("opus", 8000, "complex")

        summary = await get_savings_summary("today")
        assert summary["total_calls"] == 3
        assert summary["total_tokens"] == 23000
        assert summary["cost_saved_usd"] > 0
        assert summary["time_saved_sec"] > 0
        assert "haiku" in summary["by_model"]
        assert "sonnet" in summary["by_model"]
        assert "opus" in summary["by_model"]

    @pytest.mark.asyncio
    async def test_haiku_contributes_most_savings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))

        await log_claude_usage("haiku", 10000, "simple")
        await log_claude_usage("sonnet", 10000, "moderate")

        summary = await get_savings_summary("today")
        haiku_saved = summary["by_model"]["haiku"]["cost_saved"]
        sonnet_saved = summary["by_model"]["sonnet"]["cost_saved"]
        assert haiku_saved > sonnet_saved
