#!/usr/bin/env python3
# llm_router-hook-version: 5
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
from datetime import datetime
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


def _lock_wait_s() -> float:
    """Seconds to wait for the state lock, per acquisition (default 0.25).

    This hook takes the lock once per run, so that is also the most it can add.
    """
    try:
        return max(0.0, float(os.environ.get("LLM_ROUTER_BREAKER_LOCK_WAIT_S", "0.25")))
    except ValueError:
        return 0.25


def _log_hook_error(message: str) -> None:
    """One line in hook_errors.log (the llm_router.hook_health schema). Never raises."""
    try:
        home = _router_home()
        home.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now().isoformat(), "hook": "agent-depth-release",
                 "error": message[:200]}
        with (home / "hook_errors.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _slot_ttl_s() -> float:
    """Same bound as agent-route.py (LLM_ROUTER_AGENT_SLOT_TTL_S, default 3600 s)."""
    try:
        ttl = float(os.environ.get("LLM_ROUTER_AGENT_SLOT_TTL_S", "3600"))
        return ttl if ttl > 0 else 3600.0
    except (ValueError, TypeError):
        return 3600.0


def _live_slots(data: dict, now: float) -> list:
    """In-flight entries [token, start_ts] younger than the TTL (mirrors agent-route.py)."""
    raw = data.get("slots")
    if not isinstance(raw, list):
        try:
            legacy, ts = max(0, int(data.get("depth", 0))), float(data.get("ts", 0))
        except (ValueError, TypeError):
            legacy, ts = 0, 0.0
        raw = [[f"legacy{i}", ts] for i in range(legacy)]
    ttl, live = _slot_ttl_s(), []
    for e in raw:
        try:
            if isinstance(e, list) and len(e) >= 2 and now - float(e[1]) < ttl:
                live.append([str(e[0]), float(e[1])])
        except (ValueError, TypeError):
            continue
    return live




def _lock(depth_file: Path):
    """flock the sidecar lock file agent-route.py uses; None if it cannot be had."""
    fh = None
    try:
        import fcntl
        fd = os.open(f"{depth_file}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "a+")
        deadline = time.monotonic() + _lock_wait_s()
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.002)
    except Exception as exc:  # noqa: BLE001
        _msg = (f"agent breaker state lock unavailable ({type(exc).__name__}); "
                f"continuing unlocked")
        print(f"llm-router: {_msg}", file=sys.stderr)
        _log_hook_error(_msg)
        if fh is not None:
            fh.close()
        return None


def _atomic_write(path: Path, data: dict) -> None:
    """tmp file + os.replace, mode 0600 (same scheme as agent-route.py)."""
    import uuid
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    if hook_input.get("tool_name", "") != "Agent":
        sys.exit(0)

    session_id = _get_session_id()
    depth_file = _depth_file(session_id)
    if not depth_file.exists():
        # SessionEnd (or a session that never took a slot) left no state. Writing here
        # would recreate a stale file that nothing ever cleans, and so would the lock.
        sys.exit(0)
    token = str(hook_input.get("tool_use_id") or "").strip()
    lock_fh = _lock(depth_file)
    try:
        try:
            data = json.loads(depth_file.read_text())
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            # SessionEnd removed it between the check and the lock: do not recreate it,
            # and remove the lock file _lock() just created.
            Path(f"{depth_file}.lock").unlink(missing_ok=True)
            return
        except (json.JSONDecodeError, ValueError):
            data = {}

        now = time.time()
        slots = _live_slots(data, now)
        if token:
            # Release THIS call's slot. No match is a no-op: an Explore/allowlisted/
            # routed-away call never held one, and must not free a sibling's.
            slots = [e for e in slots if e[0] != token]
        elif slots:
            slots.sort(key=lambda e: e[1])
            slots.pop(0)  # host sent no tool_use_id: free the oldest (the old count semantics)

        # Keep the nesting registry ("agents"/"pending") agent-route.py and
        # subagent-start.py keep in this same file; only the in-flight slots move.
        data.update({"slots": slots, "depth": len(slots), "session_id": session_id, "ts": now})
        _atomic_write(depth_file, data)
    except Exception as exc:  # noqa: BLE001 -- fail open, say so
        _msg = f"agent depth not released ({type(exc).__name__})"
        print(f"llm-router: {_msg}", file=sys.stderr)
        _log_hook_error(_msg)
    finally:
        if lock_fh is not None:
            lock_fh.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
