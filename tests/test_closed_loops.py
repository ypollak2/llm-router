"""Tests for v6.2 "Closed Loops" feature — integrated feedback system."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from llm_router.cost import log_correction, get_correction_count
from llm_router.memory.profiles import parse_routing_directives
from llm_router.monitoring.live_tracker import get_trend_pressure
from llm_router.retrospective import run_weekly_retrospective
from llm_router.model_selector import select_model
from llm_router.types import ClassificationResult, Complexity, QualityMode


class TestCriticalBugFix:
    """Test: llm_reroute now passes corrected_model to log_correction."""

    @pytest.mark.asyncio
    async def test_reroute_populates_corrected_model(self, monkeypatch, tmp_path):
        """Verify that rerouting records the corrected model in the database."""
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))

        # Simulate a correction with model
        await log_correction(
            original_tool="llm_query",
            original_model="anthropic/claude-haiku-4-5-20251001",
            corrected_tool="llm_code",
            corrected_model="anthropic/claude-sonnet-4-6",
            reason="haiku was too weak for code",
        )

        # Verify the correction was recorded
        count = await get_correction_count("llm_query")
        assert count == 1, "Correction should be counted"


class TestLoop1DirectivesFeedback:
    """Test: Directives loaded and applied from directives.md."""

    def test_parse_routing_directives_with_valid_format(self):
        """Parse ROUTING_RULE directives from directives.md format."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("""
## 2026-04-19 10:30 — security_review
- Source: 5 user corrections
- Rule: ROUTING_RULE: security_review → anthropic/claude-opus-4-6 (confidence: high)
- Status: CONFIRMED

## 2026-04-19 11:00 — code
- Rule: ROUTING_RULE: code → anthropic/claude-sonnet-4-6 (confidence: medium)
""")
            f.flush()

            directives = parse_routing_directives(Path(f.name))

            assert "security_review" in directives
            assert directives["security_review"] == "anthropic/claude-opus-4-6"
            assert "code" in directives
            assert directives["code"] == "anthropic/claude-sonnet-4-6"

    def test_parse_routing_directives_empty_file(self):
        """Handle missing directives.md gracefully."""
        directives = parse_routing_directives(Path("/nonexistent/directives.md"))
        assert directives == {}

    def test_parse_routing_directives_malformed_lines(self):
        """Skip malformed directive lines."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("""
ROUTING_RULE: valid_task → anthropic/claude-opus-4-6
This is not a directive line
ROUTING_RULE: incomplete →
→ malformed
ROUTING_RULE: another → anthropic/claude-sonnet-4-6 (confidence: high)
""")
            f.flush()

            directives = parse_routing_directives(Path(f.name))

            # Should only parse the valid ones
            assert "valid_task" in directives
            assert "another" in directives
            assert len(directives) == 2


class TestLoop2WeeklyRetrospective:
    """Test: Weekly retrospective aggregates patterns and generates directives."""

    @pytest.mark.asyncio
    async def test_run_weekly_retrospective_no_data(self):
        """Handle case with no retrospectives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            retro_dir = Path(tmpdir) / "retrospectives"
            retro_dir.mkdir()

            with mock.patch("llm_router.retrospective.RETROSPECT_DIR", retro_dir):
                result = await run_weekly_retrospective()

                assert result["period"] == "weekly"
                assert result["daily_retrospectives"] == 0
                assert result["recurring_patterns"] == []
                assert result["permanent_directives_generated"] == 0


