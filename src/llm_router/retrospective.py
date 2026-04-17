"""IAF-style session retrospective engine for llm-router.

Performs 5-step debrief analysis:
1. FACTS — What happened (neutral, data-driven)
2. EXPECTATIONS vs REALITY — Where routing fell short
3. ROOT CAUSE — Why (classifier error, stale profile, etc.)
4. ACTIONS — Concrete directives to improve next session
5. MEMORY — Binding directives written to files

All analysis data comes from existing usage.db — no new data collection needed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from llm_router.types import TaskType


# ── Constants ──────────────────────────────────────────────────────────────

RETROSPECT_DIR = Path.home() / ".llm-router" / "retrospectives"
DIRECTIVES_FILE = Path.home() / ".llm-router" / "directives.md"
SESSION_START_FILE = Path.home() / ".llm-router" / "session_start.txt"
DB_PATH = Path.home() / ".llm-router" / "usage.db"


# ── Session Window Detection ───────────────────────────────────────────────

def get_session_window() -> tuple[datetime, datetime]:
    """Detect session start and end times.

    Reads SESSION_START_FILE for the session start. Falls back to 2 hours ago
    if file is missing or unreadable.

    Returns:
        (start_dt, end_dt) tuple with UTC datetimes
    """
    end_dt = datetime.now(timezone.utc)

    if SESSION_START_FILE.exists():
        try:
            ts = float(SESSION_START_FILE.read_text().strip())
            start_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return start_dt, end_dt
        except (ValueError, OSError):
            pass

    # Fallback: last 2 hours
    return end_dt - timedelta(hours=2), end_dt


def _iso_from_timestamp(ts: float) -> str:
    """Convert Unix timestamp to ISO 8601 string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── Data Collection ────────────────────────────────────────────────────────

