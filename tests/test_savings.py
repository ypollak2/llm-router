"""Tests for savings tracking and calculation."""

from __future__ import annotations

import json

import pytest

from llm_router import cost
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
    async def test_empty_summary(self, temp_db):
        summary = await get_savings_summary("today")
        assert summary["total_calls"] == 0
        assert summary["cost_saved_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_cumulative_savings(self, temp_db):

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
    async def test_haiku_contributes_most_savings(self, temp_db):

        await log_claude_usage("haiku", 10000, "simple")
        await log_claude_usage("sonnet", 10000, "moderate")

        summary = await get_savings_summary("today")
        haiku_saved = summary["by_model"]["haiku"]["cost_saved"]
        sonnet_saved = summary["by_model"]["sonnet"]["cost_saved"]
        assert haiku_saved > sonnet_saved


# ── Savings persistence (new routing_decisions-era functions) ────────────────


@pytest.fixture
def temp_savings_db(tmp_path, monkeypatch):
    """Temp DB + temp JSONL path for savings persistence tests."""
    db_path = tmp_path / "test_savings.db"
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    # Reset config singleton so it reads the new env vars
    import llm_router.config as config_module
    config_module._config = None
    log_path = tmp_path / "savings_log.jsonl"
    monkeypatch.setattr(cost, "SAVINGS_LOG_PATH", log_path)
    return db_path, log_path


class TestLogSavingsPersistence:
    @pytest.mark.asyncio
    async def test_log_savings_persists(self, temp_savings_db):
        db_path, _ = temp_savings_db
        await cost.log_savings(
            task_type="query",
            estimated_saved=0.033,
            external_cost=0.001,
            model="gemini/flash",
            session_id="test-session",
        )
        summary = await cost.get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] == 1
        assert summary["total_saved"] == pytest.approx(0.033)
        assert summary["total_external_cost"] == pytest.approx(0.001)
        assert summary["net_savings"] == pytest.approx(0.032)

    @pytest.mark.asyncio
    async def test_log_savings_multiple_sessions(self, temp_savings_db):
        db_path, _ = temp_savings_db
        await cost.log_savings("query", 0.03, 0.001, "flash", "session-1")
        await cost.log_savings("code", 0.05, 0.002, "gpt-4o-mini", "session-1")
        await cost.log_savings("research", 0.10, 0.005, "sonar", "session-2")

        summary = await cost.get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] == 3
        assert len(summary["by_session"]) == 2
        session_ids = {s["session_id"] for s in summary["by_session"]}
        assert session_ids == {"session-1", "session-2"}


class TestLifetimeSavingsSummary:
    @pytest.mark.asyncio
    async def test_empty_summary(self, temp_savings_db):
        summary = await cost.get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] == 0
        assert summary["total_saved"] == 0.0
        assert summary["by_session"] == []

    @pytest.mark.asyncio
    async def test_net_savings_calculation(self, temp_savings_db):
        db_path, _ = temp_savings_db
        await cost.log_savings("query", 0.10, 0.03, "model", "s1")
        await cost.log_savings("code", 0.20, 0.05, "model", "s1")

        summary = await cost.get_lifetime_savings_summary(days=0)
        assert summary["net_savings"] == pytest.approx(0.22)


class TestImportSavingsLog:
    @pytest.mark.asyncio
    async def test_import_basic(self, temp_savings_db):
        _, log_path = temp_savings_db
        entries = [
            {"timestamp": "2026-03-29T10:00:00Z", "session_id": "s1",
             "task_type": "query", "estimated_saved": 0.033,
             "external_cost": 0.001, "model": "flash"},
            {"timestamp": "2026-03-29T10:01:00Z", "session_id": "s1",
             "task_type": "code", "estimated_saved": 0.05,
             "external_cost": 0.002, "model": "gpt-4o-mini"},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        imported = await cost.import_savings_log()
        assert imported == 2
        assert log_path.read_text() == ""

        summary = await cost.get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] == 2

    @pytest.mark.asyncio
    async def test_import_skips_bad_json(self, temp_savings_db):
        _, log_path = temp_savings_db
        log_path.write_text(
            '{"task_type":"query","estimated_saved":0.03,"external_cost":0,"model":"m","session_id":"s"}\n'
            'NOT_VALID_JSON\n'
            '{"task_type":"code","estimated_saved":0.05,"external_cost":0,"model":"m","session_id":"s"}\n'
        )
        imported = await cost.import_savings_log()
        assert imported == 2

    @pytest.mark.asyncio
    async def test_import_missing_file(self, temp_savings_db):
        _, log_path = temp_savings_db
        assert not log_path.exists()
        assert await cost.import_savings_log() == 0

    @pytest.mark.asyncio
    async def test_import_empty_file(self, temp_savings_db):
        _, log_path = temp_savings_db
        log_path.write_text("")
        assert await cost.import_savings_log() == 0
