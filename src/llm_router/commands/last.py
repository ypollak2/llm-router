#!/usr/bin/env python3
"""llm-router last — Show the most recent routing decisions (real-time feedback).

Command: uv run llm-router last [--count N]

Shows the last N routing decisions in reverse chronological order (newest first).
Useful for checking what model handled your last request.

Example output:
  Just now   → routed to ollama/gemma4:latest (query/simple) - FREE
  2 min ago  → routed to openai/gpt-4o-mini (code/simple) - $0.0010
  5 min ago  → routed to openai/gpt-4o (analysis/moderate) - $0.0062
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from llm_router.terminal_style import Color, Symbol


def get_usage_db_path() -> Path:
    """Get path to usage.db."""
    return Path.home() / ".llm-router" / "usage.db"


def fetch_recent_decisions(
    db_path: Path,
    count: int = 5,
) -> list[dict]:
    """Fetch most recent routing decisions.

    Args:
        db_path: Path to usage.db
        count: Number of decisions to fetch

    Returns:
        List of routing decision dicts (newest first)
    """
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM routing_decisions WHERE success = 1 ORDER BY timestamp DESC LIMIT ?"
        cursor.execute(query, (count,))
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def format_time_ago(timestamp_str: str) -> str:
    """Format ISO timestamp as relative time (e.g., 'just now', '2 min ago')."""
    try:
        dt = datetime.fromisoformat(timestamp_str)
        now = datetime.now()
        delta = now - dt
        
        if delta.total_seconds() < 60:
            return "just now"
        elif delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() / 60)
            return f"{mins} min ago"
        elif delta.total_seconds() < 86400:
            hours = int(delta.total_seconds() / 3600)
            return f"{hours}h ago"
        else:
            days = int(delta.total_seconds() / 86400)
            return f"{days}d ago"
    except (ValueError, AttributeError):
        return timestamp_str


def format_cost_indicator(cost_usd: float) -> str:
    """Format cost as color-coded indicator."""
    if cost_usd == 0.0:
        return Color.CONFIDENCE_GREEN("FREE")
    elif cost_usd < 0.001:
        return Color.CONFIDENCE_GREEN(f"${cost_usd:.4f}")
    elif cost_usd < 0.01:
        return Color.ORCHESTRATE_BLUE(f"${cost_usd:.4f}")
    else:
        return Color.WARNING_RED(f"${cost_usd:.4f}")


def print_recent_decisions(decisions: list[dict]) -> None:
    """Print formatted recent routing decisions.

    Args:
        decisions: List of routing decisions
    """
    if not decisions:
        print("No routing decisions yet. Use llm-router to route your first request.")
        return

    print()
    print(Color.ORCHESTRATE_BLUE("=" * 70))
    print(Color.ORCHESTRATE_BLUE(f"  {len(decisions)} Most Recent Routing Decisions"))
    print(Color.ORCHESTRATE_BLUE("=" * 70))
    print()

    for decision in decisions:
        time_ago = format_time_ago(decision.get("timestamp", ""))
        model = decision.get("final_model", "unknown")
        task = decision.get("task_type", "unknown")
        complexity = decision.get("complexity", "unknown")
        cost = decision.get("cost_usd", 0)
        
        # Extract provider from model (e.g., "ollama/gemma4:latest" -> "ollama")
        provider = model.split("/")[0] if "/" in model else model
        
        cost_indicator = format_cost_indicator(cost)
        
        print(
            f"{time_ago:10} {Symbol.ARROW.value} routed to "
            f"{Color.ORCHESTRATE_BLUE(model)} "
            f"({task}/{complexity}) - {cost_indicator}"
        )

    print()


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point for last command.

    Args:
        args: Command line arguments (for testing)

    Returns:
        Exit code (0 on success)
    """
    parser = argparse.ArgumentParser(
        description="Show most recent routing decisions"
    )
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=5,
        help="Number of decisions to show (default: 5)",
    )

    parsed = parser.parse_args(args or [])

    db_path = get_usage_db_path()
    if not db_path.exists():
        print(f"Error: {db_path} not found. Run some routed calls first.")
        return 1

    decisions = fetch_recent_decisions(db_path, count=parsed.count)
    print_recent_decisions(decisions)
    return 0


if __name__ == "__main__":
    exit(main())
