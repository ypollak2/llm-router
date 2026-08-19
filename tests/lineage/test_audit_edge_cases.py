"""Comprehensive edge case and stress tests for lineage tracking system.

Tests boundaries, error conditions, data integrity, and stress scenarios.
Part of the full llm_router audit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
import json

import pytest

from llm_router.lineage import decision_logger, log_routing_decision
from llm_router.lineage.lineage_store import LineageStore
from llm_router.lineage.lineage_query import LineageQuery



class TestEmptySession:
    """Test session with no routing decisions."""

    def test_empty_session_report_graceful(self):
        """Session with zero decisions should handle gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            query = LineageQuery(store=store)
            recent = query.get_recent(limit=10)

            assert len(recent) == 0, "Empty session should have no decisions"

            metrics = query.get_token_usage_by_model()
            assert metrics == {}, "Empty session should return empty metrics"


class TestLargeSession:
    """Test session with many routing decisions."""

    def test_large_session_100_decisions(self):
        """Verify system handles 100+ decisions without degradation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            models = ["gemini-2.5-flash", "claude-sonnet-4-6", "claude-haiku-4-5"]
            operations = ["llm_query", "llm_code", "llm_analyze", "llm_research"]

            # Log 100 decisions
            for i in range(100):
                model_idx = i % len(models)
                op_idx = i % len(operations)

                log_routing_decision(
                    operation=operations[op_idx],
                    classification="query/simple",
                    selected_model=models[model_idx],
                    selection_reason="router_picked",
                    input_tokens=50 + i,
                    output_tokens=30 + i,
                    cost_usd=0.0001 * (i % 10),
                    request_id=f"req-{i // 10}",  # Group into 10 requests
                )

            # Verify all decisions logged
            query = LineageQuery(store=store)
            recent = query.get_recent(limit=200)
            assert len(recent) == 100, "All 100 decisions should be logged"

            # Verify aggregates work at scale
            metrics = query.get_token_usage_by_model()
            assert len(metrics) == 3, "Should have 3 unique models"
            total_tokens = sum(v["tokens"] for v in metrics.values())
            assert total_tokens > 0, "Token totals should be positive"


class TestMalformedData:
    """Test handling of corrupt or malformed data."""

    @pytest.mark.xfail(reason="WIP lineage suite: audit edge-case behaviour not yet stabilized", strict=False)
    def test_corrupt_jsonl_line(self):
        """System should skip corrupt JSONL lines gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Add valid decision
            log_routing_decision(
                operation="llm_query",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
            )

            # Add corrupt line directly to JSONL
            with open(store.jsonl_file, "a") as f:
                f.write("{corrupted json data\n")

            # Add another valid decision
            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
            )

            # Query should handle gracefully
            query = LineageQuery(store=store)
            recent = query.get_recent(limit=10)

            # Should have 2 valid decisions despite the corrupt line
            assert len(recent) >= 2, "Should retrieve valid decisions despite corruption"


class TestConcurrentAccess:
    """Test concurrent JSONL writes."""

    def test_multiple_sequential_writes(self):
        """Verify sequential writes don't corrupt JSONL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Simulate rapid sequential writes
            for i in range(50):
                log_routing_decision(
                    operation=f"op_{i}",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                    request_id=f"req-rapid-{i}",
                )

            # Verify all lines are valid JSON
            valid_count = 0
            with open(store.jsonl_file) as f:
                for line in f:
                    try:
                        json.loads(line)
                        valid_count += 1
                    except json.JSONDecodeError:
                        pass

            assert valid_count == 50, "All lines should be valid JSON"


class TestDataConsistency:
    """Test JSONL ↔ SQLite consistency."""

    def test_jsonl_sqlite_parity(self):
        """JSONL and SQLite should have same decision count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Log 10 decisions
            for i in range(10):
                log_routing_decision(
                    operation=f"op_{i}",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                )

            # Count JSONL lines
            jsonl_count = 0
            with open(store.jsonl_file) as f:
                jsonl_count = sum(1 for line in f)

            # Count SQLite rows
            query = LineageQuery(store=store)
            recent = query.get_recent(limit=100)
            sqlite_count = len(recent)

            assert jsonl_count == sqlite_count == 10, "JSONL and SQLite should match"


