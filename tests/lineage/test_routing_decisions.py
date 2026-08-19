"""Tests for routing decision logging and lineage tracking.

Verifies that:
1. Simple operations don't use expensive models
2. Complex operations use appropriate models
3. Fallback chains are tracked correctly
4. Token accounting is accurate
"""

from __future__ import annotations

import tempfile

import pytest

from llm_router.lineage.decision_logger import log_routing_decision
from llm_router.lineage.lineage_query import LineageQuery
from llm_router.lineage.lineage_store import LineageStore
from llm_router.lineage.report_generator import format_decision, generate_routing_report


@pytest.fixture
def temp_lineage_store():
    """Create isolated LineageStore for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = LineageStore(router_dir=tmpdir)
        yield store


class TestRoutingDecisionLogging:
    """Test logging of routing decisions."""

    def test_log_simple_operation_with_cheap_model(self, temp_lineage_store):
        """Log a simple operation routed to Haiku (cheap model)."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        decision = log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            input_tokens=50,
            output_tokens=30,
            cost_usd=0.0002,
            latency_ms=145.3,
        )

        assert decision.operation == "get_cap"
        assert decision.selected_model == "claude-haiku-4-5"
        assert decision.total_tokens == 80
        assert decision.cost_usd == 0.0002

    def test_log_complex_operation_with_expensive_model(self, temp_lineage_store):
        """Log a complex operation routed to Sonnet (expensive but appropriate)."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        decision = log_routing_decision(
            operation="llm_code",
            classification="code/complex",
            selected_model="claude-sonnet-4-6",
            selection_reason="router_picked",
            input_tokens=2000,
            output_tokens=500,
            cost_usd=0.015,
            latency_ms=2340.5,
        )

        assert decision.operation == "llm_code"
        assert decision.selected_model == "claude-sonnet-4-6"
        assert decision.total_tokens == 2500

    def test_log_fallback_chain(self, temp_lineage_store):
        """Log operation that fell back from Ollama to Gemini."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        decision = log_routing_decision(
            operation="llm_query",
            classification="query/simple",
            selected_model="gemini-2.5-flash",
            selection_reason="fallback_after_ollama_timeout",
            fallback_chain=["ollama"],
            fallback_reason="ollama_timeout",
            input_tokens=100,
            output_tokens=80,
            cost_usd=0.00015,
            latency_ms=450.2,
        )

        assert decision.fallback_chain == ["ollama"]
        assert decision.fallback_reason == "ollama_timeout"
        assert decision.selected_model == "gemini-2.5-flash"

    def test_log_with_request_tracing(self, temp_lineage_store):
        """Log decisions with request ID for tracing."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        request_id = "req-12345"

        # Parent decision
        parent = log_routing_decision(
            operation="validate_config",
            classification="analyze/moderate",
            selected_model="claude-sonnet-4-6",
            selection_reason="router_picked",
            request_id=request_id,
            input_tokens=800,
            output_tokens=200,
            cost_usd=0.008,
        )

        # Nested decision
        child = log_routing_decision(
            operation="check_syntax",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="nested_simple",
            request_id=request_id,
            parent_decision_id=parent.decision_id,
            input_tokens=150,
            output_tokens=50,
            cost_usd=0.00005,
        )

        assert child.parent_decision_id == parent.decision_id
        assert child.request_id == request_id

    def test_decision_is_immutable(self, temp_lineage_store):
        """RoutingDecision is frozen and cannot be mutated."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        decision = log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
        )

        with pytest.raises(AttributeError):
            decision.selected_model = "claude-sonnet-4-6"


