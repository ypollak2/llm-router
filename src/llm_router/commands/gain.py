"""Token savings analytics dashboard (RTK-style)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from llm_router.config import get_config
from llm_router.terminal_style import Color, Symbol


def bold(text: str) -> str:
    """Make text bold with color."""
    return f"\033[1m{text}\033[0m"


def cyan(text: str) -> str:
    """Apply cyan color."""
    return f"\033[36m{text}\033[0m"


def dim(text: str) -> str:
    """Make text dim."""
    return f"\033[2m{text}\033[0m"


def gold(text: str) -> str:
    """Apply gold/amber color."""
    return f"\033[33m{text}\033[0m"


def green(text: str) -> str:
    """Apply green color."""
    return Color.CONFIDENCE_GREEN(text)


def red(text: str) -> str:
    """Apply red color."""
    return Color.WARNING_RED(text)


def yellow(text: str) -> str:
    """Apply yellow color."""
    return f"\033[33m{text}\033[0m"


def white(text: str) -> str:
    """Apply white color."""
    return f"\033[37m{text}\033[0m"


def underline(text: str) -> str:
    """Apply underline."""
    return f"\033[4m{text}\033[0m"


def table(rows: list[list[str]], headers: list[str]) -> str:
    """Format a simple ASCII table."""
    if not rows:
        return ""

    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    # Build table
    lines = []

    # Header
    header_line = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    lines.append(header_line)
    lines.append("-" * len(header_line))

    # Rows
    for row in rows:
        row_line = " | ".join(cell.ljust(w) for cell, w in zip(row, col_widths))
        lines.append(row_line)

    return "\n".join(lines)


class SavingsAnalytics:
    """Compute and display token savings metrics."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Path.home() / ".llm-router" / "usage.db"

    def get_routing_decisions(self, days: int = 7) -> list[dict]:
        """Fetch routing decisions from the last N days."""
        if not self.db_path.exists():
            return []

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            rows = conn.execute(
                """
                SELECT
                    original_tool,
                    selected_model,
                    complexity,
                    budget_pct_used,
                    estimated_cost_usd,
                    session_id,
                    timestamp
                FROM routing_decisions
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
                """,
                (cutoff,),
            ).fetchall()
            conn.close()

            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def estimate_opus_cost(self, selected_model: str, estimated_cost: float) -> float:
        """Estimate what the cost would be if run on Opus."""
        # Cost multipliers: Opus is ~3x Sonnet, ~10x Haiku
        multipliers = {
            "claude-haiku": 10,
            "claude-sonnet": 3,
            "claude-opus": 1,
            "gemini-flash": 15,
            "gemini-pro": 5,
            "gpt-4o-mini": 8,
            "gpt-4o": 3,
            "o3": 2,
        }

        # Try exact match first, then substring
        for model_key, multiplier in multipliers.items():
            if model_key in selected_model.lower():
                return estimated_cost * multiplier

        # Default: assume 3x cost for unknown models
        return estimated_cost * 3

    def compute_savings(self, days: int = 7) -> dict:
        """Compute savings metrics."""
        decisions = self.get_routing_decisions(days=days)

        if not decisions:
            return {
                "period_days": days,
                "total_decisions": 0,
                "total_cost_usd": 0.0,
                "total_opus_cost_usd": 0.0,
                "total_saved_usd": 0.0,
                "efficiency_multiplier": 1.0,
                "by_tool": {},
                "by_model": {},
                "by_complexity": {},
                "daily_breakdown": {},
            }

        total_cost = 0.0
        total_opus_cost = 0.0
        by_tool = {}
        by_model = {}
        by_complexity = {}
        daily_breakdown = {}

        for decision in decisions:
            cost = decision.get("estimated_cost_usd", 0.0)
            opus_cost = self.estimate_opus_cost(decision["selected_model"], cost)
            tool = decision.get("original_tool", "unknown")
            model = decision.get("selected_model", "unknown")
            complexity = decision.get("complexity", "unknown")
            date = decision.get("timestamp", "").split("T")[0]

            total_cost += cost
            total_opus_cost += opus_cost

            # By tool
            if tool not in by_tool:
                by_tool[tool] = {"count": 0, "cost": 0.0, "opus_cost": 0.0}
            by_tool[tool]["count"] += 1
            by_tool[tool]["cost"] += cost
            by_tool[tool]["opus_cost"] += opus_cost

            # By model
            if model not in by_model:
                by_model[model] = {"count": 0, "cost": 0.0, "opus_cost": 0.0}
            by_model[model]["count"] += 1
            by_model[model]["cost"] += cost
            by_model[model]["opus_cost"] += opus_cost

            # By complexity
            if complexity not in by_complexity:
                by_complexity[complexity] = {"count": 0, "cost": 0.0, "opus_cost": 0.0}
            by_complexity[complexity]["count"] += 1
            by_complexity[complexity]["cost"] += cost
            by_complexity[complexity]["opus_cost"] += opus_cost

            # Daily breakdown
            if date not in daily_breakdown:
                daily_breakdown[date] = {"count": 0, "cost": 0.0, "opus_cost": 0.0}
            daily_breakdown[date]["count"] += 1
            daily_breakdown[date]["cost"] += cost
            daily_breakdown[date]["opus_cost"] += opus_cost

        total_saved = total_opus_cost - total_cost
        efficiency = total_opus_cost / total_cost if total_cost > 0 else 1.0

        return {
            "period_days": days,
            "total_decisions": len(decisions),
            "total_cost_usd": round(total_cost, 4),
            "total_opus_cost_usd": round(total_opus_cost, 4),
            "total_saved_usd": round(total_saved, 4),
            "efficiency_multiplier": round(efficiency, 2),
            "by_tool": by_tool,
            "by_model": by_model,
            "by_complexity": by_complexity,
            "daily_breakdown": daily_breakdown,
        }

    def format_savings(self, savings: dict, period_days: int = 7) -> str:
        """Format savings metrics as terminal output."""
        output = []

        # Header
        output.append("")
        output.append("╔" + "═" * 66 + "╗")
        output.append("║" + " " * 18 + bold("💰 TOKEN SAVINGS DASHBOARD") + " " * 23 + "║")
        output.append("╚" + "═" * 66 + "╝")
        output.append("")

        # Summary
        period = savings["period_days"]
        decisions = savings["total_decisions"]
        cost = savings["total_cost_usd"]
        opus_cost = savings["total_opus_cost_usd"]
        saved = savings["total_saved_usd"]
        multiplier = savings["efficiency_multiplier"]

        output.append(bold(f"Period: Last {period} days  |  Decisions: {decisions}"))
        output.append("")

        # Cost breakdown
        if decisions > 0:
            output.append(bold("COST BREAKDOWN"))
            output.append(f"  {cyan('Actual cost')}           ${cost:.4f}")
            output.append(f"  {yellow('Opus baseline')}        ${opus_cost:.4f}")
            output.append(f"  {green('Total saved')}          ${saved:.4f}")
            output.append(f"  {gold(f'Efficiency: {multiplier}x')} (Opus cost per actual $)")
            output.append("")

            # Savings percentage
            savings_pct = ((opus_cost - cost) / opus_cost * 100) if opus_cost > 0 else 0
            if savings_pct >= 80:
                savings_bar = green(f"█" * 8) + dim(f"█" * 2)
            elif savings_pct >= 60:
                savings_bar = yellow(f"█" * 7) + dim(f"█" * 3)
            else:
                savings_bar = red(f"█" * 6) + dim(f"█" * 4)
            output.append(f"Savings: {savings_bar} {savings_pct:.1f}%")
            output.append("")

            # By model
            if savings["by_model"]:
                output.append(bold("BY MODEL"))
                by_model_sorted = sorted(
                    savings["by_model"].items(),
                    key=lambda x: x[1]["cost"],
                    reverse=True,
                )
                rows = []
                for model, stats in by_model_sorted:
                    rows.append([
                        model,
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        f"${stats['opus_cost']:.4f}",
                        f"${stats['opus_cost'] - stats['cost']:.4f}",
                    ])
                output.append(table(
                    rows,
                    headers=["Model", "Uses", "Actual", "Opus", "Saved"],
                ))
                output.append("")

            # By complexity
            if savings["by_complexity"]:
                output.append(bold("BY COMPLEXITY"))
                by_complexity_sorted = sorted(
                    savings["by_complexity"].items(),
                    key=lambda x: x[1]["cost"],
                    reverse=True,
                )
                rows = []
                for complexity, stats in by_complexity_sorted:
                    rows.append([
                        complexity.upper(),
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        f"${stats['opus_cost']:.4f}",
                        f"${stats['opus_cost'] - stats['cost']:.4f}",
                    ])
                output.append(table(
                    rows,
                    headers=["Complexity", "Uses", "Actual", "Opus", "Saved"],
                ))
                output.append("")

            # By tool
            if savings["by_tool"]:
                output.append(bold("BY TOOL"))
                by_tool_sorted = sorted(
                    savings["by_tool"].items(),
                    key=lambda x: x[1]["count"],
                    reverse=True,
                )
                rows = []
                for tool, stats in by_tool_sorted[:10]:  # Top 10
                    rows.append([
                        tool,
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        f"${stats['opus_cost'] - stats['cost']:.4f}",
                    ])
                output.append(table(
                    rows,
                    headers=["Tool", "Uses", "Cost", "Saved"],
                ))
                output.append("")

            # Daily trend
            if savings["daily_breakdown"]:
                output.append(bold("DAILY TREND (Last 7 days)"))
                daily_sorted = sorted(
                    savings["daily_breakdown"].items(),
                    key=lambda x: x[0],
                    reverse=True,
                )
                rows = []
                for date, stats in daily_sorted[:7]:
                    saved_daily = stats["opus_cost"] - stats["cost"]
                    pct = (saved_daily / stats["opus_cost"] * 100) if stats["opus_cost"] > 0 else 0
                    rows.append([
                        date,
                        str(stats["count"]),
                        f"${stats['cost']:.4f}",
                        f"${saved_daily:.4f}",
                        f"{pct:.1f}%",
                    ])
                output.append(table(
                    rows,
                    headers=["Date", "Uses", "Cost", "Saved", "Savings %"],
                ))
                output.append("")
        else:
            output.append(dim("No routing decisions recorded yet."))
            output.append(dim("Run some LLM tasks and savings will appear here."))
            output.append("")

        # Compression statistics
        output.append("")
        output.append(bold("⚙️  COMPRESSION LAYER"))
        output.append("")
        
        # Get compression stats
        try:
            from llm_router.cost import get_compression_stats
            import asyncio
            compression_data = asyncio.run(get_compression_stats(days=period_days if period_days else 7))
            
            if compression_data.get("total_operations", 0) > 0:
                rtk = compression_data.get("rtk_stats", {})
                if rtk.get("operations", 0) > 0:
                    output.append(bold("RTK Command Output Compression"))
                    ops_count = rtk.get('operations', 0)
                    output.append(f"  Commands processed: {cyan(str(ops_count))}")
                    original = rtk.get('original_tokens', 0)
                    compressed = rtk.get('compressed_tokens', 0)
                    saved = rtk.get('tokens_saved', 0)
                    output.append(f"  Original tokens: {original:,}")
                    output.append(f"  Compressed tokens: {compressed:,}")
                    output.append(f"  Tokens saved: {green(str(saved))}")
                    
                    if original > 0:
                        compression_pct = (1 - rtk.get("avg_compression_ratio", 1)) * 100
                        output.append(f"  Compression ratio: {cyan(f'{compression_pct:.1f}%')} reduction")
                    
                    # By strategy
                    strategies = compression_data.get("by_strategy", {})
                    if strategies:
                        output.append("")
                        output.append(f"  {dim('Top compression strategies:')}")
                        for i, (strategy, stats) in enumerate(list(strategies.items())[:3]):
                            saved_tokens = stats.get("tokens_saved", 0)
                            ops_count = stats.get("operations", 0)
                            output.append(f"    {i+1}. {strategy}: {saved_tokens:,} tokens saved ({ops_count} ops)")
                    
                    output.append("")
            else:
                output.append(dim("No command compression yet. Compression activates when shell commands are used."))
                output.append("")
        except Exception:
            pass

        # Footer
        output.append(dim("💡 Tip: Use 'llm_usage' to see detailed cost breakdown by provider"))
        output.append(dim("        Use 'llm_savings' to see savings over different time periods"))
        output.append(dim("        RTK compression activates on shell commands (git, pytest, cargo, etc)"))
        output.append("")

        return "\n".join(output)


def show_gain(period: str = "week") -> str:
    """Show token savings dashboard (RTK-style gain command)."""
    days_map = {
        "today": 1,
        "week": 7,
        "month": 30,
        "all": 365,
    }

    days = days_map.get(period, 7)
    analytics = SavingsAnalytics()
    savings = analytics.compute_savings(days=days)

    return analytics.format_savings(savings, period_days=days)


if __name__ == "__main__":
    print(show_gain("week"))