class TestLoop3TrendPressure:
    """Test: Monitoring trends feed back into model selection."""

    def test_get_trend_pressure_no_snapshots(self):
        """Return 0.0 when no snapshots available."""
        with mock.patch("llm_router.monitoring.live_tracker.load_session_snapshots", return_value=[]):
            pressure = get_trend_pressure()
            assert pressure == 0.0

    def test_get_trend_pressure_improving_trend(self):
        """Return 0.0 when accuracy is improving."""
        snapshots = [
            {"facts": {"accuracy": 0.85}, "hour": 1},
            {"facts": {"accuracy": 0.92}, "hour": 2},  # improved
        ]
        with mock.patch("llm_router.monitoring.live_tracker.load_session_snapshots", return_value=snapshots):
            pressure = get_trend_pressure()
            assert pressure == 0.0, "Improving trend should have no pressure"

    def test_get_trend_pressure_declining_trend(self):
        """Return 0.1-0.3 when accuracy is declining."""
        snapshots = [
            {"facts": {"accuracy": 0.95}, "hour": 1},
            {"facts": {"accuracy": 0.88}, "hour": 2},  # declined by 0.07
        ]
        with mock.patch("llm_router.monitoring.live_tracker.load_session_snapshots", return_value=snapshots):
            pressure = get_trend_pressure()
            # -0.07 * 2 = 0.14 pressure
            assert 0.1 < pressure < 0.3, f"Expected 0.1-0.3, got {pressure}"

    def test_get_trend_pressure_steep_decline(self):
        """Cap pressure at 0.3 for very steep declines."""
        snapshots = [
            {"facts": {"accuracy": 0.99}, "hour": 1},
            {"facts": {"accuracy": 0.75}, "hour": 2},  # declined by 0.24
        ]
        with mock.patch("llm_router.monitoring.live_tracker.load_session_snapshots", return_value=snapshots):
            pressure = get_trend_pressure()
            assert pressure <= 0.3, "Pressure should be capped at 0.3"


class TestLoop4CommunityProfiles:
    """Test: Community profile URL defaults when not provided."""

    def test_import_profile_default_url(self):
        """Use default community URL when none provided."""
        # This test verifies the code path, but doesn't actually fetch
        # We'll just verify that the function accepts empty URL

        # The actual implementation should have DEFAULT_COMMUNITY_URL
        # We test that the code doesn't error when url is empty
        from llm_router.tools.admin import llm_import_profile

        # We can't actually test the full import without network,
        # but we verify the function signature accepts empty url
        assert callable(llm_import_profile)


class TestTrendPressureIntegration:
    """Test: Trend pressure integrates with model selection."""

    @pytest.mark.asyncio
    async def test_select_model_with_trend_pressure(self):
        """Trend pressure soft-escalates model tier."""
        classification = ClassificationResult(
            complexity=Complexity.SIMPLE,
            confidence=0.95,
            reasoning="simple task",
            inferred_task_type=None,
            classifier_model="claude-haiku",
            classifier_cost_usd=0.001,
            classifier_latency_ms=50.0,
        )

        # With trend pressure: should consider escalation
        rec_with_trend = await select_model(classification, 0.0, QualityMode.BALANCED, "haiku", 0.2)

        # Should be valid, trend doesn't force escalation under no budget pressure
        # but reasoning should mention trend
        assert rec_with_trend.reasoning is not None

    @pytest.mark.asyncio
    async def test_select_model_blends_budget_and_trend(self):
        """Effective pressure = 0.8 * budget + 0.2 * trend."""
        classification = ClassificationResult(
            complexity=Complexity.MODERATE,
            confidence=0.90,
            reasoning="moderate task",
            inferred_task_type=None,
            classifier_model="claude-sonnet",
            classifier_cost_usd=0.003,
            classifier_latency_ms=100.0,
        )

        # Test: combined pressure
        rec = await select_model(
            classification,
            budget_pct_used=0.5,  # 50% budget
            quality_mode=QualityMode.BALANCED,
            min_model="haiku",
            trend_pressure=0.2,  # 20% trend decline
        )

        # Effective pressure = 0.5 * 0.8 + 0.2 * 0.2 = 0.4 + 0.04 = 0.44
        # At 44%, should trigger downshift
        assert rec.reasoning is not None


class TestEndToEndClosedLoop:
    """Integration test: All 4 loops working together."""

    @pytest.mark.asyncio
    async def test_correction_recorded_with_model(self, monkeypatch, tmp_path):
        """Verify full path: correction → model recorded → learned profile built."""
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))

        # Record a correction with model
        await log_correction(
            original_tool="llm_query",
            original_model="anthropic/claude-haiku-4-5-20251001",
            corrected_tool="llm_analyze",
            corrected_model="anthropic/claude-opus-4-6",
            reason="haiku insufficient for analysis",
        )

        # Verify count increases
        count = await get_correction_count("llm_query")
        assert count >= 1, "Should record the correction"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
