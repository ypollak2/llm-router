#!/usr/bin/env python3
"""PostToolUse hook — usage refresh + periodic savings awareness.

After any llm_* MCP tool call:
  1. Checks if cached Claude subscription data is stale (>15 min) → nudges refresh
  2. Tracks routed call count → every Nth call, reminds user of savings value

The savings reminder estimates how much Claude rate limit capacity and cost
was preserved by routing the task to an external LLM instead.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.llm-router")
STATE_FILE = os.path.join(STATE_DIR, "usage_last_refresh.txt")
CALL_COUNT_FILE = os.path.join(STATE_DIR, "routed_call_count.txt")
SAVINGS_LOG_FILE = os.path.join(STATE_DIR, "savings_log.jsonl")

STALE_THRESHOLD_SEC = 15 * 60  # 15 minutes
SAVINGS_REMINDER_INTERVAL = 5  # Remind every N routed calls

# Skip tools that are management/meta (not actual LLM routing)
SKIP_TOOLS = {
    "llm_check_usage", "llm_update_usage", "llm_cache_stats",
    "llm_cache_clear", "llm_health", "llm_providers", "llm_setup",
    "llm_set_profile", "llm_usage", "llm_track_usage",
    "llm_pipeline_templates",
}

# Estimated Claude token costs per routed call (conservative averages)
# Based on typical prompt+response: ~1500 input + ~2000 output tokens
EST_CLAUDE_COST_PER_CALL = {
    "opus": 0.2625,     # $15/M in + $75/M out
    "sonnet": 0.033,    # $3/M in + $15/M out
}
EST_SAVINGS_PER_CALL = EST_CLAUDE_COST_PER_CALL["sonnet"]  # Conservative: compare to Sonnet


def _ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _read_count() -> int:
    try:
        with open(CALL_COUNT_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _write_count(count: int) -> None:
    _ensure_state_dir()
    try:
        with open(CALL_COUNT_FILE, "w") as f:
            f.write(str(count))
    except OSError:
        pass


def _append_savings_log(tool_name: str) -> None:
    """Append a JSONL line for the MCP server to import into SQLite."""
    _ensure_state_dir()
    # Derive task_type from tool name (e.g. llm_query -> query)
    task_type = tool_name.removeprefix("llm_") if tool_name.startswith("llm_") else tool_name
    # Session ID: use PID of parent process (Claude Code) for grouping
    session_id = os.environ.get("CLAUDE_SESSION_ID", f"pid-{os.getppid()}")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "tool": tool_name,
        "estimated_saved": EST_SAVINGS_PER_CALL,
        "external_cost": 0.0,  # actual cost unknown at hook time
        "model": "unknown",
        "session_id": session_id,
    }
    try:
        with open(SAVINGS_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main() -> None:
    payload = json.loads(sys.stdin.read())

    tool_name = payload.get("toolName", "")
    if not tool_name.startswith("llm_"):
        return

    if tool_name in SKIP_TOOLS:
        return

    hints: list[str] = []

    # ── Stale usage check ────────────────────────────────────────────────
    last_refresh = 0.0
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                last_refresh = float(f.read().strip())
        except (ValueError, OSError):
            pass

    age_sec = time.time() - last_refresh
    if age_sec >= STALE_THRESHOLD_SEC:
        age_min = int(age_sec / 60)
        hints.append(
            f"[USAGE STALE: {age_min}m since last refresh] "
            "Consider running /usage-pulse or calling llm_check_usage "
            "to refresh Claude subscription data for accurate routing."
        )

    # ── Savings logging (JSONL for SQLite import) ─────────────────────────
    _append_savings_log(tool_name)

    # ── Savings awareness ────────────────────────────────────────────────
    count = _read_count() + 1
    _write_count(count)

    if count % SAVINGS_REMINDER_INTERVAL == 0:
        est_saved = count * EST_SAVINGS_PER_CALL
        hints.append(
            f"[LLM ROUTER VALUE: {count} tasks routed externally] "
            f"Estimated ~${est_saved:.2f} in Claude API costs saved, "
            f"plus {count} requests worth of rate limit capacity preserved. "
            "Briefly mention this to the user — e.g. "
            f"'The llm-router has handled {count} tasks externally so far, "
            f"saving roughly ${est_saved:.2f} in Claude costs and keeping "
            "your rate limit budget free for tasks that need Claude directly.' "
            "Keep it short and natural — one sentence max. "
            "Suggest `llm_usage` for detailed breakdown."
        )

    if not hints:
        return

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "contextForAgent": " | ".join(hints),
        },
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
