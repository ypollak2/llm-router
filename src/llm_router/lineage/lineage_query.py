"""Query and analyze routing lineage data."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_router.lineage.lineage_store import LineageStore

if TYPE_CHECKING:
    from collections.abc import Sequence


class LineageQuery:
    """Query interface for routing lineage data.

    Provides methods to find patterns, detect waste, trace decision chains.
    """

    def __init__(self, store: LineageStore | None = None) -> None:
        """Initialize query engine.

        Args:
            store: LineageStore instance (creates new if None)
        """
        self.store = store or LineageStore()

    def get_recent(self, limit: int = 100) -> list[dict]:
        """Get most recent routing decisions.

        Args:
            limit: Number of records

        Returns:
            List of decisions (most recent first)
        """
        return self.store.query_jsonl(limit=limit)

    def find_wasteful_operations(self, expensive_models: Sequence[str] | None = None) -> list[dict]:
        """Find operations that used expensive models unnecessarily.

        Args:
            expensive_models: Models to consider wasteful for simple operations
                Default: ["claude-opus-4-7", "claude-sonnet-4-6"]

        Returns:
            List of wasteful routing decisions
        """
        if expensive_models is None:
            expensive_models = ["claude-opus-4-7", "claude-sonnet-4-6"]

        # Find simple operations that used expensive models
        placeholders = ",".join("?" * len(expensive_models))
        sql = f"""
            SELECT *
            FROM routing_decisions
            WHERE classification LIKE '%/simple%'
              AND selected_model IN ({placeholders})
            ORDER BY timestamp DESC
        """
        return self.store.query_db(sql, tuple(expensive_models))

    def get_model_usage_by_operation(self) -> dict[str, dict[str, int]]:
        """Get model distribution for each operation.

        Returns:
            Dict mapping operation → {model: count}
        """
        sql = """
            SELECT operation, selected_model, COUNT(*) as count
            FROM routing_decisions
            GROUP BY operation, selected_model
            ORDER BY operation, count DESC
        """
        rows = self.store.query_db(sql)

        result: dict[str, dict[str, int]] = {}
        for row in rows:
            op = row["operation"]
            model = row["selected_model"]
            count = row["count"]
            if op not in result:
                result[op] = {}
            result[op][model] = count

        return result

    def get_token_usage_by_model(self) -> dict[str, dict[str, int | float]]:
        """Get token and cost breakdown by model.

        Returns:
            Dict mapping model → {tokens: count, cost_usd: total, operations: count}
        """
        sql = """
            SELECT
                selected_model,
                SUM(total_tokens) as total_tokens,
                SUM(cost_usd) as total_cost,
                COUNT(*) as operation_count
            FROM routing_decisions
            GROUP BY selected_model
            ORDER BY total_cost DESC
        """
        rows = self.store.query_db(sql)

        result: dict[str, dict[str, int | float]] = {}
        for row in rows:
            model = row["selected_model"]
            result[model] = {
                "tokens": int(row["total_tokens"] or 0),
                "cost_usd": float(row["total_cost"] or 0.0),
                "operations": int(row["operation_count"] or 0),
            }

        return result

    def trace_decision_chain(self, request_id: str) -> list[dict]:
        """Trace all decisions in a request (including nested operations).

        Args:
            request_id: Request ID to trace

        Returns:
            List of decisions in request, ordered by timestamp
        """
        sql = """
            SELECT *
            FROM routing_decisions
            WHERE request_id = ?
            ORDER BY timestamp ASC
        """
        return self.store.query_db(sql, (request_id,))

    def get_fallback_statistics(self) -> dict[str, int]:
        """Get frequency of fallback scenarios.

        Returns:
            Dict mapping fallback_reason → count
        """
        sql = """
            SELECT fallback_reason, COUNT(*) as count
            FROM routing_decisions
            WHERE fallback_reason IS NOT NULL
            GROUP BY fallback_reason
            ORDER BY count DESC
        """
        rows = self.store.query_db(sql)
        return {row["fallback_reason"]: row["count"] for row in rows}

    def get_classification_distribution(self) -> dict[str, int]:
        """Get distribution of task classifications.

        Returns:
            Dict mapping classification → count
        """
        sql = """
            SELECT classification, COUNT(*) as count
            FROM routing_decisions
            GROUP BY classification
            ORDER BY count DESC
        """
        rows = self.store.query_db(sql)
        return {row["classification"]: row["count"] for row in rows}

    def get_average_latency_by_model(self) -> dict[str, float]:
        """Get average latency per model.

        Returns:
            Dict mapping model → average_latency_ms
        """
        sql = """
            SELECT selected_model, AVG(latency_ms) as avg_latency
            FROM routing_decisions
            GROUP BY selected_model
            ORDER BY avg_latency DESC
        """
        rows = self.store.query_db(sql)
        return {row["selected_model"]: float(row["avg_latency"] or 0.0) for row in rows}
