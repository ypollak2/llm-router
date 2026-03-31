#!/usr/bin/env python3
# llm-router-hook-version: 2
"""SessionStart hook — inject routing banner + reset session tracking.

Fires once when a new Claude Code session begins. Three jobs:
  1. Inject a compact routing table at position 0 of the context window,
     so routing rules are always salient regardless of session length.
  2. Reset the session stats tracker so session-end summary is accurate.
  3. Reset stale circuit breakers so yesterday's provider failures don't
     block healthy providers today.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

STATE_DIR = os.path.expanduser("~/.llm-router")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
SESSION_ID_FILE = os.path.join(STATE_DIR, "session_id.txt")

BANNER = """
╔════════════════════════════════════════════════════════════════╗
║  ⚡ llm-router ACTIVE — mandatory routing in effect           ║
╠════════════════════════════════════════════════════════════════╣
║  query/*    → llm_query    (Haiku — 50x cheaper than Opus)   ║
║  code/*     → llm_code     (Sonnet — 10x cheaper)            ║
║  generate/* → llm_generate (Gemini Flash — 20x cheaper)      ║
║  research/* → llm_research (Perplexity — web-grounded)       ║
║  analyze/*  → llm_analyze  (Sonnet — deep analysis)          ║
╠════════════════════════════════════════════════════════════════╣
║  EVERY prompt gets classified. Call the tool. Don't answer   ║
║  yourself. FORBIDDEN: Agent subagents · self-answer ·        ║
║  WebSearch · WebFetch when a ROUTE hint is present.          ║
║  The cheap model's output IS your response.                  ║
╚════════════════════════════════════════════════════════════════╝
""".strip()


def _reset_session_stats() -> None:
    """Write current timestamp and a fresh UUID as session identifiers."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(SESSION_START_FILE, "w") as f:
            f.write(str(time.time()))
        with open(SESSION_ID_FILE, "w") as f:
            f.write(str(uuid.uuid4()))
    except OSError:
        pass


def _reset_stale_health() -> None:
    """Call llm_update_usage via the MCP server to refresh Claude usage
    and reset stale circuit breakers in the router process.

    We do this by invoking a lightweight Python snippet that imports the
    health tracker directly (same process as the router when running tests,
    but for the actual MCP server this is a no-op — the server resets its
    own tracker on startup via the check_and_update_hooks path).

    Failure here is non-fatal: routing still works, just may skip providers
    that failed yesterday.
    """
    # Write a stale-reset marker so the router process can act on it
    reset_file = os.path.join(STATE_DIR, "reset_stale.flag")
    try:
        with open(reset_file, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def main() -> None:
    try:
        json.load(sys.stdin)  # consume input (may be empty)
    except (json.JSONDecodeError, EOFError):
        pass

    _reset_session_stats()
    _reset_stale_health()

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "contextForAgent": BANNER,
        }
    }))


if __name__ == "__main__":
    main()