def fetch_session_decisions(
    start: datetime, end: datetime
) -> list[dict]:
    """Fetch all routing decisions in the session window.

    Args:
        start: Session start datetime (UTC)
        end: Session end datetime (UTC)

    Returns:
        List of routing decision dicts from routing_decisions table
    """
    if not DB_PATH.exists():
        return []

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        start_iso = start.isoformat()
        end_iso = end.isoformat()

        rows = conn.execute(
            """
            SELECT * FROM routing_decisions
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def fetch_session_corrections(
    start: datetime, end: datetime
) -> list[dict]:
    """Fetch all manual routing corrections (llm_rate feedback) in the session.

    Note: Currently no rate table exists; this is a placeholder for future
    integration with the quality feedback system.

    Args:
        start: Session start datetime (UTC)
        end: Session end datetime (UTC)

    Returns:
        List of correction dicts (empty until rate table is implemented)
    """
    # TODO: Implement once quality.py has a rate/feedback table
    return []


# ── IAF Debrief Steps ──────────────────────────────────────────────────────

def analyze_facts(decisions: list[dict], corrections: list[dict]) -> dict:
    """Step 1: Collect neutral, data-driven facts about the session.

    Args:
        decisions: Routing decisions
        corrections: Manual corrections

    Returns:
        Dict with: total_calls, total_cost, total_saved, duration_min,
                   correction_count, task_distribution, model_distribution,
                   avg_confidence, classification_accuracy
    """
    if not decisions:
        return {
            "total_calls": 0,
            "total_cost": 0.0,
            "total_saved": 0.0,
            "duration_min": 0,
            "correction_count": len(corrections),
            "task_distribution": {},
            "model_distribution": {},
            "avg_confidence": 0.0,
            "classification_accuracy": 1.0,
        }

    # Duration
    start_ts = datetime.fromisoformat(decisions[0]["timestamp"])
    end_ts = datetime.fromisoformat(decisions[-1]["timestamp"])
    duration_min = int((end_ts - start_ts).total_seconds() / 60)

    # Cost and savings
    total_cost = sum(d.get("cost_usd", 0) or 0 for d in decisions)
    total_saved = sum(d.get("saved_usd", 0) or 0 for d in decisions)

    # Distribution by task type and model
    tasks = {}
    models = {}
    for d in decisions:
        task = d.get("task_type", "unknown")
        model = d.get("final_model", "unknown")
        tasks[task] = tasks.get(task, 0) + 1
        models[model] = models.get(model, 0) + 1

    # Average confidence
    confidences = [d.get("classifier_confidence", 0) or 0 for d in decisions]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    # Classification accuracy (based on correction ratio)
    accuracy = 1.0 - (len(corrections) / len(decisions)) if decisions else 1.0
    accuracy = max(0.0, min(1.0, accuracy))

    return {
        "total_calls": len(decisions),
        "total_cost": total_cost,
        "total_saved": total_saved,
        "duration_min": duration_min,
        "correction_count": len(corrections),
        "task_distribution": tasks,
        "model_distribution": models,
        "avg_confidence": avg_conf,
        "classification_accuracy": accuracy,
    }


def analyze_gaps(decisions: list[dict], corrections: list[dict]) -> list[dict]:
    """Step 2: Identify where routing fell short of expectations.

    Flags:
    - LOW_CONFIDENCE: classifier_confidence < 0.70
    - LOW_QUALITY: judge_score < 0.85 (requires feedback system)
    - MANUAL_OVERRIDE: decision in corrections list
    - BUDGET_DOWNGRADE: final_model != recommended_model
    - PROVIDER_FAILURE: success = 0

    Args:
        decisions: Routing decisions
        corrections: Manual corrections

    Returns:
        List of gap dicts with task_type, model, flags, reason
    """
    gaps = []
    correction_hashes = {c.get("decision_id") for c in corrections}

    for d in decisions:
        gap_flags = []
        reason = ""

        # Check confidence
        conf = d.get("classifier_confidence", 0) or 0
        if conf < 0.70:
            gap_flags.append("LOW_CONFIDENCE")
            reason = f"classifier confidence only {conf*100:.0f}%"

        # Check quality score (if available)
        judge = d.get("judge_score")
        if judge is not None and judge < 0.85:
            gap_flags.append("LOW_QUALITY")
            reason = f"quality score {judge*100:.0f}% below threshold"

        # Check for manual correction
        if d.get("id") in correction_hashes:
            gap_flags.append("MANUAL_OVERRIDE")
            reason = "user escalated or changed routing"

        # Check for budget downshift
        if d.get("final_model") != d.get("recommended_model"):
            gap_flags.append("BUDGET_DOWNGRADE")
            reason = "budget pressure forced downshift"

        # Check for provider failure
        if not d.get("success", 1):
            gap_flags.append("PROVIDER_FAILURE")
            reason = "provider call failed"

        if gap_flags:
            gaps.append({
                "decision_id": d.get("id"),
                "timestamp": d.get("timestamp"),
                "task_type": d.get("task_type", "unknown"),
                "recommended_model": d.get("recommended_model", "?"),
                "final_model": d.get("final_model", "?"),
                "confidence": conf,
                "flags": gap_flags,
                "reason": reason,
            })

    return gaps


def classify_root_causes(gaps: list[dict]) -> list[dict]:
    """Step 3: Classify root causes behind each gap.

    Root causes:
    - CLASSIFIER_ERROR: model wasn't detected from task keywords
    - PROFILE_STALE: repeated overrides for same task_type indicate profile mismatch
    - NEW_TASK_TYPE: task_type has no prior directives
    - PROVIDER_FAILURE: external provider unavailable
    - BUDGET_PRESSURE: real-time subscription/API limits forced downgrade

    Args:
        gaps: List of gaps from analyze_gaps()

    Returns:
        List of dicts with gap_id, root_cause, confidence (0-3 scale)
    """
    causes = []

    # Count override frequency per task type
    override_counts = {}
    for gap in gaps:
        if "MANUAL_OVERRIDE" in gap["flags"]:
            task = gap["task_type"]
            override_counts[task] = override_counts.get(task, 0) + 1

    for gap in gaps:
        task = gap["task_type"]
        flags = gap["flags"]

        # Assign root cause with confidence
        # Note: Order matters — check recurring patterns (PROFILE_STALE) before single-gap signals
        if "PROVIDER_FAILURE" in flags:
            causes.append({
                "gap_id": gap["decision_id"],
                "task_type": task,
                "root_cause": "PROVIDER_FAILURE",
                "confidence": 3,  # Certain
                "evidence": "Provider call returned error",
            })
        elif "MANUAL_OVERRIDE" in flags:
            # Check if this task type has repeated overrides (PROFILE_STALE is strong signal)
            count = override_counts.get(task, 0)
            if count >= 2:
                causes.append({
                    "gap_id": gap["decision_id"],
                    "task_type": task,
                    "root_cause": "PROFILE_STALE",
                    "confidence": 3,  # Highest — recurring pattern detected
                    "evidence": f"{count} overrides for {task} — profile mismatch",
                })
            else:
                causes.append({
                    "gap_id": gap["decision_id"],
                    "task_type": task,
                    "root_cause": "NEW_TASK_TYPE",
                    "confidence": 1,  # Medium (unclear)
                    "evidence": "Single override — possibly new task type",
                })
        elif "BUDGET_DOWNGRADE" in flags:
            causes.append({
                "gap_id": gap["decision_id"],
                "task_type": task,
                "root_cause": "BUDGET_PRESSURE",
                "confidence": 2,  # High
                "evidence": "Budget pressure forced model downshift",
            })
        elif "LOW_CONFIDENCE" in flags and "LOW_QUALITY" in flags:
            # Both low confidence AND low quality = classifier failed
            causes.append({
                "gap_id": gap["decision_id"],
                "task_type": task,
                "root_cause": "CLASSIFIER_ERROR",
                "confidence": 3,  # Certain
                "evidence": "Low confidence + low quality = classifier error",
            })
        elif "LOW_CONFIDENCE" in flags:
            causes.append({
                "gap_id": gap["decision_id"],
                "task_type": task,
                "root_cause": "CLASSIFIER_ERROR",
                "confidence": 2,  # High
                "evidence": f"Classifier confidence {gap['confidence']*100:.0f}%",
            })
        elif "LOW_QUALITY" in flags:
            causes.append({
                "gap_id": gap["decision_id"],
                "task_type": task,
                "root_cause": "CLASSIFIER_ERROR",
                "confidence": 1,  # Medium (quality is weaker signal)
                "evidence": f"Quality score below threshold",
            })
        else:
            causes.append({
                "gap_id": gap["decision_id"],
                "task_type": task,
                "root_cause": "UNKNOWN",
                "confidence": 0,
                "evidence": "No clear root cause detected",
            })

    return causes


def generate_actions(
    root_causes: list[dict], corrections: list[dict], decisions: list[dict]
) -> list[dict]:
    """Step 4: Generate concrete directives to improve next session.

    Action types:
    - ROUTING_RULE: escalate/downgrade a task_type permanently
    - KEYWORD_ENHANCEMENT: add keywords to classifier for task_type
    - PROFILE_UPDATE: adjust routing profile based on overrides

    Each action includes:
    - type: Action type
    - task_type: Which task is affected
    - directive: What to change
    - confidence: How certain (1-3 scale)
    - trigger_count: How many times this pattern occurred

    Args:
        root_causes: Root cause analysis from classify_root_causes()
        corrections: Manual corrections
        decisions: Original routing decisions

    Returns:
        List of action dicts
    """
    actions = []

    # Count root causes by type and task
    cause_counts = {}
    for cause in root_causes:
        key = (cause["root_cause"], cause["task_type"])
        cause_counts[key] = cause_counts.get(key, 0) + 1

    # Generate actions based on recurring patterns
    for (root_cause, task_type), count in cause_counts.items():
        if root_cause == "CLASSIFIER_ERROR" and count >= 2:
            # Repeated classifier errors for same task — escalate
            actions.append({
                "type": "ROUTING_RULE",
                "task_type": task_type,
                "directive": f"escalate_{task_type}_to_sonnet",
                "confidence": count,
                "evidence": f"{count} classifier errors for {task_type}",
            })
        elif root_cause == "PROFILE_STALE" and count >= 2:
            # Repeated overrides — profile is stale
            actions.append({
                "type": "PROFILE_UPDATE",
                "task_type": task_type,
                "directive": f"move_{task_type}_from_budget_to_balanced",
                "confidence": count,
                "evidence": f"{count} manual overrides indicate profile mismatch",
            })
        elif root_cause == "BUDGET_PRESSURE" and count >= 1:
            # Budget pressure — note for future optimization
            actions.append({
                "type": "BUDGET_ALERT",
                "task_type": task_type,
                "directive": f"monitor_{task_type}_cost",
                "confidence": 1,
                "evidence": "Budget pressure forced downshift",
            })

    return actions


def build_retrospective(
    start: datetime, end: datetime, decisions: list[dict], corrections: list[dict]
) -> dict:
    """Assemble complete IAF debrief report.

    Args:
        start: Session start datetime
        end: Session end datetime
        decisions: Routing decisions
        corrections: Manual corrections

    Returns:
        Complete retrospective dict
    """
    facts = analyze_facts(decisions, corrections)
    gaps = analyze_gaps(decisions, corrections)
    causes = classify_root_causes(gaps)
    actions = generate_actions(causes, corrections, decisions)

    return {
        "session_start": start.isoformat(),
        "session_end": end.isoformat(),
        "facts": facts,
        "gaps": gaps,
        "root_causes": causes,
        "actions": actions,
        "timestamp_generated": datetime.now(timezone.utc).isoformat(),
    }


# ── File I/O ───────────────────────────────────────────────────────────────

def write_retrospective_file(retro: dict, session_id: str = "") -> Path:
    """Write retrospective to a dated markdown file.

    File location: ~/.llm-router/retrospectives/YYYY-MM-DD-HHMMSS.md

    Args:
        retro: Retrospective dict from build_retrospective()
        session_id: Optional session identifier

    Returns:
        Path to written file
    """
    RETROSPECT_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    filename = now.strftime("%Y-%m-%d-%H%M%S") + ".md"
    filepath = RETROSPECT_DIR / filename

    facts = retro.get("facts", {})
    gaps = retro.get("gaps", [])
    causes = retro.get("root_causes", [])
    actions = retro.get("actions", [])

    content = f"""---
name: Session Retrospective {now.strftime('%Y-%m-%d %H:%M')}
description: IAF debrief — {facts.get('total_calls', 0)} calls, {len(gaps)} gaps, {len(actions)} directives
type: feedback
---

# Session Retrospective

**Date**: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}

