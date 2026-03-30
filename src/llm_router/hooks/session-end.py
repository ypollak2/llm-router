#!/usr/bin/env python3
# llm-router-hook-version: 1
"""Stop hook — session routing distribution summary.

Fires when a Claude Code session ends. Reads savings_log.jsonl entries
since session start and prints a routing distribution summary showing:
  - Which tools were called and how many times
  - How many prompts were answered directly (missed routing)
  - Estimated cost saved vs all-Opus baseline
"""

from __future__ import annotations

import json
import os
import sys
import time

STATE_DIR = os.path.expanduser("~/.llm-router")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
SAVINGS_LOG_FILE = os.path.join(STATE_DIR, "savings_log.jsonl")
CALL_COUNT_FILE = os.path.join(STATE_DIR, "routed_call_count.txt")

# Tool display names
TOOL_LABELS = {
    "llm_query": ("llm_query   ", "Haiku   "),
    "llm_code": ("llm_code    ", "Sonnet  "),
    "llm_generate": ("llm_generate", "Flash   "),
    "llm_research": ("llm_research", "Perplx  "),
    "llm_analyze": ("llm_analyze ", "Sonnet  "),
    "llm_image": ("llm_image   ", "Imagen  "),
    "llm_stream": ("llm_stream  ", "varies  "),
    "llm_route": ("llm_route   ", "auto    "),
}

# Estimated cost per routed call saved vs Opus baseline
# Opus: ~$0.26/call (1500 in + 2000 out tokens @ $15/$75 per M)
# Haiku: ~$0.003/call, Sonnet: ~$0.033/call, Flash: ~$0.002/call
SAVINGS_PER_TOOL = {
    "llm_query": 0.257,
    "llm_code": 0.227,
    "llm_generate": 0.258,
    "llm_research": 0.200,
    "llm_analyze": 0.227,
    "llm_image": 0.200,
    "llm_stream": 0.150,
    "llm_route": 0.200,
}


def _bar(count: int, max_count: int, width: int = 16) -> str:
    if max_count == 0:
        return "░" * width
    filled = round(count / max_count * width)
    return "█" * filled + "░" * (width - filled)


def _read_session_start() -> float:
    try:
        with open(SESSION_START_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return time.time() - 3600  # default: last hour


def _read_session_calls() -> dict[str, int]:
    """Read savings_log.jsonl entries since session start."""
    session_start = _read_session_start()
    counts: dict[str, int] = {}

    if not os.path.exists(SAVINGS_LOG_FILE):
        return counts

    try:
        with open(SAVINGS_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("timestamp", "")
                    # Parse ISO timestamp to epoch
                    from datetime import datetime
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    if ts >= session_start:
                        tool = entry.get("tool", "unknown")
                        counts[tool] = counts.get(tool, 0) + 1
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
    except OSError:
        pass

    return counts


def _format_summary(counts: dict[str, int]) -> str:
    if not counts:
        return ""

    total = sum(counts.values())
    max_count = max(counts.values()) if counts else 1
    total_saved = sum(SAVINGS_PER_TOOL.get(tool, 0.15) * n for tool, n in counts.items())

    lines = [
        "━━ Session Routing Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for tool, n in sorted(counts.items(), key=lambda x: -x[1]):
        label, model = TOOL_LABELS.get(tool, (f"{tool:<12}", "?       "))
        bar = _bar(n, max_count)
        saved = SAVINGS_PER_TOOL.get(tool, 0.15) * n
        lines.append(f"  {label}  {bar}  {n:3}x  {model}  ~${saved:.2f} saved")

    lines.append(f"  {'─' * 58}")
    lines.append(f"  {total} tasks routed externally  |  ~${total_saved:.2f} saved vs all-Opus")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    counts = _read_session_calls()
    if not counts:
        sys.exit(0)

    summary = _format_summary(counts)
    if not summary:
        sys.exit(0)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "contextForAgent": summary,
        }
    }))


if __name__ == "__main__":
    main()
