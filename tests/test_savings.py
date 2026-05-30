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


# ─────────────────────────────────────────────────────────────────────────────
# v9.2.2 — cache-aware 4-component cost, per-task baseline, honest floor
# ─────────────────────────────────────────────────────────────────────────────


class TestCacheAwareCost:
    """Fix #1 — cost.py should track input/output/cache_write/cache_read separately
    and price each at its own published Anthropic rate."""

    def test_claude_cost_input_output_only(self):
        from llm_router.cost import _claude_cost
        # Sonnet: 1000 input × $3/Mtok + 500 output × $15/Mtok
        #       = (3000 + 7500) / 1_000_000 = $0.0105
        cost_usd = _claude_cost("sonnet", input_t=1000, output_t=500)
        assert abs(cost_usd - 0.0105) < 1e-6

    def test_claude_cost_with_cache_read_is_much_cheaper(self):
        from llm_router.cost import _claude_cost
        # 10_000 cache_read tokens at Sonnet's $0.30/Mtok = $0.003
        # vs 10_000 raw input tokens at $3/Mtok = $0.030 — 10× cheaper
        cached = _claude_cost("sonnet", input_t=0, output_t=0, cache_read_t=10_000)
        uncached = _claude_cost("sonnet", input_t=10_000, output_t=0)
        assert cached < uncached / 5  # at least 5× cheaper

    def test_claude_cost_with_cache_write_is_more_expensive(self):
        from llm_router.cost import _claude_cost
        # Cache write is ~25% more expensive than input
        write = _claude_cost("sonnet", input_t=0, output_t=0, cache_write_t=10_000)
        regular = _claude_cost("sonnet", input_t=10_000, output_t=0)
        assert write > regular

    def test_claude_cost_full_4_component(self):
        from llm_router.cost import _claude_cost
        # Opus: 1000 in × 15 + 500 out × 75 + 200 cw × 18.75 + 5000 cr × 1.50
        # = 15_000 + 37_500 + 3750 + 7500 = 63_750 / 1_000_000 = $0.06375
        cost_usd = _claude_cost(
            "opus", input_t=1000, output_t=500,
            cache_write_t=200, cache_read_t=5000,
        )
        assert abs(cost_usd - 0.06375) < 1e-6

    def test_claude_cost_unknown_model_returns_zero(self):
        from llm_router.cost import _claude_cost
        assert _claude_cost("nonexistent-model", input_t=1000, output_t=500) == 0.0

    @pytest.mark.asyncio
    async def test_log_claude_usage_persists_cache_token_columns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        await log_claude_usage(
            model="sonnet",
            tokens_used=0,  # forces computation from sub-components
            complexity="moderate",
            input_tokens=100,
            output_tokens=200,
            cache_creation_input_tokens=5_000,
            cache_read_input_tokens=10_000,
        )
        # Read it back via aiosqlite
        import aiosqlite
        async with aiosqlite.connect(tmp_path / "test.db") as db:
            cursor = await db.execute(
                "SELECT input_tokens, output_tokens, cache_creation_input_tokens, "
                "cache_read_input_tokens FROM claude_usage ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        assert row == (100, 200, 5_000, 10_000)


class TestTaskAwareBaseline:
    """Fix #2 — picking the *realistic* baseline (what would have been used
    without routing) rather than always crediting Opus-vs-cheap delta."""

    def test_simple_query_baseline_is_haiku(self):
        from llm_router.cost import _get_baseline_for_task
        assert _get_baseline_for_task("query", "simple") == "haiku"

    def test_moderate_query_baseline_is_haiku(self):
        from llm_router.cost import _get_baseline_for_task
        assert _get_baseline_for_task("query", "moderate") == "haiku"

    def test_code_moderate_baseline_is_sonnet(self):
        from llm_router.cost import _get_baseline_for_task
        assert _get_baseline_for_task("code", "moderate") == "sonnet"

    def test_complex_anything_baseline_is_opus(self):
        from llm_router.cost import _get_baseline_for_task
        assert _get_baseline_for_task("code", "complex") == "opus"
        assert _get_baseline_for_task("analyze", "complex") == "opus"
        assert _get_baseline_for_task("query", "complex") == "opus"

    def test_research_baseline_is_opus(self):
        from llm_router.cost import _get_baseline_for_task
        assert _get_baseline_for_task("research", "simple") == "opus"

    def test_haiku_in_baseline_pricing(self):
        """Haiku must be in the BASELINE_PRICING table for task-aware logic to work."""
        from llm_router.cost import BASELINE_PRICING
        assert "haiku" in BASELINE_PRICING
        assert "input" in BASELINE_PRICING["haiku"]
        assert "output" in BASELINE_PRICING["haiku"]


class TestNegativeSavingsAndRoutingOverhead:
    """Fix #3 — drop max(0.0, ...) clamp; track routing overhead so the realized
    savings number is honest about when routing cost more than it saved."""

    def test_savings_can_be_negative_when_routing_cost_money(self):
        """Use the new signature that accepts routing_overhead_usd."""
        from llm_router.cost import calc_savings
        # 100 tokens on Haiku vs Sonnet baseline saves very little.
        # If routing_overhead is large, realized is negative.
        cost_saved, _time = calc_savings(
            "haiku", tokens_used=100,
            task_type="query", complexity="simple",
            routing_overhead_usd=0.01,
        )
        # cost_saved is now the NET (gross - overhead), can be negative
        # Haiku 100 tokens vs Haiku baseline = $0 saved gross.
        # Net = 0 - 0.01 = -0.01.
        assert cost_saved < 0

    def test_savings_positive_when_overhead_small(self):
        from llm_router.cost import calc_savings
        # Haiku-handling Opus-baseline task → big gross savings, small overhead
        cost_saved, _ = calc_savings(
            "haiku", tokens_used=10_000,
            task_type="code", complexity="complex",
            routing_overhead_usd=0.0001,
        )
        assert cost_saved > 0

    @pytest.mark.asyncio
    async def test_get_realized_savings_returns_gross_overhead_net(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        # Log a call with overhead
        await log_claude_usage(
            model="haiku", tokens_used=1000, complexity="simple",
            input_tokens=500, output_tokens=500,
            routing_overhead_usd=0.002,
        )
        from llm_router.cost import get_realized_savings
        result = await get_realized_savings(period="all")
        assert "gross_saved_usd" in result
        assert "routing_overhead_usd" in result
        assert "realized_saved_usd" in result
        # realized = gross - overhead
        assert result["realized_saved_usd"] == pytest.approx(
            result["gross_saved_usd"] - result["routing_overhead_usd"]
        )


class TestAnthropicResponseFieldsExist:
    """Fix #4 — LLMResponse needs cache token fields so the Anthropic API parser
    has somewhere to put them. Caller in router.py then forwards to log_claude_usage."""

    def test_llm_response_has_cache_token_fields(self):
        from llm_router.types import LLMResponse
        # Construct a response with cache fields — must not raise
        r = LLMResponse(
            model="sonnet",
            provider="anthropic",
            content="hi",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
            latency_ms=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=1000,
        )
        assert r.cache_creation_input_tokens == 200
        assert r.cache_read_input_tokens == 1000

    def test_llm_response_cache_fields_default_to_zero(self):
        """Backward compat — old code creating LLMResponse without cache args still works."""
        from llm_router.types import LLMResponse
        r = LLMResponse(
            model="haiku", provider="anthropic", content="x",
            input_tokens=10, output_tokens=5, cost_usd=0.0001, latency_ms=100,
        )
        assert r.cache_creation_input_tokens == 0
        assert r.cache_read_input_tokens == 0


# ─────────────────────────────────────────────────────────────────────────────
# v9.3.0 — Codex CLI parallel cost tracking
# ─────────────────────────────────────────────────────────────────────────────


class TestCodexCost:
    def test_codex_cost_input_output_only(self):
        from llm_router.cost import _codex_cost
        # gpt-5.4: 1000 input × $5/Mtok + 500 output × $20/Mtok
        # = (5000 + 10000) / 1_000_000 = $0.015
        cost = _codex_cost("gpt-5.4", input_t=1000, output_t=500)
        assert abs(cost - 0.015) < 1e-6

    def test_codex_cost_with_cache_read(self):
        from llm_router.cost import _codex_cost
        # 10_000 cache_read on gpt-5.4 = 10_000 × $1.25/Mtok = $0.0125
        cost = _codex_cost("gpt-5.4", input_t=0, output_t=0, cache_read_t=10_000)
        assert abs(cost - 0.0125) < 1e-6

    def test_codex_cost_unknown_model_zero(self):
        from llm_router.cost import _codex_cost
        assert _codex_cost("nonexistent-gpt", input_t=1000, output_t=500) == 0.0

    def test_codex_baseline_simple_query_is_gpt5_mini(self):
        from llm_router.cost import _get_codex_baseline_for_task
        assert _get_codex_baseline_for_task("query", "simple") == "gpt-5-mini"

    def test_codex_baseline_code_moderate_is_gpt5_4(self):
        from llm_router.cost import _get_codex_baseline_for_task
        assert _get_codex_baseline_for_task("code", "moderate") == "gpt-5.4"

    def test_codex_baseline_complex_is_o3(self):
        from llm_router.cost import _get_codex_baseline_for_task
        assert _get_codex_baseline_for_task("code", "complex") == "o3"
        assert _get_codex_baseline_for_task("research", "simple") == "o3"

    @pytest.mark.asyncio
    async def test_log_codex_usage_persists_4_components(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import log_codex_usage
        await log_codex_usage(
            model="gpt-5-mini",
            tokens_used=0,
            complexity="simple",
            task_type="query",
            input_tokens=100,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=5000,
        )
        import aiosqlite
        async with aiosqlite.connect(tmp_path / "test.db") as db:
            cursor = await db.execute(
                "SELECT model, input_tokens, output_tokens, cache_read_input_tokens "
                "FROM codex_usage ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        assert row == ("gpt-5-mini", 100, 200, 5000)

    @pytest.mark.asyncio
    async def test_log_codex_usage_returns_savings_dict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import log_codex_usage
        # gpt-5-mini for a query task (baseline=gpt-5-mini per task-aware) → 0 savings
        # Use o3 baseline (complex) for actual savings demonstration
        result = await log_codex_usage(
            "gpt-5-mini", tokens_used=0, complexity="complex",
            task_type="code", input_tokens=1000, output_tokens=500,
        )
        assert "cost_saved_usd" in result
        assert "time_saved_sec" in result
        # o3 baseline ($15/$60) vs gpt-5-mini ($0.40/$2) → big positive savings
        assert result["cost_saved_usd"] > 0


class TestDualPlatformRealizedSavings:
    @pytest.mark.asyncio
    async def test_realized_savings_all_sums_both_tables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import log_claude_usage, log_codex_usage, get_realized_savings
        await log_claude_usage(
            "haiku", tokens_used=1000, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        await log_codex_usage(
            "gpt-5-mini", tokens_used=1000, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        result = await get_realized_savings(period="all", platform="all")
        assert "by_platform" in result
        assert result["by_platform"]["claude"]["gross_saved_usd"] > 0
        assert result["by_platform"]["codex"]["gross_saved_usd"] > 0
        # Combined ≈ sum
        assert result["gross_saved_usd"] == pytest.approx(
            result["by_platform"]["claude"]["gross_saved_usd"]
            + result["by_platform"]["codex"]["gross_saved_usd"]
        )

    @pytest.mark.asyncio
    async def test_realized_savings_codex_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import log_codex_usage, get_realized_savings
        await log_codex_usage(
            "gpt-5-mini", tokens_used=1000, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        result = await get_realized_savings(period="all", platform="codex")
        assert "by_platform" not in result  # single-platform doesn't include breakdown
        assert result["gross_saved_usd"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# v9.3.1 — Gemini CLI parallel cost tracking
# ─────────────────────────────────────────────────────────────────────────────


class TestGeminiCost:
    def test_gemini_cost_input_output_only(self):
        from llm_router.cost import _gemini_cost
        # gemini-2.5-flash: 1000 input × $0.30/Mtok + 500 output × $2.50/Mtok
        # = (300 + 1250) / 1_000_000 = $0.00155
        cost = _gemini_cost("gemini-2.5-flash", input_t=1000, output_t=500)
        assert abs(cost - 0.00155) < 1e-7

    def test_gemini_cost_with_cache_read(self):
        from llm_router.cost import _gemini_cost
        # 10_000 cache_read on gemini-2.5-pro = 10_000 × $0.31/Mtok = $0.0031
        cost = _gemini_cost("gemini-2.5-pro", input_t=0, output_t=0, cache_read_t=10_000)
        assert abs(cost - 0.0031) < 1e-6

    def test_gemini_cost_unknown_model_zero(self):
        from llm_router.cost import _gemini_cost
        assert _gemini_cost("nonexistent-gemini", input_t=1000, output_t=500) == 0.0

    def test_gemini_baseline_simple_query_is_flash(self):
        from llm_router.cost import _get_gemini_baseline_for_task
        assert _get_gemini_baseline_for_task("query", "simple") == "gemini-2.0-flash"

    def test_gemini_baseline_moderate_code_is_25_flash(self):
        from llm_router.cost import _get_gemini_baseline_for_task
        assert _get_gemini_baseline_for_task("code", "moderate") == "gemini-2.5-flash"

    def test_gemini_baseline_complex_is_pro(self):
        from llm_router.cost import _get_gemini_baseline_for_task
        assert _get_gemini_baseline_for_task("code", "complex") == "gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_log_gemini_usage_persists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import log_gemini_usage
        await log_gemini_usage(
            model="gemini-2.5-flash", tokens_used=0, complexity="moderate",
            task_type="code", input_tokens=100, output_tokens=200,
            cache_read_input_tokens=5000,
        )
        import aiosqlite
        async with aiosqlite.connect(tmp_path / "test.db") as db:
            cursor = await db.execute(
                "SELECT model, input_tokens, output_tokens, cache_read_input_tokens "
                "FROM gemini_usage ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        assert row == ("gemini-2.5-flash", 100, 200, 5000)


class TestTriPlatformRealizedSavings:
    @pytest.mark.asyncio
    async def test_realized_savings_all_sums_three_tables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import (
            log_claude_usage, log_codex_usage, log_gemini_usage, get_realized_savings,
        )
        # Use complex task so each platform's cheap-model-vs-flagship-baseline has savings
        await log_claude_usage(
            "haiku", tokens_used=0, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        await log_codex_usage(
            "gpt-5-mini", tokens_used=0, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        await log_gemini_usage(
            "gemini-2.0-flash", tokens_used=0, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        result = await get_realized_savings(period="all", platform="all")
        assert "by_platform" in result
        bp = result["by_platform"]
        assert set(bp.keys()) == {"claude", "codex", "gemini"}
        assert bp["claude"]["gross_saved_usd"] > 0
        assert bp["codex"]["gross_saved_usd"] > 0
        assert bp["gemini"]["gross_saved_usd"] > 0
        assert result["gross_saved_usd"] == pytest.approx(
            bp["claude"]["gross_saved_usd"]
            + bp["codex"]["gross_saved_usd"]
            + bp["gemini"]["gross_saved_usd"]
        )

    @pytest.mark.asyncio
    async def test_realized_savings_gemini_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.cost import log_gemini_usage, get_realized_savings
        await log_gemini_usage(
            "gemini-2.0-flash", tokens_used=0, complexity="complex",
            task_type="code", input_tokens=500, output_tokens=500,
        )
        result = await get_realized_savings(period="all", platform="gemini")
        assert "by_platform" not in result
        assert result["gross_saved_usd"] > 0


class TestGeminiPlatformDetection:
    """Sanity check that _is_gemini_session catches Gemini model prefixes."""
    def test_gemini_25_pro_detected(self):
        import importlib.util

        from tests.conftest import get_hook_path

        spec = importlib.util.spec_from_file_location(
            "_ar", str(get_hook_path("auto-route.py"))
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert m._is_gemini_session({"model": "gemini-2.5-pro"})
        assert m._is_gemini_session({"model": "gemini-2.0-flash"})
        assert not m._is_gemini_session({"model": "claude-sonnet-4-6"})
        assert not m._is_gemini_session({"model": "gpt-5.5"})
        assert not m._is_gemini_session({"model": ""})