## Facts

- **Duration**: {facts.get('duration_min', 0)} minutes
- **Routed calls**: {facts.get('total_calls', 0)}
- **Cost**: ${facts.get('total_cost', 0.0):.4f}
- **Saved**: ${facts.get('total_saved', 0.0):.4f}
- **Manual corrections**: {facts.get('correction_count', 0)}
- **Avg confidence**: {facts.get('avg_confidence', 0)*100:.0f}%
- **Accuracy**: {facts.get('classification_accuracy', 0)*100:.0f}%

### Task Distribution

"""
    for task, count in facts.get("task_distribution", {}).items():
        content += f"- {task}: {count} calls\n"

    content += "\n### Model Distribution\n\n"
    for model, count in facts.get("model_distribution", {}).items():
        model_short = model.split("/", 1)[-1] if "/" in model else model
        content += f"- {model_short}: {count} calls\n"

    content += "\n## Gaps Detected\n\n"
    if gaps:
        for gap in gaps:
            content += f"- {gap.get('task_type', '?')}: {gap.get('reason', 'unknown gap')}\n"
            content += f"  Flags: {', '.join(gap.get('flags', []))}\n"
    else:
        content += "- None detected\n"

    content += "\n## Root Causes\n\n"
    if causes:
        for cause in causes:
            content += (
                f"- {cause.get('root_cause')}: {cause.get('task_type', '?')}\n"
                f"  Evidence: {cause.get('evidence', 'N/A')}\n"
            )
    else:
        content += "- None identified\n"

    content += "\n## Actions\n\n"
    if actions:
        for action in actions:
            content += (
                f"- [{action.get('type')}] {action.get('directive', 'N/A')}\n"
                f"  Evidence: {action.get('evidence', 'N/A')}\n"
            )
    else:
        content += "- None generated\n"

    filepath.write_text(content)
    return filepath


def write_directives(actions: list[dict]) -> None:
    """Append actions to the binding directives log.

    File: ~/.llm-router/directives.md (append-only)

    Format:
    ## YYYY-MM-DD HH:MM — {task_type}
    - Source: {evidence}
    - Rule: {directive}
    - Status: {status}

    Args:
        actions: List of actions from generate_actions()
    """
    if not actions:
        return

    DIRECTIVES_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M")

    entries = []
    for action in actions:
        entry = f"""## {timestamp} — {action.get('task_type', 'unknown')}
