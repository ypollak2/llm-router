#!/usr/bin/env python3
# llm_router-hook-version: 1
"""PostToolUse[Agent] hook — release the agent nesting-depth slot.

The circuit breaker in agent-route.py (PreToolUse[Agent]) increments a
per-session depth counter before approving a real (non-Explore, non-
allowlisted, non-routed) subagent spawn, to bound runaway nested-agent
recursion. Nothing previously decremented it back down when that subagent
finished, so depth was a lifetime total, not a live nesting count: after 3
real Agent spawns anywhere in a session's lifetime, every further Agent
call was permanently blocked for the rest of that session, even once all
three had long since completed. This hook fires right after each Agent
call finishes and gives the slot back.

Must key on the exact same session id / file naming as agent-route.py's
_get_session_id() / _depth_file() — see that file for why CLAUDE_CODE_
SESSION_ID (not the old shared ~/.llm-router/session_id.txt) is used, and why
the depth file itself is per-session rather than one shared file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


def _get_session_id() -> str:
    env_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if env_session:
        return env_session
    session_file = Path.home() / ".llm-router" / "session_id.txt"
    try:
        return session_file.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def _depth_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return Path.home() / ".llm-router" / f"agent_depth_{safe}.json"


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    if hook_input.get("tool_name", "") != "Agent":
        sys.exit(0)

    session_id = _get_session_id()
    depth_file = _depth_file(session_id)
    try:
        data = json.loads(depth_file.read_text())
        depth = max(0, int(data.get("depth", 0)) - 1)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        depth = 0

    depth_file.write_text(json.dumps({
        "depth": depth,
        "session_id": session_id,
        "ts": time.time(),
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
