"""Detailed savings report command.

Shows comprehensive token and cost breakdown across all models, periods, and usage patterns.

Usage:
    llm-router savings-report              — full report (all time)
    llm-router savings-report --period week — weekly report
    llm-router savings-report --period day  — today only
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _format_tokens(count: int) -> str:
    """Format token count as human-readable (e.g., 18.4k, 1.2M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _get_db_path() -> Path:
    """Get path to the usage database."""
    return Path.home() / ".llm-router" / "usage.db"


def _get_time_filter(period: str = "all") -> str:
    """Get SQL WHERE clause for time period filter."""
    if period == "day":
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    elif period == "week":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    elif period == "month":
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    else:
        return ""  # "all" — no filter

    cutoff_str = cutoff.isoformat()
    return f"AND timestamp > '{cutoff_str}'"


def _query_routing_stats(db_path: Path, period: str = "all") -> dict:
    """Query routing (external API) statistics from database."""
    time_filter = _get_time_filter(period)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Overall stats
        query = f"""
            SELECT
                COUNT(*) as calls,
                SUM(input_tokens) as total_in,
                SUM(output_tokens) as total_out,
                SUM(cost_usd) as total_cost,
                provider,
                model
            FROM usage
            WHERE 1=1 {time_filter}
            GROUP BY provider, model
            ORDER BY calls DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        stats = {
            "total_calls": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "by_provider": {},
            "by_model": {},
        }

        for row in rows:
            calls = row["calls"]
            in_tok = row["total_in"] or 0
            out_tok = row["total_out"] or 0
            cost = row["total_cost"] or 0.0
            total_tok = in_tok + out_tok
            provider = row["provider"] or "unknown"
            model = row["model"] or "unknown"

            stats["total_calls"] += calls
            stats["total_tokens"] += total_tok
            stats["total_cost"] += cost

            # By provider
            if provider not in stats["by_provider"]:
                stats["by_provider"][provider] = {
                    "calls": 0,
                    "tokens": 0,
                    "cost": 0.0,
                }
            stats["by_provider"][provider]["calls"] += calls
            stats["by_provider"][provider]["tokens"] += total_tok
            stats["by_provider"][provider]["cost"] += cost

            # By model
            if model not in stats["by_model"]:
                stats["by_model"][model] = {
                    "calls": 0,
                    "tokens": 0,
                    "cost": 0.0,
                    "provider": provider,
                }
            stats["by_model"][model]["calls"] += calls
            stats["by_model"][model]["tokens"] += total_tok
            stats["by_model"][model]["cost"] += cost

        conn.close()
        return stats

    except Exception as e:
        print(f"Error querying database: {e}")
        return {}


def _query_free_stats(db_path: Path, period: str = "all") -> dict:
    """Query free model (Ollama, Codex) statistics."""
    time_filter = _get_time_filter(period)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Look for zero-cost or free provider entries
        query = f"""
            SELECT
                COUNT(*) as calls,
                SUM(input_tokens) as total_in,
                SUM(output_tokens) as total_out,
                provider,
                model
            FROM usage
            WHERE (cost_usd = 0 OR cost_usd IS NULL OR provider IN ('ollama', 'codex'))
            {time_filter}
            GROUP BY provider, model
            ORDER BY calls DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        stats = {"total_calls": 0, "total_tokens": 0, "by_provider": {}}

        for row in rows:
            calls = row["calls"]
            in_tok = row["total_in"] or 0
            out_tok = row["total_out"] or 0
            total_tok = in_tok + out_tok
            provider = row["provider"] or "unknown"
            model = row["model"] or "unknown"

            stats["total_calls"] += calls
            stats["total_tokens"] += total_tok

            if provider not in stats["by_provider"]:
                stats["by_provider"][provider] = {
                    "calls": 0,
                    "tokens": 0,
                    "models": {},
                }
            stats["by_provider"][provider]["calls"] += calls
            stats["by_provider"][provider]["tokens"] += total_tok
            stats["by_provider"][provider]["models"][model] = {
                "calls": calls,
                "tokens": total_tok,
            }

        conn.close()
        return stats

    except Exception:
        return {}


def _calculate_baseline_cost(tokens: int) -> float:
    """Calculate Sonnet baseline cost for tokens."""
    SONNET_INPUT_PER_M = 3.0
    SONNET_OUTPUT_PER_M = 15.0
    # Conservative estimate: 40% input, 60% output
    input_tokens = int(tokens * 0.4)
    output_tokens = int(tokens * 0.6)
    return (input_tokens * SONNET_INPUT_PER_M + output_tokens * SONNET_OUTPUT_PER_M) / 1_000_000


def render_savings_report(period: str = "all") -> str:
    """Generate and render detailed savings report."""
    db_path = _get_db_path()

    if not db_path.exists():
        return "No usage data found. Start routing prompts to generate data."

    routing_stats = _query_routing_stats(db_path, period)
    free_stats = _query_free_stats(db_path, period)

    if not routing_stats.get("total_calls") and not free_stats.get("total_calls"):
        return "No routing data available for this period."

    lines = []

    # Period label
    period_label = {
        "day": "Last 24 Hours",
        "week": "Last 7 Days",
        "month": "Last 30 Days",
        "all": "All Time",
    }.get(period, "All Time")

    lines.append(f"\n╭─ DETAILED SAVINGS REPORT ─ {period_label} " + "─" * 40 + "╮")
    lines.append("│")

    # === EXTERNAL ROUTING SECTION ===
    if routing_stats.get("total_calls"):
        lines.append("│ EXTERNAL ROUTING (Paid APIs)")
        lines.append("│")

        total_calls = routing_stats["total_calls"]
        total_tokens = routing_stats["total_tokens"]
        total_cost = routing_stats["total_cost"]
        baseline_cost = _calculate_baseline_cost(total_tokens)
        saved = max(0.0, baseline_cost - total_cost)
        savings_pct = round(saved / baseline_cost * 100) if baseline_cost > 0 else 0

        tokens_str = _format_tokens(total_tokens)
        lines.append(
            f"│ {total_calls:>6} calls  │  {tokens_str:>8} tokens  │  "
            f"${total_cost:.4f} actual vs ${baseline_cost:.4f} baseline  │  "
            f"${saved:.4f} saved ({savings_pct}%)"
        )
        lines.append("│")

        # By provider
        if routing_stats["by_provider"]:
            lines.append("│ BY PROVIDER:")
            for provider, data in sorted(
                routing_stats["by_provider"].items(), key=lambda x: -x[1]["cost"]
            ):
                tokens_str = _format_tokens(data["tokens"])
                prov_baseline = _calculate_baseline_cost(data["tokens"])
                prov_saved = max(0.0, prov_baseline - data["cost"])
                lines.append(
                    f"│   {provider:<12} {data['calls']:>4}×  {tokens_str:>8}  "
                    f"${data['cost']:.4f}  (saved: ${prov_saved:.4f})"
                )
            lines.append("│")

        # By model (top 10)
        if routing_stats["by_model"]:
            lines.append("│ BY MODEL (Top 10):")
            for model, data in sorted(
                routing_stats["by_model"].items(), key=lambda x: -x[1]["calls"]
            )[:10]:
                tokens_str = _format_tokens(data["tokens"])
                model_baseline = _calculate_baseline_cost(data["tokens"])
                model_saved = max(0.0, model_baseline - data["cost"])
                lines.append(
                    f"│   {model:<24} {data['calls']:>4}×  {tokens_str:>8}  "
                    f"${data['cost']:.4f}  (saved: ${model_saved:.4f})"
                )
            lines.append("│")

    # === FREE ROUTING SECTION ===
    if free_stats.get("total_calls"):
        lines.append("│ FREE ROUTING (Local + Codex)")
        lines.append("│")

        total_calls = free_stats["total_calls"]
        total_tokens = free_stats["total_tokens"]
        baseline_cost = _calculate_baseline_cost(total_tokens)

        tokens_str = _format_tokens(total_tokens)
        lines.append(
            f"│ {total_calls:>6} calls  │  {tokens_str:>8} tokens  │  "
            f"$0.0000 actual vs ${baseline_cost:.4f} baseline  │  "
            f"${baseline_cost:.4f} saved (100%)"
        )
        lines.append("│")

        # By provider
        if free_stats["by_provider"]:
            lines.append("│ BY PROVIDER:")
            for provider, data in sorted(
                free_stats["by_provider"].items(), key=lambda x: -x[1]["tokens"]
            ):
                tokens_str = _format_tokens(data["tokens"])
                prov_baseline = _calculate_baseline_cost(data["tokens"])
                lines.append(
                    f"│   {provider:<12} {data['calls']:>4}×  {tokens_str:>8}  "
                    f"$0.0000 actual  (saved: ${prov_baseline:.4f})"
                )

                # Sub-models
                for model, model_data in sorted(
                    data["models"].items(), key=lambda x: -x[1]["tokens"]
                ):
                    model_tokens_str = _format_tokens(model_data["tokens"])
                    model_baseline = _calculate_baseline_cost(model_data["tokens"])
                    lines.append(
                        f"│     • {model:<20} {model_data['calls']:>3}×  "
                        f"{model_tokens_str:>8}  (saved: ${model_baseline:.4f})"
                    )
            lines.append("│")

    # === SUMMARY ===
    if routing_stats.get("total_calls") and free_stats.get("total_calls"):
        combined_tokens = routing_stats["total_tokens"] + free_stats["total_tokens"]
        combined_cost = routing_stats["total_cost"]
        combined_baseline = _calculate_baseline_cost(combined_tokens)
        combined_saved = combined_baseline - combined_cost
        combined_pct = (
            round(combined_saved / combined_baseline * 100) if combined_baseline > 0 else 0
        )

        lines.append("│ COMBINED TOTALS:")
        lines.append(
            f"│ {_format_tokens(combined_tokens):>8} tokens  │  "
            f"${combined_cost:.4f} actual vs ${combined_baseline:.4f} baseline  │  "
            f"${combined_saved:.4f} saved ({combined_pct}%)"
        )

    lines.append("│")
    lines.append("╰" + "─" * 77 + "╯\n")

    return "\n".join(lines)


def main(args: list[str]) -> int:
    """Entry point for savings-report command."""
    period = "all"

    if "--period" in args:
        idx = args.index("--period")
        if idx + 1 < len(args):
            period = args[idx + 1]

    print(render_savings_report(period))
    return 0
