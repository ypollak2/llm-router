"""Community Benchmarks — local routing accuracy tracking (v3.4).

Computes accuracy metrics from the routing_decisions table using user feedback
(llm_rate thumbs) and routing outcomes. Provides per-task-type accuracy stats
and confidence metadata that can be surfaced in routing decisions.

Opt-in anonymous sharing is gated behind LLM_ROUTER_COMMUNITY=true. In v3.4
this only prepares a local export file; the actual upload endpoint is planned
for a future release once the server infrastructure is in place.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from llm_router.cost import _get_db

log = logging.getLogger("llm_router.community")

_COMMUNITY_EXPORT_PATH = Path.home() / ".llm-router" / "community_export.jsonl"


async def get_benchmark_stats() -> dict[str, dict]:
    """Compute accuracy stats per task type from routing_decisions feedback.

    A decision is counted as "good" when was_good=1 (llm_rate thumbs up).
    Decisions without feedback are excluded from accuracy calculations but
    counted in the total sample size.

    Returns:
        Dict mapping task_type → {total, rated, good, bad, accuracy_pct, top_model}
    """
    db = await _get_db()
    try:
        rows = await (await db.execute(
            """
            SELECT task_type,
                   COUNT(*) as total,
                   COUNT(was_good) as rated,
                   SUM(CASE WHEN was_good = 1 THEN 1 ELSE 0 END) as good,
                   SUM(CASE WHEN was_good = 0 THEN 1 ELSE 0 END) as bad,
                   final_model
            FROM routing_decisions
            WHERE task_type IS NOT NULL
            GROUP BY task_type
            ORDER BY total DESC
            """
        )).fetchall()

        # Top model per task type
        top_model_rows = await (await db.execute(
            """
            SELECT task_type, final_model, COUNT(*) as c
            FROM routing_decisions
            WHERE final_model IS NOT NULL
            GROUP BY task_type, final_model
            ORDER BY task_type, c DESC
            """
        )).fetchall()
    finally:
        await db.close()

    # Build top model map (first entry per task_type after ORDER BY c DESC)
    top_models: dict[str, str] = {}
    for task_type, model, _ in top_model_rows:
        if task_type not in top_models and model:
            top_models[task_type] = model

    stats: dict[str, dict] = {}
    for task_type, total, rated, good, bad, _ in rows:
        good = good or 0
        bad  = bad  or 0
        rated = rated or 0
        accuracy = round(good / rated * 100, 1) if rated > 0 else None
        stats[task_type] = {
            "total":        total,
            "rated":        rated,
            "good":         good,
            "bad":          bad,
            "accuracy_pct": accuracy,
            "top_model":    top_models.get(task_type, "unknown"),
        }
    return stats


def get_confidence_str(stats: dict, task_type: str) -> str:
    """Return a human-readable confidence string for a given task type.

    Example: "94% accurate (127 calls, 43 rated)"
    """
    s = stats.get(task_type)
    if not s or s["total"] < 5:
        return "too few calls for confidence estimate"
    if s["accuracy_pct"] is None:
        return f"{s['total']} calls — rate with llm_rate to build accuracy data"
    return (
        f"{s['accuracy_pct']:.0f}% accurate "
        f"({s['total']} calls, {s['rated']} rated)"
    )


def format_benchmark_report(stats: dict[str, dict]) -> str:
    """Format a benchmark accuracy report table."""
    if not stats:
        return "  No routing decisions found. Use llm_route or llm_query to start building data."

    W = 64
    lines = [
        "─" * W,
        "  Routing Accuracy by Task Type  (from llm_rate feedback)",
        "",
        f"  {'Task':<12}  {'Total':>6}  {'Rated':>6}  {'Accuracy':>9}  Top model",
        "",
    ]

    for task_type, s in sorted(stats.items(), key=lambda x: -(x[1]["total"])):
        acc = f"{s['accuracy_pct']:.0f}%" if s["accuracy_pct"] is not None else "—"
        model = s["top_model"]
        short = model.split("/", 1)[-1][:20] if "/" in model else model[:20]
        lines.append(
            f"  {task_type:<12}  {s['total']:>6}  {s['rated']:>6}  {acc:>9}  {short}"
        )

    total_decisions = sum(s["total"] for s in stats.values())
    total_rated     = sum(s["rated"] for s in stats.values())
    total_good      = sum(s["good"]  for s in stats.values())
    overall_acc     = round(total_good / total_rated * 100, 1) if total_rated > 0 else None
    acc_str = f"{overall_acc:.0f}%" if overall_acc is not None else "—"

    lines += [
        "",
        f"  {'TOTAL':<12}  {total_decisions:>6}  {total_rated:>6}  {acc_str:>9}",
        "",
        "  Rate responses with llm_rate to improve accuracy tracking.",
    ]

    if total_rated == 0:
        lines.append(
            "  ℹ️  No feedback yet — use llm_rate after routed responses to track quality."
        )

    lines.append("─" * W)
    return "\n".join(lines)


async def prepare_community_export() -> str:
    """Prepare anonymised export data for future community sharing.

    Data is written to ~/.llm-router/community_export.jsonl.
    PII is stripped: prompts are hashed, no user identity included.

    Returns:
        Path to the export file, or error message.
    """
    db = await _get_db()
    try:
        rows = await (await db.execute(
            """
            SELECT task_type, complexity, final_model, final_provider,
                   was_good, input_tokens, output_tokens, latency_ms
            FROM routing_decisions
            WHERE task_type IS NOT NULL AND final_model IS NOT NULL
            ORDER BY id DESC
            LIMIT 1000
            """
        )).fetchall()
    finally:
        await db.close()

    _COMMUNITY_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(_COMMUNITY_EXPORT_PATH, "w") as f:
        for row in rows:
            entry = {
                "task_type":    row[0],
                "complexity":   row[1],
                "final_model":  row[2],
                "final_provider": row[3],
                "was_good":     row[4],
                "input_tokens": row[5],
                "output_tokens": row[6],
                "latency_ms":   row[7],
            }
            f.write(json.dumps(entry) + "\n")
            count += 1

    return f"{count} decisions exported to {_COMMUNITY_EXPORT_PATH}"
