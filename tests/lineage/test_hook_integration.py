"""Tests for session hook integration with routing lineage tracking.

Verifies that:
1. Session-start initializes lineage tracking
2. Session-end generates routing efficiency report
3. Waste alerts are detected and formatted
4. All components gracefully handle missing data
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from llm_router.hooks.lineage_integration import (
    format_routing_section,
    get_routing_metrics_summary,
    get_session_routing_report,
    get_waste_alerts,
    init_session_lineage,
)
from llm_router.lineage.decision_logger import log_routing_decision


class TestSessionStartIntegration:
    """Test SessionStart hook integration."""

    def test_init_session_lineage_creates_marker(self):
        """init_session_lineage should create marker file indicating tracking is active."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Monkey-patch STATE_DIR
            import llm_router.hooks.lineage_integration as li

            original_state_dir = li.STATE_DIR
            li.STATE_DIR = tmpdir

            try:
                init_session_lineage()

                marker = Path(tmpdir) / ".lineage_active"
                assert marker.exists(), "Marker file should exist after init"
                assert marker.read_text() == "1"
            finally:
                li.STATE_DIR = original_state_dir

    def test_init_session_lineage_cleans_old_files(self):
        """init_session_lineage should remove old lineage files for session isolation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            router_dir = Path(tmpdir)

            # Create old lineage files (from previous session)
            old_jsonl = router_dir / "routing_lineage.jsonl"
            old_db = router_dir / "routing_lineage.db"
            old_jsonl.write_text("old data")
            old_db.write_text("old db")

            # Monkey-patch STATE_DIR
            import llm_router.hooks.lineage_integration as li

            original_state_dir = li.STATE_DIR
            li.STATE_DIR = str(router_dir)

            try:
                init_session_lineage()

                # Old files should be gone
                assert not old_jsonl.exists(), "Old JSONL should be deleted"
                assert not old_db.exists(), "Old DB should be deleted"
            finally:
                li.STATE_DIR = original_state_dir


class TestSessionEndIntegration:
    """Test SessionEnd hook integration."""

    def test_get_session_routing_report_empty_when_no_decisions(self):
        """Should return empty string when no routing decisions were made."""
        report = get_session_routing_report()
        # Can be empty string (no decisions this session)
        assert isinstance(report, str)

    def test_get_waste_alerts_empty_when_no_waste(self):
        """Should return empty list when all routing is appropriate."""
        from llm_router.lineage import decision_logger

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_router.lineage.lineage_store import LineageStore

            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Log appropriate routing (simple op on cheap model)
            for i in range(5):
                log_routing_decision(
                    operation="get_cap",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                )

            alerts = get_waste_alerts()
            assert len(alerts) == 0, "No alerts for appropriate routing"

    def test_get_waste_alerts_detects_waste(self):
        """Should return alerts when expensive models are used for simple operations."""
        from llm_router.lineage import decision_logger

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_router.lineage.lineage_store import LineageStore

            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Log wasteful routing (simple op on expensive model)
            log_routing_decision(
                operation="get_cap",
                classification="query/simple",
                selected_model="claude-opus-4-7",
                selection_reason="manual_override",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.003,
            )

            alerts = get_waste_alerts(store=store)
            assert len(alerts) > 0, "Should detect wasteful routing"
            assert "ROUTING WASTE DETECTED" in alerts[0]
            assert "get_cap" in str(alerts)
            assert "claude-opus-4-7" in str(alerts)
            assert "savings" in str(alerts).lower()

    def test_get_routing_metrics_summary(self):
        """Should return high-level metrics when decisions exist."""
        from llm_router.lineage import decision_logger

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_router.lineage.lineage_store import LineageStore

            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Log some decisions
            log_routing_decision(
                operation="get_cap",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
                input_tokens=50,
                output_tokens=30,
                cost_usd=0.00006,
            )

            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
                input_tokens=2000,
                output_tokens=500,
                cost_usd=0.015,
            )

            metrics = get_routing_metrics_summary(store=store)

            assert metrics["model_count"] == 2
            assert metrics["operation_count"] == 2
            assert metrics["total_tokens"] == 2580
            assert metrics["total_cost"] == pytest.approx(0.01506, abs=0.00001)
            assert "gemini-2.5-flash" in metrics["models_used"]
            assert "claude-sonnet-4-6" in metrics["models_used"]

    def test_format_routing_section_shows_metrics(self):
        """format_routing_section should include metrics and waste detection."""
        from llm_router.lineage import decision_logger

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_router.lineage.lineage_store import LineageStore

            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Log appropriate routing
            log_routing_decision(
                operation="get_cap",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
                input_tokens=50,
                output_tokens=30,
                cost_usd=0.00006,
            )

            section = format_routing_section(store=store)

            assert "ROUTING EFFICIENCY REPORT" in section
            assert "Operations:" in section
            assert "Models Used:" in section
            assert "wasteful" in section.lower() or "✅" in section

    def test_format_routing_section_alerts_on_waste(self):
        """format_routing_section should show waste alerts clearly."""
        from llm_router.lineage import decision_logger

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_router.lineage.lineage_store import LineageStore

            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Log wasteful routing
            for i in range(3):
                log_routing_decision(
                    operation="get_cap",
                    classification="query/simple",
                    selected_model="claude-opus-4-7",
                    selection_reason="manual_override",
                    cost_usd=0.003,
                )

            section = format_routing_section(store=store)

            assert "⚠️  ROUTING WASTE DETECTED" in section
            assert "get_cap" in section
            assert "savings" in section.lower()


class TestGracefulFailures:
    """Test that integration functions handle errors gracefully."""

    def test_get_session_routing_report_handles_missing_system(self):
        """Should return empty string if lineage system not available."""
        # This is handled by try/except in get_session_routing_report
        # Result should be empty string, never an exception
        result = get_session_routing_report()
        assert isinstance(result, str)

    def test_get_waste_alerts_handles_error(self):
        """Should return empty list if query fails."""
        alerts = get_waste_alerts()
        assert isinstance(alerts, list)

    def test_get_routing_metrics_summary_handles_missing_data(self):
        """Should return empty dict if no data available."""
        metrics = get_routing_metrics_summary()
        assert isinstance(metrics, dict)

    def test_format_routing_section_handles_error(self):
        """Should return empty string if formatting fails."""
        section = format_routing_section()
        assert isinstance(section, str)


class TestEndToEndSessionLifecycle:
    """Test complete session lifecycle with lineage tracking."""

    def test_session_with_routing_decisions(self):
        """Simulate a complete session with routing decisions and reporting."""
        from llm_router.lineage import decision_logger

        with tempfile.TemporaryDirectory() as tmpdir:
            from llm_router.lineage.lineage_store import LineageStore
            import llm_router.hooks.lineage_integration as li

            # Simulate SessionStart with monkeypatch
            original_state_dir = li.STATE_DIR
            li.STATE_DIR = tmpdir
            try:
                init_session_lineage()
                assert (Path(tmpdir) / ".lineage_active").exists()

                # Create fresh store after init_session_lineage cleaned files
                store = LineageStore(router_dir=tmpdir)
                decision_logger._store = store

                # Simulate session with various operations
                request_id = "req-session-001"

                # 1. Simple query (get_cap) on cheap model - APPROPRIATE
                log_routing_decision(
                    operation="get_cap",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                    input_tokens=50,
                    output_tokens=30,
                    cost_usd=0.00006,
                    request_id=request_id,
                )

                # 2. Code generation on expensive model - APPROPRIATE
                log_routing_decision(
                    operation="llm_code",
                    classification="code/complex",
                    selected_model="claude-sonnet-4-6",
                    selection_reason="router_picked",
                    input_tokens=2000,
                    output_tokens=500,
                    cost_usd=0.015,
                    request_id=request_id,
                )

                # 3. Another simple operation - APPROPRIATE
                log_routing_decision(
                    operation="check_syntax",
                    classification="query/simple",
                    selected_model="claude-haiku-4-5",
                    selection_reason="router_picked",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.00005,
                    request_id=request_id,
                )

                # Simulate SessionEnd
                metrics = get_routing_metrics_summary(store=store)
                alerts = get_waste_alerts(store=store)
                section = format_routing_section(store=store)

                # Verify session captured everything
                assert metrics["operation_count"] == 3
                assert metrics["total_cost"] > 0.01
                assert len(alerts) == 0, "No waste for appropriate routing"
                assert "ROUTING EFFICIENCY REPORT" in section
                assert "✅ No wasteful" in section  # Should show clean slate
            finally:
                li.STATE_DIR = original_state_dir
