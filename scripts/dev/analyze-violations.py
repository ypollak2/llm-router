#!/usr/bin/env python3
"""Analyze routing violations from enforcement.log — per-session breakdown.

This script reads ~/.llm-router/enforcement.log and:
  1. Groups violations by session_id with counts
  2. For the top 10 worst sessions, reads corresponding tool_history_*.json
  3. Outputs a markdown report with:
     - Summary: total violations, date range, distinct sessions
     - Top 10 sessions table: session_id, violation count, expected vs actual tools
     - Per-session detail: timestamps, tool sequence, what should have been called
  4. Writes to ~/.llm-router/retrospectives/violation-report-<date>.md
  5. Prints report to stdout

Usage:
    python3 analyze-violations.py
"""

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


def parse_enforcement_log() -> tuple[list[dict], Optional[str], Optional[str]]:
    """Parse enforcement.log and return violations list, earliest date, latest date."""
    log_path = Path.home() / ".llm-router" / "enforcement.log"
    if not log_path.exists():
        return [], None, None
    
    violations = []
    earliest = None
    latest = None
    
    try:
        for line in log_path.read_text().splitlines():
            if not line.strip():
                continue
            
            # Parse format: [2026-04-26 10:30:45] VIOLATION session=abc12345678 expected=llm_query got=Bash
            match = re.match(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (\w+) (.+)",
                line
            )
            if not match:
                continue
            
            timestamp_str, event_type, details = match.groups()
            
            # Parse details: key=value key=value...
            detail_dict = {}
            for part in details.split():
                if "=" in part:
                    key, value = part.split("=", 1)
                    detail_dict[key] = value
            
            if event_type == "VIOLATION":
                violation = {
                    "timestamp": timestamp_str,
                    "session_id": detail_dict.get("session", "unknown"),
                    "expected": detail_dict.get("expected", "?"),
                    "got": detail_dict.get("got", "?"),
                }
                violations.append(violation)
                
                # Track earliest/latest
                ts = timestamp_str.split()[0]  # Extract date part
                if earliest is None or ts < earliest:
                    earliest = ts
                if latest is None or ts > latest:
                    latest = ts
    except Exception as e:
        print(f"⚠️  Warning: Error parsing enforcement.log: {e}")
    
    return violations, earliest, latest


def group_violations_by_session(violations: list[dict]) -> dict[str, list[dict]]:
    """Group violations by session_id."""
    groups = defaultdict(list)
    for v in violations:
        groups[v["session_id"]].append(v)
    return dict(groups)


def read_tool_history(session_id: str) -> Optional[list[dict]]:
    """Read tool_history_{session_id}.json if it exists."""
    path = Path.home() / ".llm-router" / f"tool_history_{session_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("calls", [])
    except Exception:
        return None


def format_tool_sequence(session_id: str, violations: list[dict]) -> str:
    """Format a readable tool sequence for the session."""
    tool_history = read_tool_history(session_id)
    if not tool_history:
        return "  (no tool history available)"
    
    # Build a timeline showing violations
    violation_map = {(v["timestamp"], v["got"]): v for v in violations}
    
    lines = []
    for call in tool_history[-10:]:  # Show last 10 tool calls
        tool = call.get("tool", "?")
        ts = call.get("timestamp", 0)
        
        # Check if this tool call was a violation
        ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        
        # Find if this matches a violation
        is_violation = any(
            v["got"] == tool for v in violations
        )
        
        if is_violation:
            lines.append(f"  ❌ {ts_str}  {tool}")
        else:
            lines.append(f"  ✓  {ts_str}  {tool}")
    
    return "\n".join(lines) if lines else "  (empty)"


