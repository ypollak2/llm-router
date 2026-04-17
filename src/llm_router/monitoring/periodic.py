"""Mid-session periodic snapshots and trend analysis.

Takes hourly snapshots of session retrospectives to track:
- Accuracy trends (is it improving or degrading?)
- Gap emergence patterns (when did problems start?)
- Action accumulation (are issues recurring?)

Snapshots are stored locally and analyzed at session end to show trends.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from llm_router.retrospective import (
    fetch_session_decisions,
    fetch_session_corrections,
    analyze_facts,
    analyze_gaps,
    classify_root_causes,
    generate_actions,
)


SNAPSHOT_DIR = Path.home() / ".llm-router" / "session_snapshots"


def _get_session_hour() -> int:
    """Calculate which hour of the session we're in.

    Returns:
        Hour number (1-based) since session started
    """
    try:
        session_start_file = Path.home() / ".llm-router" / "session_start.txt"
        if not session_start_file.exists():
            return 1

        start_ts = float(session_start_file.read_text().strip())
        now_ts = datetime.now(timezone.utc).timestamp()
        hours_elapsed = (now_ts - start_ts) / 3600
        return max(1, int(hours_elapsed) + 1)
    except (ValueError, OSError):
        return 1


def _get_today_snapshot_filename(hour: int) -> str:
    """Get standardized snapshot filename for today."""
    now = datetime.now()
    return f"{now.strftime('%Y-%m-%d')}-{hour:02d}h.json"


async def take_session_snapshot(
    start_dt: datetime, end_dt: datetime, hour_num: int
) -> dict:
    """Take a snapshot of current session state at hourly intervals.

    Analyzes routing decisions since session start, captures:
    - Current facts (calls, cost, accuracy)
    - Gaps detected (problems)
    - Root causes (why problems happened)
    - Actions (what to improve)

    Args:
        start_dt: Session start datetime
        end_dt: Current time
        hour_num: Which hour of the session (1-based)

    Returns:
        Snapshot dict with timestamp, hour, facts, gaps, actions
    """
    # Fetch decisions since session start
    decisions = fetch_session_decisions(start_dt, end_dt)
    corrections = fetch_session_corrections(start_dt, end_dt)

    # Run same analysis as retrospective
    facts = analyze_facts(decisions, corrections)
    gaps = analyze_gaps(decisions, corrections)
    causes = classify_root_causes(gaps)
    actions = generate_actions(causes, corrections, decisions)

    snapshot = {
        "hour": hour_num,
        "timestamp": end_dt.isoformat(),
        "facts": {
            "total_calls": facts.get("total_calls", 0),
            "total_cost": facts.get("total_cost", 0.0),
            "total_saved": facts.get("total_saved", 0.0),
            "accuracy": facts.get("classification_accuracy", 1.0),
            "duration_min": facts.get("duration_min", 0),
        },
        "gap_count": len(gaps),
        "action_count": len(actions),
        "top_gap": gaps[0].get("task_type", "none") if gaps else "none",
    }

    return snapshot


def save_session_snapshot(snapshot: dict) -> Path:
    """Save snapshot to disk.

    File: ~/.llm-router/session_snapshots/YYYY-MM-DD-HHh.json

    Args:
        snapshot: Snapshot dict from take_session_snapshot()

    Returns:
        Path to saved file
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    hour = snapshot.get("hour", 1)
    filename = _get_today_snapshot_filename(hour)
    filepath = SNAPSHOT_DIR / filename

    filepath.write_text(json.dumps(snapshot, indent=2))
    return filepath


