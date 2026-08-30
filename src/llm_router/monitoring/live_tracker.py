"""Live session snapshot tracking — captures hourly snapshots and displays progress.

Runs during active sessions to track routing accuracy trends and gap emergence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from llm_router.monitoring.periodic import (
    take_session_snapshot,
    save_session_snapshot,
    load_session_snapshots,
)
from llm_router.retrospective import (
    get_session_window,
)


async def check_and_capture_hourly_snapshot() -> dict | None:
    """Check if it's time for a new hourly snapshot and capture it.

    Returns:
        Snapshot dict if captured, None if not yet time for next snapshot
    """
    try:
        # Get current session window
        start_dt, end_dt = get_session_window()
        
        # Calculate which hour we're in
        now_ts = datetime.now(timezone.utc).timestamp()
        start_ts = start_dt.timestamp()
        hours_elapsed = (now_ts - start_ts) / 3600
        current_hour = max(1, int(hours_elapsed) + 1)
        
        # Check if we already have a snapshot for this hour
        snapshots = load_session_snapshots()
        if snapshots and snapshots[-1].get("hour") == current_hour:
            return None  # Already captured for this hour
        
        # Time to capture a new snapshot
        snapshot = await take_session_snapshot(start_dt, end_dt, current_hour)
        save_session_snapshot(snapshot)
        
        return snapshot
    except Exception:
        return None


def get_live_trend_indicator() -> str:
    """Get a compact trend indicator for display during session.

    Returns:
        Formatted trend string like "↑ 95% (Hour 2)" or "↓ 88% ⚠ gaps emerging"
    """
    try:
        snapshots = load_session_snapshots()
        if not snapshots:
            return ""
        
        current = snapshots[-1]
        current_hour = current.get("hour", 1)
        # GH#56/#60: "accuracy" is None (unmeasured), not 0.0/1.0, when a
        # snapshot's hour saw no routing decisions — `.get(..., 1.0)` does not
        # catch that because the key is present with value None, so `* 100`
        # would raise TypeError. Render "n/a" and skip the trend comparison
        # rather than resurrecting a fake accuracy figure.
        raw_accuracy = current.get("facts", {}).get("accuracy")
        accuracy_str = "n/a" if raw_accuracy is None else f"{int(raw_accuracy * 100)}%"
        gaps = current.get("gap_count", 0)

        # Show trend indicator
        if len(snapshots) > 1:
            prev_raw_accuracy = snapshots[-2].get("facts", {}).get("accuracy")
            if raw_accuracy is None or prev_raw_accuracy is None:
                indicator = f"📊 {accuracy_str} (H{current_hour})"
            else:
                accuracy = int(raw_accuracy * 100)
                prev_accuracy = int(prev_raw_accuracy * 100)
                if accuracy > prev_accuracy:
                    indicator = f"↑ {accuracy}% (H{current_hour})"
                elif accuracy < prev_accuracy:
                    indicator = f"↓ {accuracy}% (H{current_hour})"
                else:
                    indicator = f"→ {accuracy}% (H{current_hour})"
        else:
            indicator = f"📊 {accuracy_str} (H{current_hour})"
        
        # Add gap warning
        if gaps > 0:
            indicator += f" ⚠ {gaps} gap{'s' if gaps != 1 else ''}"
        
        return indicator
    except Exception:
        return ""


def display_hourly_progress() -> str:
    """Display current session progress for mid-session checks.

    Returns:
        Formatted progress string
    """
    try:
        snapshots = load_session_snapshots()
        if not snapshots:
            return ""
        
        current = snapshots[-1]
        current_hour = current.get("hour", 1)
        facts = current.get("facts", {})
        
        # Build compact progress line
        calls = facts.get("total_calls", 0)
        raw_accuracy = facts.get("accuracy")
        accuracy_str = "n/a" if raw_accuracy is None else f"{int(raw_accuracy * 100)}%"
        gaps = current.get("gap_count", 0)
        saved = facts.get("total_saved", 0)

        line = f"【Hour {current_hour}】 {calls} calls · {accuracy_str} accuracy"
        if gaps > 0:
            line += f" · {gaps} gap{'s' if gaps != 1 else ''}"
        line += f" · ${saved:.2f} saved"
        
        return line
    except Exception:
        return ""


def get_trend_pressure() -> float:
    """Get trend-based pressure modifier for model selection.
    
    Analyzes accuracy trend across snapshots:
    - Returns 0.0 when no snapshots or trend is flat/improving
    - Returns 0.1–0.3 when accuracy trend is declining
    
    The pressure is applied in model_selector to soft-escalate model tier
    when quality is declining, independent of budget pressure.
    
    Returns:
        Trend pressure value (0.0–0.3)
    """
    try:
        snapshots = load_session_snapshots()
        if not snapshots or len(snapshots) < 2:
            return 0.0  # Not enough data
        
        # Compare last 2 snapshots. GH#56/#60: accuracy is None (unmeasured)
        # rather than defaulted when a snapshot's hour saw no decisions —
        # `.get(..., 1.0)` doesn't apply because the key is present with
        # value None. Treat unmeasured as "no signal" rather than inventing
        # a trend from a fake 1.0.
        current_accuracy = snapshots[-1].get("facts", {}).get("accuracy")
        prev_accuracy = snapshots[-2].get("facts", {}).get("accuracy")
        if current_accuracy is None or prev_accuracy is None:
            return 0.0  # unmeasured — no basis for escalation pressure

        # Calculate trend
        trend = current_accuracy - prev_accuracy
        
        if trend >= 0:
            return 0.0  # Flat or improving
        
        # Declining — escalate pressure based on magnitude
        # -0.05 drop → 0.1 pressure
        # -0.10 drop → 0.2 pressure
        # -0.15+ drop → 0.3 pressure
        pressure = min(0.3, abs(trend) * 2)  # rough mapping: -0.15 → 0.3
        return pressure
    except Exception:
        return 0.0
