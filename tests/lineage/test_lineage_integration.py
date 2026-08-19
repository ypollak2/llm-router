"""End-to-end integration tests for routing lineage tracking.

Demonstrates realistic usage patterns and validates the complete flow
of capturing, storing, querying, and reporting routing decisions.
"""

from __future__ import annotations

import tempfile


from llm_router.lineage.decision_logger import log_routing_decision
from llm_router.lineage.lineage_query import LineageQuery
from llm_router.lineage.lineage_store import LineageStore
from llm_router.lineage.report_generator import generate_routing_report


class TestEndToEndLineageTracking:
    """End-to-end scenarios simulating real LLM Router usage."""

    def test_realistic_session_with_mixed_operations(self):
        """Simulate a realistic session with various operations and routing decisions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)

            # Import fresh to use isolated store
            from llm_router.lineage import decision_logger

            decision_logger._store = store

            # Session 1: User does various operations
            session_id = "session-2024-001"

            # User calls `llm_router budget --set openai 50` (simple query)
            log_routing_decision(
                operation="get_caps",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="router_picked",
                input_tokens=45,
                output_tokens=25,
                cost_usd=0.00008,
                latency_ms=120.5,
                request_id=session_id,
            )

            # User calls `llm_router set-enforce` (moderate analysis)
            parent_enforce = log_routing_decision(
                operation="validate_config",
                classification="analyze/moderate",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
                input_tokens=800,
                output_tokens=200,
                cost_usd=0.008,
                latency_ms=1240.0,
                request_id=session_id,
            )

            # Nested: Check syntax (simple)
            log_routing_decision(
                operation="check_syntax",
                classification="query/simple",
                selected_model="claude-haiku-4-5",
                selection_reason="nested_simple",
                input_tokens=150,
                output_tokens=50,
                cost_usd=0.00005,
                latency_ms=340.0,
                request_id=session_id,
                parent_decision_id=parent_enforce.decision_id,
            )

            # User runs code generation (complex)
            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
                input_tokens=2500,
                output_tokens=1000,
                cost_usd=0.025,
                latency_ms=3450.0,
                request_id=session_id,
            )

            # Fallback scenario: First try Ollama, fall back to Gemini
            log_routing_decision(
                operation="llm_research",
                classification="research/moderate",
                selected_model="gemini-2.5-flash",
                selection_reason="fallback_after_ollama_timeout",
                fallback_chain=["ollama"],
                fallback_reason="ollama_timeout",
                input_tokens=1200,
                output_tokens=800,
                cost_usd=0.0008,
                latency_ms=4500.0,
                request_id=session_id,
            )

            # Check lineage data was stored correctly
            query = LineageQuery(store)
            recent = query.get_recent(limit=10)
            assert len(recent) == 5, "Should have 5 decisions logged"

            # Verify session tracing
            chain = query.trace_decision_chain(session_id)
            assert len(chain) == 5
            assert chain[0]["operation"] == "get_caps"
            assert chain[1]["operation"] == "validate_config"

            # Verify no wasteful operations (all appropriate)
            wasteful = query.find_wasteful_operations()
            assert len(wasteful) == 0, "No simple operations used expensive models"

            # Verify fallback tracking
            fallbacks = query.get_fallback_statistics()
            assert fallbacks["ollama_timeout"] == 1

            # Generate and validate report
            report = generate_routing_report(store)
            assert "LLM_ROUTER ROUTING DECISION LINEAGE REPORT" in report
            assert "get_caps" in report
            assert "llm_code" in report

    def test_detect_wasteful_patterns_across_sessions(self):
        """Verify ability to detect token waste patterns across multiple sessions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)

            from llm_router.lineage import decision_logger

            decision_logger._store = store

            # Session 1: Efficient usage (using cheap models for simple ops)
            for i in range(50):
                log_routing_decision(
                    operation="get_cap",
                    classification="query/simple",
                    selected_model="gemini-2.5-flash",
                    selection_reason="router_picked",
                    input_tokens=80,
                    output_tokens=40,
                    cost_usd=0.00006,
                )

            # Session 2: Wasteful pattern (accidentally using Opus for simple queries)
            for i in range(10):
                log_routing_decision(
                    operation="get_cap",
                    classification="query/simple",
                    selected_model="claude-opus-4-7",
                    selection_reason="manual_override",  # User made a mistake
                    input_tokens=80,
                    output_tokens=40,
                    cost_usd=0.003,  # Much more expensive
                )

            query = LineageQuery(store)
            wasteful = query.find_wasteful_operations()

            assert len(wasteful) == 10, "Should detect all 10 wasteful operations"
            assert all(w["selected_model"] == "claude-opus-4-7" for w in wasteful)

            # Calculate cost difference: 10 expensive opus calls vs the cheaper
            # haiku model that should have handled the same simple tasks.
            expensive_cost = 10 * 0.003
            wasted = expensive_cost - (10 * 0.00006)

            assert wasted > 0.02, f"Should detect significant waste: ${wasted:.4f}"

    def test_query_performance_optimization_metrics(self):
        """Verify metrics show where optimization should focus."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)

            from llm_router.lineage import decision_logger

            decision_logger._store = store

            # Log a diverse set of operations
            operations = [
                ("get_cap", "query/simple", "gemini-2.5-flash", 0.00006, 120),
                ("get_spend", "query/simple", "claude-haiku-4-5", 0.00003, 145),
                ("llm_code", "code/complex", "claude-sonnet-4-6", 0.015, 2340),
                ("llm_analyze", "analyze/moderate", "claude-sonnet-4-6", 0.008, 1200),
                ("llm_research", "research/moderate", "gemini-2.5-flash", 0.0008, 3400),
            ]

            for op, cls, model, cost, latency in operations:
                for i in range(5):
                    log_routing_decision(
                        operation=op,
                        classification=cls,
                        selected_model=model,
                        selection_reason="router_picked",
                        input_tokens=500 if "code" in cls else 200,
                        output_tokens=200 if "code" in cls else 100,
                        cost_usd=cost,
                        latency_ms=latency,
                    )

            query = LineageQuery(store)

            # Get model usage breakdown
            token_usage = query.get_token_usage_by_model()
            assert "gemini-2.5-flash" in token_usage
            assert "claude-sonnet-4-6" in token_usage
            assert "claude-haiku-4-5" in token_usage

            # Sonnet should be highest cost (complex work)
            sonnet_cost = token_usage["claude-sonnet-4-6"]["cost_usd"]
            haiku_cost = token_usage["claude-haiku-4-5"]["cost_usd"]
            assert sonnet_cost > haiku_cost, "Complex work (Sonnet) should cost more"

            # Get model distribution
            model_by_op = query.get_model_usage_by_operation()
            assert model_by_op["llm_code"]["claude-sonnet-4-6"] == 5
            assert model_by_op["get_spend"]["claude-haiku-4-5"] == 5

            # Get latency metrics
            latencies = query.get_average_latency_by_model()
            research_latency = latencies.get("gemini-2.5-flash", 0)
            assert research_latency > 1000, "Research (3400ms avg) should show high latency"

    def test_report_identifies_optimization_opportunities(self):
        """Verify report clearly shows where optimization can save money."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)

            from llm_router.lineage import decision_logger

            decision_logger._store = store

            # Create a scenario with clear optimization opportunity
            # Many simple queries using expensive Opus (waste)
            for i in range(20):
                log_routing_decision(
                    operation="get_cap",
                    classification="query/simple",
                    selected_model="claude-opus-4-7",
                    selection_reason="legacy_code",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.003,
                )

            # A few legitimate complex operations on Sonnet (appropriate)
            for i in range(3):
                log_routing_decision(
                    operation="llm_code",
                    classification="code/complex",
                    selected_model="claude-sonnet-4-6",
                    selection_reason="router_picked",
                    input_tokens=2000,
                    output_tokens=500,
                    cost_usd=0.015,
                )

            # And some efficient simple queries on Haiku
            for i in range(30):
                log_routing_decision(
                    operation="check_syntax",
                    classification="query/simple",
                    selected_model="claude-haiku-4-5",
                    selection_reason="router_picked",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.00003,
                )

            report = generate_routing_report(store)

            # Report should highlight the waste
            assert "WASTEFUL OPERATIONS" in report
            assert "get_cap" in report
            assert "claude-opus-4-7" in report

            # Verify cost breakdown is visible
            assert "$" in report  # Cost figures shown
            assert "tokens" in report.lower()  # Token usage shown

    def test_lineage_store_dual_write_consistency(self):
        """Verify JSONL and SQLite stores are consistent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LineageStore(router_dir=tmpdir)

            from llm_router.lineage import decision_logger

            decision_logger._store = store

            # Log some decisions
            for i in range(25):
                log_routing_decision(
                    operation=f"op_{i % 5}",
                    classification="query/simple",
                    selected_model="claude-haiku-4-5",
                    selection_reason="router_picked",
                )

            # Read from JSONL
            jsonl_records = store.query_jsonl(limit=100)
            assert len(jsonl_records) == 25

            # Read from SQLite
            db_records = store.query_db("SELECT * FROM routing_decisions")
            assert len(db_records) == 25

            # Both should have same operations
            jsonl_ops = sorted([r["operation"] for r in jsonl_records])
            db_ops = sorted([r["operation"] for r in db_records])
            assert jsonl_ops == db_ops

            # Verify recent queries are consistent
            recent_jsonl = store.query_jsonl(limit=5)
            assert len(recent_jsonl) == 5
            assert recent_jsonl[-1] == jsonl_records[-1]  # Most recent