- Source: {action.get('evidence', 'N/A')}
- Rule: {action.get('directive', 'N/A')}
- Status: PENDING (requires {action.get('confidence', 1)}/3 confirmations)

"""
        entries.append(entry)

    try:
        # Append to existing or create new
        if DIRECTIVES_FILE.exists():
            existing = DIRECTIVES_FILE.read_text()
            DIRECTIVES_FILE.write_text(existing + "\n".join(entries))
        else:
            DIRECTIVES_FILE.write_text("\n".join(entries))
    except OSError:
        pass  # Graceful failure — don't break session


def write_claude_mem_entry(retro: dict) -> Optional[Path]:
    """Write retrospective to claude-mem feedback format.

    Location: ~/.claude/projects/{hash}/memory/retro_YYYY-MM-DD.md

    Uses frontmatter: name, description, type: feedback

    Args:
        retro: Retrospective dict

    Returns:
        Path to written file, or None if claude-mem is not available
    """
    # Try to find claude-mem project directory
    from pathlib import Path
    import hashlib
    import os

    project_root = Path.cwd()
    project_hash = hashlib.md5(str(project_root).encode()).hexdigest()[:8]

    mem_dir = Path.home() / ".claude" / "projects" / f"-{project_root.name}" / "memory"

    if not mem_dir.exists():
        # Not in a claude-mem tracked project
        return None

    now = datetime.now()
    filename = f"retro_{now.strftime('%Y-%m-%d')}.md"
    filepath = mem_dir / filename

    facts = retro.get("facts", {})
    gaps = retro.get("gaps", [])

    content = f"""---