def load_session_snapshots(date_str: str = "") -> list[dict]:
    """Load all snapshots from a session.

    Args:
        date_str: Date in YYYY-MM-DD format (default: today)

    Returns:
        List of snapshot dicts, sorted by hour
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    if not SNAPSHOT_DIR.exists():
        return []

    snapshots = []
    for snapshot_file in sorted(SNAPSHOT_DIR.glob(f"{date_str}-*.json")):
        try:
            snapshots.append(json.loads(snapshot_file.read_text()))
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by hour
    return sorted(snapshots, key=lambda s: s.get("hour", 0))


def analyze_session_trends(snapshots: list[dict]) -> dict:
    """Analyze trends across hourly snapshots.

    Detects:
    - Accuracy improvement/degradation
    - Gap emergence timeline
    - When did problems start?
    - Accelerating or stabilizing issues?

    Args:
        snapshots: List of snapshots from load_session_snapshots()

    Returns:
        Trend analysis dict
    """
    if not snapshots:
        return {
            "trend_type": "none",
            "accuracy_change": 0.0,
            "gap_emergence": [],
            "concerning": False,
        }

    # Extract accuracy and gap counts
    accuracies = [s["facts"]["accuracy"] for s in snapshots]
    gap_counts = [s["gap_count"] for s in snapshots]

    first_accuracy = accuracies[0]
    last_accuracy = accuracies[-1]
    accuracy_change = last_accuracy - first_accuracy  # Negative = degrading

    # Detect gap emergence
    gap_emergence = []
    for i, count in enumerate(gap_counts):
        if i == 0 and count > 0:
            gap_emergence.append({"hour": snapshots[i]["hour"], "gap_count": count})
        elif i > 0 and count > gap_counts[i - 1]:
            gap_emergence.append({
                "hour": snapshots[i]["hour"],
                "gap_count": count,
                "delta": count - gap_counts[i - 1],
            })

    # Determine trend type
    if accuracy_change > 0.05:
        trend_type = "improving"
    elif accuracy_change < -0.05:
        trend_type = "degrading"
    else:
        trend_type = "stable"

    # Flag concerning patterns
    concerning = trend_type == "degrading" or len(gap_emergence) >= 2

    return {
        "trend_type": trend_type,
        "accuracy_change": accuracy_change,
        "first_accuracy": first_accuracy,
        "last_accuracy": last_accuracy,
        "gap_emergence": gap_emergence,
        "concerning": concerning,
        "snapshot_count": len(snapshots),
    }


def format_trend_summary(trend: dict) -> str:
    """Format trend analysis for display.

    Args:
        trend: Trend analysis dict from analyze_session_trends()

    Returns:
        Formatted string for console output
    """
    if trend["snapshot_count"] == 0:
        return "  No snapshots recorded (< 1 hour)"

    lines = []

    # Accuracy trend
    acc_pct_start = int(trend["first_accuracy"] * 100)
    acc_pct_end = int(trend["last_accuracy"] * 100)
    acc_change = int(trend["accuracy_change"] * 100)
    acc_symbol = "↑" if acc_change > 0 else "↓" if acc_change < 0 else "→"

    lines.append(
        f"  Accuracy trend: {acc_pct_start}% {acc_symbol} {acc_pct_end}% "
        f"({acc_change:+d}pp over {trend['snapshot_count']}h)"
    )

    # Gap emergence timeline
    if trend["gap_emergence"]:
        lines.append(f"  Gap emergence:")
        for emergence in trend["gap_emergence"]:
            if "delta" in emergence:
                lines.append(
                    f"    Hour {emergence['hour']}: {emergence['gap_count']} gaps "
                    f"(+{emergence['delta']})"
                )
            else:
                lines.append(
                    f"    Hour {emergence['hour']}: First gap detected ({emergence['gap_count']})"
                )

    # Warning if concerning
    if trend["concerning"]:
        if trend["trend_type"] == "degrading":
            lines.append(f"  ⚠️  Accuracy degrading - profile may be stale")
        if len(trend["gap_emergence"]) >= 2:
            lines.append(f"  ⚠️  Recurring gaps - consider adjusting routing rules")

    return "\n".join(lines)


def get_current_snapshot() -> dict:
    """Get or create snapshot for current hour.

    Returns:
        Snapshot dict for this hour (may be from disk or newly created)
    """
    # For now, just return the last saved snapshot
    snapshots = load_session_snapshots()
    if snapshots:
        return snapshots[-1]

    # Return empty snapshot if none exist
    return {
        "hour": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "facts": {"accuracy": 1.0, "total_calls": 0},
        "gap_count": 0,
        "action_count": 0,
    }


def cleanup_old_snapshots(keep_days: int = 7) -> int:
    """Clean up snapshots older than N days.

    Args:
        keep_days: How many days of snapshots to keep

    Returns:
        Number of files deleted
    """
    if not SNAPSHOT_DIR.exists():
        return 0

    cutoff = datetime.now().timestamp() - (keep_days * 86400)
    deleted = 0

    for snapshot_file in SNAPSHOT_DIR.glob("*.json"):
        if snapshot_file.stat().st_mtime < cutoff:
            try:
                snapshot_file.unlink()
                deleted += 1
            except OSError:
                pass

    return deleted