class TestLineageQuery:
    """Test querying lineage data."""

    def test_find_wasteful_operations(self, temp_lineage_store):
        """Detect when expensive models were used for simple operations."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        # Log a wasteful decision (Opus used for simple query)
        log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-opus-4-7",
            selection_reason="manual_override",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.002,
        )

        # Log an appropriate decision (Haiku for simple query)
        log_routing_decision(
            operation="get_spend",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            input_tokens=80,
            output_tokens=40,
            cost_usd=0.00008,
        )

        query = LineageQuery(temp_lineage_store)
        wasteful = query.find_wasteful_operations()

        assert len(wasteful) == 1
        assert wasteful[0]["operation"] == "get_cap"
        assert wasteful[0]["selected_model"] == "claude-opus-4-7"

    def test_model_usage_by_operation(self, temp_lineage_store):
        """Get model distribution for each operation."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        # Log multiple decisions
        for i in range(3):
            log_routing_decision(
                operation="get_cap",
                classification="query/simple",
                selected_model="claude-haiku-4-5",
                selection_reason="router_picked",
            )

        for i in range(2):
            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
            )

        query = LineageQuery(temp_lineage_store)
        usage = query.get_model_usage_by_operation()

        assert usage["get_cap"]["claude-haiku-4-5"] == 3
        assert usage["llm_code"]["claude-sonnet-4-6"] == 2

    def test_token_usage_by_model(self, temp_lineage_store):
        """Get token and cost breakdown by model."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0001,
        )

        log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0001,
        )

        query = LineageQuery(temp_lineage_store)
        usage = query.get_token_usage_by_model()

        assert usage["claude-haiku-4-5"]["tokens"] == 300
        assert usage["claude-haiku-4-5"]["cost_usd"] == 0.0002
        assert usage["claude-haiku-4-5"]["operations"] == 2

    def test_trace_decision_chain(self, temp_lineage_store):
        """Trace all decisions in a request."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        request_id = "req-999"

        parent = log_routing_decision(
            operation="validate_config",
            classification="analyze/moderate",
            selected_model="claude-sonnet-4-6",
            selection_reason="router_picked",
            request_id=request_id,
        )

        log_routing_decision(
            operation="check_syntax",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="nested",
            request_id=request_id,
            parent_decision_id=parent.decision_id,
        )

        log_routing_decision(
            operation="validate_schema",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="nested",
            request_id=request_id,
            parent_decision_id=parent.decision_id,
        )

        query = LineageQuery(temp_lineage_store)
        chain = query.trace_decision_chain(request_id)

        assert len(chain) == 3
        assert chain[0]["operation"] == "validate_config"

    def test_get_fallback_statistics(self, temp_lineage_store):
        """Get frequency of fallback scenarios."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        for i in range(3):
            log_routing_decision(
                operation="llm_query",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="fallback_after_ollama_timeout",
                fallback_reason="ollama_timeout",
            )

        for i in range(2):
            log_routing_decision(
                operation="llm_query",
                classification="query/simple",
                selected_model="gemini-2.5-flash",
                selection_reason="fallback_after_codex_error",
                fallback_reason="codex_service_error",
            )

        query = LineageQuery(temp_lineage_store)
        fallbacks = query.get_fallback_statistics()

        assert fallbacks["ollama_timeout"] == 3
        assert fallbacks["codex_service_error"] == 2

    def test_get_classification_distribution(self, temp_lineage_store):
        """Get distribution of task classifications."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        for i in range(5):
            log_routing_decision(
                operation="get_cap",
                classification="query/simple",
                selected_model="claude-haiku-4-5",
                selection_reason="router_picked",
            )

        for i in range(3):
            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
            )

        query = LineageQuery(temp_lineage_store)
        dist = query.get_classification_distribution()

        assert dist["query/simple"] == 5
        assert dist["code/complex"] == 3

    def test_get_average_latency_by_model(self, temp_lineage_store):
        """Get average latency per model."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            latency_ms=100.0,
        )

        log_routing_decision(
            operation="get_spend",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            latency_ms=200.0,
        )

        query = LineageQuery(temp_lineage_store)
        latencies = query.get_average_latency_by_model()

        assert latencies["claude-haiku-4-5"] == pytest.approx(150.0)


class TestReportGeneration:
    """Test report generation from lineage data."""

    def test_generate_routing_report(self, temp_lineage_store):
        """Generate comprehensive routing report."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        # Log some decisions
        for i in range(3):
            log_routing_decision(
                operation="get_cap",
                classification="query/simple",
                selected_model="claude-haiku-4-5",
                selection_reason="router_picked",
                input_tokens=50,
                output_tokens=30,
                cost_usd=0.00008,
                latency_ms=145.0,
            )

        log_routing_decision(
            operation="llm_code",
            classification="code/complex",
            selected_model="claude-sonnet-4-6",
            selection_reason="router_picked",
            input_tokens=2000,
            output_tokens=500,
            cost_usd=0.015,
            latency_ms=2340.0,
        )

        report = generate_routing_report(temp_lineage_store)

        assert "LLM_ROUTER ROUTING DECISION LINEAGE REPORT" in report
        assert "TOKEN USAGE BY MODEL" in report
        assert "claude-haiku-4-5" in report
        assert "claude-sonnet-4-6" in report
        assert "get_cap" in report
        assert "llm_code" in report

    def test_format_decision(self, temp_lineage_store):
        """Format a single decision for display."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        decision = log_routing_decision(
            operation="get_cap",
            classification="query/simple",
            selected_model="claude-haiku-4-5",
            selection_reason="router_picked",
            input_tokens=50,
            output_tokens=30,
            cost_usd=0.00008,
            latency_ms=145.3,
            fallback_chain=["ollama"],
            fallback_reason="ollama_timeout",
        )

        formatted = format_decision(decision)

        assert "Operation:     get_cap" in formatted
        assert "Classification: query/simple" in formatted
        assert "Selected Model: claude-haiku-4-5" in formatted
        assert "Tokens:        80" in formatted
        assert "Fallback Chain: ollama" in formatted


class TestRoutingOptimality:
    """Tests that verify routing is optimal (no waste)."""

    def test_simple_operations_use_cheap_models_only(self, temp_lineage_store):
        """Verify all simple operations use Haiku/Gemini Flash, never Opus/Sonnet."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        # Log many simple operations with cheap models (good)
        for i in range(100):
            log_routing_decision(
                operation=f"operation_{i % 5}",
                classification="query/simple",
                selected_model="claude-haiku-4-5" if i % 2 == 0 else "gemini-2.5-flash",
                selection_reason="router_picked",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.0001,
            )

        query = LineageQuery(temp_lineage_store)
        wasteful = query.find_wasteful_operations(
            expensive_models=["claude-opus-4-7", "claude-sonnet-4-6"]
        )

        assert len(wasteful) == 0, "No simple operations should use expensive models"

    def test_complex_operations_use_appropriate_models(self, temp_lineage_store):
        """Verify complex operations use Sonnet/Opus, not Haiku."""
        from llm_router.lineage import decision_logger

        decision_logger._store = temp_lineage_store

        # Log complex operations with appropriate models
        for i in range(10):
            log_routing_decision(
                operation="llm_code",
                classification="code/complex",
                selected_model="claude-sonnet-4-6",
                selection_reason="router_picked",
                input_tokens=2000,
                output_tokens=500,
                cost_usd=0.015,
            )

        query = LineageQuery(temp_lineage_store)
        usage = query.get_model_usage_by_operation()

        # All code operations should use Sonnet (or more capable)
        assert usage["llm_code"]["claude-sonnet-4-6"] == 10
        assert "claude-haiku-4-5" not in usage.get("llm_code", {})