def generate_report(
    violations: list[dict],
    groups: dict[str, list[dict]],
    earliest: Optional[str],
    latest: Optional[str],
) -> str:
    """Generate markdown report."""
    
    # Sort sessions by violation count (descending)
    top_sessions = sorted(
        groups.items(),
        key=lambda x: len(x[1]),
        reverse=True,
    )[:10]
    
    distinct_sessions = len(groups)
    total_violations = len(violations)
    
    # Aggregate stats
    expected_tools = defaultdict(int)
    actual_tools = defaultdict(int)
    for v in violations:
        expected_tools[v["expected"]] += 1
        actual_tools[v["got"]] += 1
    
    report = []
    report.append("# Routing Violations Report\n")
    report.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    report.append(f"**Date range**: {earliest or '?'} to {latest or '?'}\n")
    report.append(f"**Total violations**: {total_violations}\n")
    report.append(f"**Distinct sessions**: {distinct_sessions}\n")
    report.append("")
    
    # Violation breakdown
    report.append("## Violation Breakdown\n")
    report.append("**Tools routed (should have been called first):**\n")
    for tool in sorted(expected_tools.keys(), key=lambda x: expected_tools[x], reverse=True):
        count = expected_tools[tool]
        pct = (count / total_violations) * 100
        report.append(f"- {tool}: {count} ({pct:.1f}%)\n")
    
    report.append("\n**Tools used instead (violations):**\n")
    for tool in sorted(actual_tools.keys(), key=lambda x: actual_tools[x], reverse=True):
        count = actual_tools[tool]
        pct = (count / total_violations) * 100
        report.append(f"- {tool}: {count} ({pct:.1f}%)\n")
    
    report.append("")
    
    # Top 10 sessions table
    if top_sessions:
        report.append("## Top 10 Sessions by Violations\n\n")
        report.append("| Session ID | Violations | Most Expected | Most Used | Tools Used |\n")
        report.append("|-----------|-----------|---------------|-----------|------------|\n")
        
        for session_id, session_violations in top_sessions:
            count = len(session_violations)
            
            # Most common expected and actual tools for this session
            exp_count = defaultdict(int)
            act_count = defaultdict(int)
            for v in session_violations:
                exp_count[v["expected"]] += 1
                act_count[v["got"]] += 1
            
            most_expected = max(exp_count.items(), key=lambda x: x[1])[0] if exp_count else "?"
            most_actual = max(act_count.items(), key=lambda x: x[1])[0] if act_count else "?"
            
            # All tools used in violations for this session
            tools_used = ", ".join(sorted(act_count.keys()))
            
            report.append(
                f"| `{session_id[:12]}...` | {count} | {most_expected} | {most_actual} | {tools_used} |\n"
            )
        
        report.append("")
    
    # Per-session details
    if top_sessions:
        report.append("## Session Details\n\n")
        
        for session_id, session_violations in top_sessions[:5]:  # Top 5 details
            count = len(session_violations)
            report.append(f"### Session `{session_id[:12]}...` ({count} violations)\n\n")
            
            # Timestamps
            timestamps = [v["timestamp"] for v in session_violations]
            first_ts = min(timestamps)
            last_ts = max(timestamps)
            report.append(f"**Time range**: {first_ts} to {last_ts}\n\n")
            
            # Tool sequence
            report.append("**Tool sequence** (last 10 calls):\n")
            sequence = format_tool_sequence(session_id, session_violations)
            report.append(sequence)
            report.append("\n\n")
    
    report.append("\n## Recommendations\n\n")
    report.append("1. **Check the MANDATORY ROUTE hint** — Review sessions with high violation counts to see if the hint was visible in their context.\n")
    report.append("2. **Improve hint visibility** — The box-drawing format in v7.5.0 makes violations less likely.\n")
    report.append("3. **Monitor enforcement mode** — Sessions with many violations may benefit from `LLM_ROUTER_ENFORCE=hard`.\n")
    report.append("4. **Per-session nudges** — After 3+ violations, the model receives a warning message to call the routed tool first.\n")
    
    return "".join(report)


def main():
    # Parse enforcement log
    violations, earliest, latest = parse_enforcement_log()
    
    if not violations:
        print("✅ No violations found in enforcement.log")
        return
    
    # Group by session
    groups = group_violations_by_session(violations)
    
    # Generate report
    report = generate_report(violations, groups, earliest, latest)
    
    # Write to file
    retrospectives_dir = Path.home() / ".llm-router" / "retrospectives"
    retrospectives_dir.mkdir(parents=True, exist_ok=True)
    
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_path = retrospectives_dir / f"violation-report-{date_str}.md"
    
    try:
        report_path.write_text(report)
        print(f"✅ Report written to {report_path}\n")
    except Exception as e:
        print(f"❌ Failed to write report: {e}")
        return
    
    # Print to stdout
    print(report)
    print(f"\n📊 Summary: {len(violations)} violations in {len(groups)} sessions")
    print(f"📁 Full report: {report_path}")


if __name__ == "__main__":
    main()