class TestTokenCalculations:
    """Test accuracy of token and cost calculations."""

    def test_token_sum_accuracy(self):
        """Token totals should match sum of individual decisions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            decisions = [
                (50, 30, 0.0001),
                (100, 50, 0.0002),
                (200, 100, 0.0003),
                (500, 250, 0.0005),
            ]

            for in_tok, out_tok, cost in decisions:
                log_routing_decision(
                    operation="llm_query",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost_usd=cost,
                )

            query = LineageQuery(store=store)
            metrics = query.get_token_usage_by_model()
            gemini_stats = metrics.get("gemini-2.5-flash", {})

            expected_tokens = sum(in_tok + out_tok for in_tok, out_tok, _ in decisions)
            actual_tokens = gemini_stats.get("tokens", 0)

            assert actual_tokens == expected_tokens, "Token count must match"


class TestFallbackChainTracking:
    """Test fallback chain recording and retrieval."""

    def test_fallback_chain_preserved(self):
        """Fallback chains should be recorded and queryable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            log_routing_decision(
                operation="llm_research",
                classification="research/moderate",
                selected_model="gemini-2.5-flash",
                selection_reason="fallback_after_timeout",
                fallback_chain=["ollama", "codex"],
                fallback_reason="ollama_timeout_exceeded",
                input_tokens=200,
                output_tokens=150,
                cost_usd=0.00015,
            )

            query = LineageQuery(store=store)
            recent = query.get_recent(limit=1)

            assert len(recent) == 1
            decision = recent[0]
            assert decision.get("fallback_chain") == ["ollama", "codex"]
            assert "ollama_timeout" in decision.get("fallback_reason", "")


class TestWastefulOperationDetection:
    """Test waste detection accuracy."""

    def test_waste_detection_accuracy(self):
        """Expensive models on simple operations should be flagged as waste."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            # Appropriate: simple query on cheap model
            log_routing_decision(
                operation="get_status",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
                input_tokens=50,
                output_tokens=30,
                cost_usd=0.00006,
            )

            # Wasteful: simple query on expensive model
            log_routing_decision(
                operation="get_config",
                classification="query/simple",
                selected_model="claude-opus-4-7",
                selection_reason="manual_override",
                input_tokens=50,
                output_tokens=30,
                cost_usd=0.003,
            )

            query = LineageQuery(store=store)
            wasteful = query.find_wasteful_operations(
                expensive_models=["claude-opus-4-7", "claude-sonnet-4-6"]
            )

            assert len(wasteful) == 1, "Should detect 1 wasteful operation"
            assert wasteful[0]["operation"] == "get_config"


class TestDatabaseRecovery:
    """Test database integrity and recovery."""

    def test_corrupted_db_recovery(self):
        """System should handle corrupted SQLite gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)

            # Create normal decision
            decision_logger._store = store
            log_routing_decision(
                operation="llm_query",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
            )

            # Corrupt the database by truncating it
            db_path = Path(store.db_file)
            with open(db_path, "r+b") as f:
                f.truncate(100)  # Truncate to invalid state

            # Attempt to query should fail gracefully
            query = LineageQuery(store=store)
            try:
                recent = query.get_recent(limit=10)
                # If it doesn't raise, it should return empty list
                assert isinstance(recent, list)
            except Exception as e:
                # Some SQL error is expected, should be specific
                assert "database" in str(e).lower() or "sql" in str(e).lower()


class TestErrorHandling:
    """Test graceful error handling."""

    def test_missing_lineage_dir(self):
        """Should create missing directories automatically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent" / "dir" / "lineage"
            store = LineageStore(router_dir=str(nonexistent))

            # Should auto-create directories
            assert store.db_file

            decision_logger._store = store
            log_routing_decision(
                operation="llm_query",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
            )

            assert Path(store.db_file).parent.exists()


class TestRequestIDLinkage:
    """Test request ID and decision chain linkage."""

    def test_request_id_grouping(self):
        """Decisions with same request_id should be grouped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)
            decision_logger._store = store

            request_id = "user-session-123"

            # Log 3 decisions for same request
            for i in range(3):
                log_routing_decision(
                    operation=f"op_{i}",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                    request_id=request_id,
                )

            query = LineageQuery(store=store)
            recent = query.get_recent(limit=10)

            same_request = [d for d in recent if d.get("request_id") == request_id]
            assert len(same_request) == 3, "All 3 decisions should share request_id"
