#!/usr/bin/env python3
"""llm-router snapshot — Mid-session monitoring and status check.

Command: uv run llm-router snapshot [--date YYYY-MM-DD] [--compact]

Shows current session facts and hourly trends:
  - Calls, cost, savings, accuracy
  - Gap emergence timeline (when problems started)
  - Accuracy degradation warning
  - Recurring gap patterns

Example output:
  ═══════════════════════════════════════════════════════════
  SESSION SNAPSHOT — Today (Hour 2)
  ═══════════════════════════════════════════════════════════

  【Current Status】
    Calls: 23  |  Cost: $1.23  |  Saved: $45.67 (97%)
    Accuracy: 91%  |  Gaps: 1  |  Actions: 1

  【Hour 1】
    Calls: 12  |  Accuracy: 93%  |  Gaps: 0
  【Hour 2】
    Calls: 11  |  Accuracy: 89%  |  Gaps: 1 ↓

  【Trends】
    Accuracy: 93% → 89% (-4pp over 2h)
    Gap emergence: Hour 2 (security_review)

  ⚠ Accuracy degrading - consider switching profiles

  ───────────────────────────────────────────────────────────
"""

import argparse
from datetime import datetime
from pathlib import Path

from llm_router.monitoring.periodic import (
    load_session_snapshots,
    analyze_session_trends,
    format_trend_summary,
    get_current_snapshot,
)
from llm_router.terminal_style import Color, Symbol


def format_snapshot_status(snapshot: dict) -> str:
    """Format current snapshot status.

    Args:
        snapshot: Snapshot dict from load_session_snapshots()

    Returns:
        Formatted status lines
    """
    lines = []

    facts = snapshot.get("facts", {})
    hour = snapshot.get("hour", 1)

    lines.append(Color.ORCHESTRATE_BLUE("【Current Status】"))
    lines.append(
        f"  Calls: {facts.get('total_calls', 0)}  "
        f"| Cost: ${facts.get('total_cost', 0):.2f}  "
        f"| Saved: ${facts.get('total_saved', 0):.2f} ({int(facts.get('total_saved', 0) / max(1, facts.get('total_saved', 1) + facts.get('total_cost', 0)) * 100)}%)"
    )

    accuracy_pct = int(facts.get("accuracy", 1.0) * 100)
    lines.append(
        f"  Accuracy: {accuracy_pct}%  "
        f"| Gaps: {snapshot.get('gap_count', 0)}  "
        f"| Actions: {snapshot.get('action_count', 0)}"
    )

    return "\n".join(lines)


def format_hourly_snapshots(snapshots: list[dict]) -> str:
    """Format hourly breakdown of snapshots.

    Args:
        snapshots: List of snapshots from load_session_snapshots()

    Returns:
        Formatted hourly lines
    """
    if len(snapshots) <= 1:
        return ""

    lines = [Color.ORCHESTRATE_BLUE("【Hourly Breakdown】")]

    for i, snapshot in enumerate(snapshots):
        hour = snapshot.get("hour", i + 1)
        facts = snapshot.get("facts", {})
        gap_count = snapshot.get("gap_count", 0)

        # Detect trends
        prev_gap_count = snapshots[i - 1].get("gap_count", 0) if i > 0 else gap_count
        gap_indicator = ""
        if gap_count > prev_gap_count:
            gap_indicator = f" ↑ (+{gap_count - prev_gap_count} gaps)"
        elif gap_count > 0 and prev_gap_count == 0:
            gap_indicator = " NEW"

        accuracy_pct = int(facts.get("accuracy", 1.0) * 100)

        lines.append(
            f"  Hour {hour}: Calls: {facts.get('total_calls', 0)}  "
            f"| Accuracy: {accuracy_pct}%  "
            f"| Gaps: {gap_count}{gap_indicator}"
        )

    return "\n".join(lines)


def print_session_snapshot(
    date_str: str = "",
    compact: bool = False,
) -> None:
    """Print formatted session snapshot.

    Args:
        date_str: Date in YYYY-MM-DD format (default: today)
        compact: If True, print compact 2-line format for mid-session checks
    """
    snapshots = load_session_snapshots(date_str)

    if not snapshots:
        if compact:
            print("No snapshots yet (< 1 hour)")
        else:
            print("No snapshots recorded for this session.")
        return

    current_snapshot = snapshots[-1]
    hour = current_snapshot.get("hour", 1)

    # Compact mode (2 lines for mid-session checks)
    if compact:
        facts = current_snapshot.get("facts", {})
        accuracy_pct = int(facts.get("accuracy", 1.0) * 100)
        gaps = current_snapshot.get("gap_count", 0)
        trend = ""
        if len(snapshots) > 1:
            prev_accuracy = int(snapshots[-2].get("facts", {}).get("accuracy", 1.0) * 100)
            if accuracy_pct < prev_accuracy:
                trend = f" ↓ ({accuracy_pct}% vs {prev_accuracy}%)"
        gap_str = f", {gaps} gap{'s' if gaps != 1 else ''}" if gaps > 0 else ""
        print(
            f"Session (Hour {hour}): {facts.get('total_calls', 0)} calls, "
            f"{accuracy_pct}% accuracy{gap_str}{trend}"
        )
        return

    # Full format
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    header = f"SESSION SNAPSHOT — {date_str} (Hour {hour})"
    print()
    print(Color.ORCHESTRATE_BLUE("═" * 70))
    print(Color.ORCHESTRATE_BLUE(f"  {header}"))
    print(Color.ORCHESTRATE_BLUE("═" * 70))
    print()

    # Current status
    print(format_snapshot_status(current_snapshot))
    print()

    # Hourly breakdown
    hourly_output = format_hourly_snapshots(snapshots)
    if hourly_output:
        print(hourly_output)
        print()

    # Trend analysis
    if len(snapshots) > 1:
        trends = analyze_session_trends(snapshots)
        if trends:
            print(Color.ORCHESTRATE_BLUE("【Trends】"))
            trend_output = format_trend_summary(trends)
            for line in trend_output.split("\n"):
                if line.strip():
                    print(f"  {line}")
            print()

    print(Color.ORCHESTRATE_BLUE("─" * 70))


def main(args: list[str] | None = None) -> int:
    """Main entry point for snapshot command.

    Args:
        args: Command line arguments (for testing)

    Returns:
        Exit code (0 on success)
    """
    parser = argparse.ArgumentParser(
        description="Show mid-session monitoring snapshot and trends"
    )
    parser.add_argument(
        "--date",
        help="Date to show snapshots for (YYYY-MM-DD, default: today)",
        default="",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact 2-line format",
    )

    parsed = parser.parse_args(args or [])

    print_session_snapshot(
        date_str=parsed.date,
        compact=parsed.compact,
    )

    return 0


if __name__ == "__main__":
    exit(main())