name: Session Retrospective {now.strftime('%Y-%m-%d')}
description: {len(gaps)} routing gaps detected this session
type: feedback
---

## Session Summary

- **Calls**: {facts.get('total_calls', 0)}
- **Accuracy**: {facts.get('classification_accuracy', 0)*100:.0f}%
- **Gaps**: {len(gaps)}

## Key Gaps

"""
    for gap in gaps[:5]:  # Top 5 gaps
        content += f"- {gap.get('task_type')}: {', '.join(gap.get('flags', []))}\n"

    try:
        filepath.write_text(content)
        return filepath
    except OSError:
        return None


# ── Async Entry Points ─────────────────────────────────────────────────────

async def run_session_retrospective(write_files: bool = True) -> dict:
    """Run a complete session retrospective.

    Fetches decisions from the session window, analyzes them, generates actions,
    and optionally writes results to disk.

    Args:
        write_files: Whether to write retrospective and directives to disk

    Returns:
        Retrospective dict
    """
    start, end = get_session_window()
    decisions = fetch_session_decisions(start, end)
    corrections = fetch_session_corrections(start, end)

    retro = build_retrospective(start, end, decisions, corrections)

    if write_files:
        try:
            write_retrospective_file(retro)
            write_directives(retro.get("actions", []))
            write_claude_mem_entry(retro)
        except Exception:
            pass  # Graceful failure — don't break session

    return retro


async def run_weekly_retrospective() -> dict:
    """Run a weekly retrospective across the last 7 days.

    Loads all daily retrospectives, detects recurring patterns,
    generates permanent directives if 3+ daily retros agree.

    Returns:
        Aggregated weekly retrospective
    """
    # Placeholder for future implementation
    # Load last 7 daily .md files, detect recurring patterns
    return {
        "period": "weekly",
        "daily_retrospectives": 0,
        "recurring_patterns": [],
        "permanent_directives_generated": 0,
    }


# ── Output Formatting ──────────────────────────────────────────────────────

def format_compact_summary(retro: dict) -> str:
    """Format a 4-line compact summary for session-end hook.

    Args:
        retro: Retrospective dict

    Returns:
        Formatted string suitable for appending to session-end output
    """
    facts = retro.get("facts", {})
    gaps = retro.get("gaps", [])
    actions = retro.get("actions", [])

    accuracy = facts.get("classification_accuracy", 1.0)
    accuracy_pct = int(accuracy * 100)

    lines = [
        "  Retrospective",
        f"  • Accuracy: {accuracy_pct}% · Gaps: {len(gaps)} · Actions: {len(actions)}",
    ]

    return "\n".join(lines)


def format_full_report(retro: dict) -> str:
    """Format full retrospective report for CLI display.

    Args:
        retro: Retrospective dict

    Returns:
        Formatted markdown report
    """
    facts = retro.get("facts", {})
    gaps = retro.get("gaps", [])
    causes = retro.get("root_causes", [])
    actions = retro.get("actions", [])

    lines = []

    # Header
    lines.append("═" * 70)
    lines.append(f"  SESSION RETROSPECTIVE ({facts.get('total_calls', 0)} calls)")
    lines.append("═" * 70)
    lines.append("")

    # Facts
    lines.append("【FACTS】")
    lines.append(
        f"  Calls: {facts.get('total_calls', 0)}  |  "
        f"Cost: ${facts.get('total_cost', 0.0):.4f}  |  "
        f"Saved: ${facts.get('total_saved', 0.0):.4f}"
    )
    lines.append(
        f"  Accuracy: {facts.get('classification_accuracy', 1.0)*100:.0f}%  |  "
        f"Corrections: {facts.get('correction_count', 0)}  |  "
        f"Duration: {facts.get('duration_min', 0)}min"
    )
    lines.append("")

    # Gaps
    if gaps:
        lines.append("【GAPS】")
        for gap in gaps[:5]:  # Top 5
            lines.append(
                f"  ⚠ {gap.get('task_type')}: {gap.get('reason', 'N/A')}"
            )
    lines.append("")

    # Root Causes
    if causes:
        lines.append("【ROOT CAUSES】")
        for cause in causes[:5]:
            lines.append(
                f"  • {cause.get('root_cause')}: {cause.get('evidence', 'N/A')}"
            )
    lines.append("")

    # Actions
    if actions:
        lines.append("【ACTIONS】")
        for action in actions:
            lines.append(
                f"  → {action.get('directive', 'N/A')}"
            )

    lines.append("─" * 70)

    return "\n".join(lines)
