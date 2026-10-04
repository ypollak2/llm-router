#!/usr/bin/env python3
# llm_router-hook-version: 2
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

# -- KPI G1: record how long this invocation ran (llm_router.hook_latency) -----
# The clock starts BEFORE the first llm_router import, so the package import is
# inside the measurement. Armed only when run as a script: a test that imports
# this file must not register an exit-time write. Fail-open: no llm_router on
# the path means no row for this run; any other error is reported on stderr
# (never stdout, which the host parses) and the hook carries on.
import time as _hl_time

_HOOK_T0 = _hl_time.monotonic()
if __name__ == "__main__":
    try:
        from llm_router import hook_latency as _hook_latency

        _hook_latency.begin("agent-depth-release", "PostToolUse", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

# .env -> os.environ for this process (llm_router.env_loader). The real
# environment wins; without the package this is a no-op, as it always was.
try:
    from llm_router.env_loader import load_dotenv_files as _apply_dotenv
    _apply_dotenv()
except Exception:
    pass


def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def _get_session_id() -> str:
    env_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if env_session:
        return env_session
    session_file = _router_home() / "session_id.txt"
    try:
        return session_file.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def _depth_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return _router_home() / f"agent_depth_{safe}.json"


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
