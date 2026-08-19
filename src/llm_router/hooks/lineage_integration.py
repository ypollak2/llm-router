"""Session hook integration for routing lineage tracking.

Provides utilities for SessionStart and SessionEnd hooks to:
- Initialize routing lineage tracking at session start
- Generate efficiency reports and waste detection at session end
- Show optimization recommendations
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR = os.path.expanduser("~/.llm-router")


def init_session_lineage() -> None:
    """Initialize routing lineage tracking for a new session.

    Called at SessionStart to reset lineage databases so session-end report
    only contains decisions from this session.
    """
    router_dir = Path(STATE_DIR)
    router_dir.mkdir(parents=True, exist_ok=True)

    # Remove old lineage files so session report is clean
    lineage_jsonl = router_dir / "routing_lineage.jsonl"
    lineage_db = router_dir / "routing_lineage.db"

    try:
        if lineage_jsonl.exists():
            lineage_jsonl.unlink()
        if lineage_db.exists():
            lineage_db.unlink()
    except OSError:
        pass  # Ignore if already deleted

    # Create marker file to indicate lineage tracking is active
    try:
        marker = router_dir / ".lineage_active"
        marker.write_text("1")
    except OSError:
        pass


def get_session_routing_report(store=None) -> str:
    """Generate routing efficiency report for session-end display.

    Args:
        store: Optional LineageStore instance. Uses default if None.

    Returns:
        Formatted report string with token usage, waste detection, etc.
        Empty string if no lineage data available.
    """
    try:
        from llm_router.lineage.lineage_query import LineageQuery
        from llm_router.lineage.report_generator import generate_routing_report

        query = LineageQuery(store=store)
        recent = query.get_recent(limit=1)

        if not recent:
            return ""  # No routing decisions this session

        return generate_routing_report(store=store)

    except Exception:
        return ""  # Gracefully fail if lineage system not available


def get_waste_alerts(store=None) -> list[str]:
    """Detect wasteful routing patterns and return alert messages.

    Args:
        store: Optional LineageStore instance. Uses default if None.

    Returns:
        List of alert strings (empty if no waste detected)
    """
    try:
        from llm_router.lineage.lineage_query import LineageQuery

        query = LineageQuery(store=store)
        wasteful = query.find_wasteful_operations(
            expensive_models=["claude-opus-4-7", "claude-sonnet-4-6"]
        )

        if not wasteful:
            return []  # No waste detected

        alerts = []

        # Group by operation
        by_op: dict[str, list] = {}
        for op in wasteful:
            op_name = op["operation"]
            if op_name not in by_op:
                by_op[op_name] = []
            by_op[op_name].append(op)

        total_wasted = sum(op["cost_usd"] for op in wasteful)

        # Build alert message
        alerts.append(f"⚠️  ROUTING WASTE DETECTED: {len(wasteful)} operations used expensive models unnecessarily")

        for op_name, instances in sorted(by_op.items()):
            op_cost = sum(i["cost_usd"] for i in instances)
            model_name = instances[0]["selected_model"]
            alerts.append(
                f"  • {op_name} on {model_name}: {len(instances)}x (${op_cost:.4f})"
            )

        # Calculate potential savings
        efficient_cost = sum(0.00006 for op in wasteful)  # What Haiku would cost
        potential_savings = total_wasted - efficient_cost

        alerts.append(f"  💰 Potential savings if routed to Haiku: ${potential_savings:.4f}")

        return alerts

    except Exception:
        return []  # Gracefully fail if lineage system not available


def get_routing_metrics_summary(store=None) -> dict:
    """Get high-level metrics about routing efficiency this session.

    Args:
        store: Optional LineageStore instance. Uses default if None.

    Returns:
        Dict with keys: model_count, operation_count, avg_tokens, total_cost, etc.
        Empty dict if no data available.
    """
    try:
        from llm_router.lineage.lineage_query import LineageQuery

        query = LineageQuery(store=store)
        recent = query.get_recent(limit=1)

        if not recent:
            return {}

        token_usage = query.get_token_usage_by_model()
        model_usage = query.get_model_usage_by_operation()
        classifications = query.get_classification_distribution()

        return {
            "model_count": len(token_usage),
            "operation_count": sum(sum(v.values()) for v in model_usage.values()),
            "total_tokens": sum(v["tokens"] for v in token_usage.values()),
            "total_cost": sum(v["cost_usd"] for v in token_usage.values()),
            "models_used": list(token_usage.keys()),
            "classifications": classifications,
        }

    except Exception:
        return {}


def format_routing_section(store=None) -> str:
    """Format routing efficiency section for session-end report.

    Includes:
    - High-level metrics
    - Waste alerts if any
    - Optimization recommendations

    Args:
        store: Optional LineageStore instance. Uses default if None.

    Returns:
        Formatted section string (empty if no data)
    """
    try:
        from llm_router.lineage.lineage_query import LineageQuery

        query = LineageQuery(store=store)
        recent = query.get_recent(limit=1)

        if not recent:
            return ""  # No routing decisions this session

        metrics = get_routing_metrics_summary(store=store)
        if not metrics:
            return ""

        lines = [
            "",
            "╔════════════════════════════════════════════════════════════════╗",
            "║             📊 LLM_ROUTER ROUTING EFFICIENCY REPORT                ║",
            "╚════════════════════════════════════════════════════════════════╝",
        ]

        # Show metrics
        lines.append(f"\n  Operations:  {metrics['operation_count']} across {metrics['model_count']} models")
        lines.append(f"  Tokens:      {metrics['total_tokens']:,}")
        lines.append(f"  Cost:        ${metrics['total_cost']:.4f}")

        # Show model distribution
        token_usage = query.get_token_usage_by_model()
        if token_usage:
            lines.append("\n  Models Used:")
            for model, stats in sorted(
                token_usage.items(), key=lambda x: x[1]["cost_usd"], reverse=True
            )[:5]:  # Top 5 models
                lines.append(f"    • {model:30} ${stats['cost_usd']:.4f}")

        # Show waste alerts
        alerts = get_waste_alerts(store=store)
        if alerts:
            lines.append("\n  " + "\n  ".join(alerts))
        else:
            lines.append("\n  ✅ No wasteful routing detected (all operations used appropriate models)")

        lines.append("")

        return "\n".join(lines)

    except Exception:
        return ""  # Gracefully fail
