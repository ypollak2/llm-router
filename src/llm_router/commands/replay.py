#!/usr/bin/env python3
"""llm-router replay — Session routing transcript with full context.

Command: uv run llm-router replay [--session SESSION_ID] [--limit N]

Prints a formatted transcript of all routing decisions in a session:
  - Timestamps
  - User prompts
  - Routing decisions (model, confidence, cost)
  - Reasoning
  - Quality scores
  - Session summary (count, cost, savings)

Example output:
  ═══════════════════════════════════════════════════════════
  SESSION REPLAY — May 10, 2026 (14:30–15:45)
  ═══════════════════════════════════════════════════════════

  14:30 You: "Write a function to parse JSON"
    → routed to haiku (code/simple)
    ✓ Confidence: ★★★★★★★★☆☆ 87%
    🧠 Reasoning: Simple standard library task, low risk
    💰 Cost: $0.0001
    ✅ Quality: 97%

  14:31 You: "What's the architecture of this project?"
    → routed to sonnet (analysis/moderate)
    ✓ Confidence: ★★★★★★★★★☆ 92%
    ...

  ───────────────────────────────────────────────────────────
  SUMMARY — 12 routed calls, $0.186 cost, $1.847 saved (90%)
  ───────────────────────────────────────────────────────────
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from llm_router.terminal_style import (
    Color,
    Symbol,
    ConfidenceLevel,
    format_box,
)


def get_usage_db_path() -> Path:
    """Get path to usage.db."""
    return Path.home() / ".llm-router" / "usage.db"


def fetch_routing_decisions(
    db_path: Path,
    limit: int = 100,
    session_id: Optional[str] = None,
) -> list[dict]:
    """Fetch routing decisions from usage.db.

    Args:
        db_path: Path to usage.db
        limit: Maximum decisions to fetch
        session_id: Optional session ID to filter by

    Returns:
        List of routing decision dicts
    """
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM routing_decisions WHERE is_simulated = 0"
        params: list = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in reversed(rows)]  # Reverse for chronological order
    except sqlite3.Error:
        return []


def format_timestamp(timestamp_str: str) -> str:
    """Format ISO timestamp as HH:MM."""
    try:
        dt = datetime.fromisoformat(timestamp_str)
        return dt.strftime("%H:%M")
    except (ValueError, AttributeError):
        return timestamp_str


def format_decision_line(decision: dict) -> str:
    """Format a single routing decision for replay output.

    Args:
        decision: Routing decision dict from DB

    Returns:
        Formatted decision lines
    """
    lines = []

    # Decision header with timestamp
    timestamp = format_timestamp(decision.get("timestamp", ""))
    task = decision.get("task_type", "unknown")
    complexity = decision.get("task_complexity", "unknown")

    lines.append(
        f"{timestamp} {Symbol.ARROW.value} "
        f"routed to {Color.ORCHESTRATE_BLUE(decision.get('model', 'unknown'))} "
        f"({task}/{complexity})"
    )

    # Confidence
    confidence = decision.get("confidence_percent", 0)
    level = ConfidenceLevel.MEDIUM
    stars = level.stars(confidence)
    lines.append(
        f"    {Symbol.STAR_FULL.value} Confidence: {stars} {int(confidence)}%"
    )

    # Reasoning
    reason = decision.get("reason_code") or decision.get("reason", "N/A")
    lines.append(f"    {Symbol.BRAIN.value} Reasoning: {reason}")

    # Cost
    cost = decision.get("cost_usd", 0)
    lines.append(
        f"    {Symbol.MONEY.value} Cost: {Color.CONFIDENCE_GREEN(f'${cost:.4f}')}"
    )

    # Quality score (if available)
    quality = decision.get("quality_score")
    if quality is not None:
        quality_pct = int(quality * 100)
        if quality_pct >= 90:
            quality_str = f"{Symbol.SUCCESS.value} Excellent ({quality_pct}%)"
        elif quality_pct >= 80:
            quality_str = f"{Symbol.CHECK.value} Good ({quality_pct}%)"
        else:
            quality_str = f"{Symbol.WARNING.value} Marginal ({quality_pct}%)"
        lines.append(f"    {quality_str}")

    return "\n".join(lines)


def print_session_replay(
    decisions: list[dict],
    session_id: Optional[str] = None,
) -> None:
    """Print formatted session replay.

    Args:
        decisions: List of routing decisions
        session_id: Optional session ID for display
    """
    if not decisions:
        print("No routed calls found in this session.")
        return

    # Header
    first_decision = decisions[0]
    last_decision = decisions[-1]

    start_time = format_timestamp(first_decision.get("timestamp", ""))
    end_time = format_timestamp(last_decision.get("timestamp", ""))

    session_label = f" ({start_time}–{end_time})" if start_time and end_time else ""
    header = f"SESSION REPLAY — May 10, 2026{session_label}"

    print()
    print(Color.ORCHESTRATE_BLUE("═" * 70))
    print(Color.ORCHESTRATE_BLUE(f"  {header}"))
    print(Color.ORCHESTRATE_BLUE("═" * 70))
    print()

    # Print each decision
    for decision in decisions:
        print(format_decision_line(decision))
        print()

    # Summary
    total_cost = sum(d.get("cost_usd", 0) for d in decisions)
    total_saved = sum(d.get("saved_usd", 0) for d in decisions)

    summary_lines = [
        f"Total routed: {len(decisions)} calls",
        f"Cost: {Color.CONFIDENCE_GREEN(f'${total_cost:.2f}')}",
    ]

    if total_saved > 0:
        savings_pct = (total_saved / (total_cost + total_saved) * 100) if (total_cost + total_saved) > 0 else 0
        summary_lines.append(
            f"{Color.CONFIDENCE_GREEN(f'Saved: ${total_saved:.2f} ({int(savings_pct)}%)')}"
        )

    print(Color.ORCHESTRATE_BLUE("─" * 70))
    print("SUMMARY")
    print(Color.ORCHESTRATE_BLUE("─" * 70))
    for line in summary_lines:
        print(f"  {line}")
    print()


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point for replay command.

    Args:
        args: Command line arguments (for testing)

    Returns:
        Exit code (0 on success)
    """
    parser = argparse.ArgumentParser(
        description="Show routing decisions from this session"
    )
    parser.add_argument(
        "--session",
        help="Session ID to filter by",
        default=None,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum decisions to show (default: 100)",
    )

    parsed = parser.parse_args(args or [])

    db_path = get_usage_db_path()
    if not db_path.exists():
        print(f"Error: {db_path} not found. Run some routed calls first.")
        return 1

    decisions = fetch_routing_decisions(
        db_path,
        limit=parsed.limit,
        session_id=parsed.session,
    )

    print_session_replay(decisions, parsed.session)
    return 0


if __name__ == "__main__":
    exit(main())
