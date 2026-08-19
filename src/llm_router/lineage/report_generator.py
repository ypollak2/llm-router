"""Generate human-readable reports from routing lineage data."""

from __future__ import annotations

from llm_router.lineage.decision_logger import RoutingDecision
from llm_router.lineage.lineage_query import LineageQuery
from llm_router.lineage.lineage_store import LineageStore


def generate_routing_report(store: LineageStore | None = None) -> str:
    """Generate comprehensive routing efficiency report.

    Args:
        store: LineageStore instance (creates new if None)

    Returns:
        Formatted report as string
    """
    if store is None:
        store = LineageStore()

    query = LineageQuery(store)

    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        "║             LLM_ROUTER ROUTING DECISION LINEAGE REPORT               ║",
        "╚══════════════════════════════════════════════════════════════════╝",
        "",
    ]

    # Section 1: Token usage by model
    lines.append("📊 TOKEN USAGE BY MODEL")
    lines.append("─" * 70)
    token_usage = query.get_token_usage_by_model()
    if token_usage:
        total_cost = sum(v["cost_usd"] for v in token_usage.values())
        for model, stats in sorted(
            token_usage.items(), key=lambda x: x[1]["cost_usd"], reverse=True
        ):
            pct = (stats["cost_usd"] / total_cost * 100) if total_cost > 0 else 0
            lines.append(
                f"  {model:30} {stats['tokens']:8,} tokens  "
                f"${stats['cost_usd']:7.2f}  {pct:5.1f}%  ({stats['operations']} ops)"
            )
    lines.append("")

    # Section 2: Model distribution per operation
    lines.append("🎯 MODEL DISTRIBUTION BY OPERATION")
    lines.append("─" * 70)
    model_by_op = query.get_model_usage_by_operation()
    for operation in sorted(model_by_op.keys()):
        lines.append(f"  {operation}")
        for model, count in sorted(
            model_by_op[operation].items(), key=lambda x: x[1], reverse=True
        ):
            lines.append(f"    → {model:35} {count:4} times")
    lines.append("")

    # Section 3: Wasteful operations (expensive models on simple tasks)
    lines.append("⚠️  WASTEFUL OPERATIONS (Expensive models on simple tasks)")
    lines.append("─" * 70)
    wasteful = query.find_wasteful_operations()
    if wasteful:
        for op in wasteful[:10]:  # Show top 10
            lines.append(
                f"  {op['operation']:20} → {op['selected_model']:30} "
                f"({op['total_tokens']} tokens, ${op['cost_usd']:.4f})"
            )
        if len(wasteful) > 10:
            lines.append(f"  ... and {len(wasteful) - 10} more")
    else:
        lines.append("  ✅ None detected!")
    lines.append("")

    # Section 4: Task classification distribution
    lines.append("📋 TASK CLASSIFICATION DISTRIBUTION")
    lines.append("─" * 70)
    classifications = query.get_classification_distribution()
    total_ops = sum(classifications.values())
    for cls in sorted(classifications.keys()):
        count = classifications[cls]
        pct = (count / total_ops * 100) if total_ops > 0 else 0
        lines.append(f"  {cls:30} {count:6}  ({pct:5.1f}%)")
    lines.append("")

    # Section 5: Fallback chain analysis
    lines.append("🔄 FALLBACK CHAIN ANALYSIS")
    lines.append("─" * 70)
    fallbacks = query.get_fallback_statistics()
    if fallbacks:
        for reason, count in sorted(fallbacks.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {reason:40} {count:6} times")
    else:
        lines.append("  ✅ No fallbacks needed!")
    lines.append("")

    # Section 6: Average latency by model
    lines.append("⏱️  AVERAGE LATENCY BY MODEL")
    lines.append("─" * 70)
    latencies = query.get_average_latency_by_model()
    for model in sorted(latencies.keys(), key=lambda x: latencies[x], reverse=True):
        latency = latencies[model]
        lines.append(f"  {model:30} {latency:8.1f} ms")
    lines.append("")

    # Section 7: Recent decisions
    lines.append("📝 RECENT ROUTING DECISIONS (Last 10)")
    lines.append("─" * 70)
    recent = query.get_recent(limit=10)
    for decision_data in reversed(recent):  # Show most recent at bottom
        d = decision_data
        lines.append(
            f"  {d['operation']:20} {d['classification']:20} → {d['selected_model']:20}"
        )
        if d.get("fallback_chain"):
            import json

            chain = d["fallback_chain"]
            if isinstance(chain, str):
                chain = json.loads(chain)
            if chain:
                lines.append(f"    Fallback chain: {' → '.join(chain)}")
        lines.append(f"    Input tokens: {d.get('input_tokens', 0)}  Output tokens: {d.get('output_tokens', 0)}  Cost: ${d['cost_usd']:.4f}  Latency: {d['latency_ms']:.1f}ms")
    lines.append("")

    return "\n".join(lines)


def format_decision(decision: RoutingDecision) -> str:
    """Format a single routing decision for display.

    Args:
        decision: RoutingDecision to format

    Returns:
        Formatted string
    """
    lines = [
        f"Operation:     {decision.operation}",
        f"Classification: {decision.classification}",
        f"Selected Model: {decision.selected_model}",
        f"Selection Reason: {decision.selection_reason}",
        f"Tokens:        {decision.total_tokens} (in: {decision.input_tokens}, out: {decision.output_tokens})",
        f"Cost:          ${decision.cost_usd:.4f}",
        f"Latency:       {decision.latency_ms:.1f} ms",
        f"Routing Overhead: {decision.routing_overhead_ms:.1f} ms",
    ]

    if decision.fallback_chain:
        lines.append(f"Fallback Chain: {' → '.join(decision.fallback_chain)}")

    if decision.fallback_reason:
        lines.append(f"Fallback Reason: {decision.fallback_reason}")

    if decision.metadata:
        import json

        lines.append(f"Metadata:      {json.dumps(decision.metadata)}")

    return "\n".join(lines)
