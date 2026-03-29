#!/usr/bin/env python3
"""PostToolUse hook — nudges Claude to refresh stale usage data.

After any llm_* MCP tool call, checks if cached Claude subscription data
is older than 15 minutes. If so, injects a gentle reminder to refresh via
Playwright's browser_evaluate.

This hook is zero-cost: it reads a tiny state file, no LLM calls.
"""

from __future__ import annotations

import json
import os
import sys
import time

STATE_FILE = os.path.expanduser("~/.llm-router/usage_last_refresh.txt")
STALE_THRESHOLD_SEC = 15 * 60  # 15 minutes


def main() -> None:
    payload = json.loads(sys.stdin.read())

    # Only trigger after llm_* tool calls
    tool_name = payload.get("toolName", "")
    if not tool_name.startswith("llm_"):
        return

    # Don't trigger on usage-related tools (would be circular)
    if tool_name in ("llm_check_usage", "llm_update_usage", "llm_cache_stats", "llm_cache_clear"):
        return

    # Check last refresh time
    last_refresh = 0.0
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                last_refresh = float(f.read().strip())
        except (ValueError, OSError):
            pass

    age_sec = time.time() - last_refresh
    if age_sec < STALE_THRESHOLD_SEC:
        return

    # Data is stale — emit a reminder
    age_min = int(age_sec / 60)
    hint = (
        f"[USAGE STALE: {age_min}m since last refresh] "
        "Consider running /usage-pulse or calling llm_check_usage "
        "to refresh Claude subscription data for accurate routing."
    )

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "contextForAgent": hint,
        },
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
