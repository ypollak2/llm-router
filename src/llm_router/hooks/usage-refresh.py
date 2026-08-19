#!/usr/bin/env python3
# llm_router-hook-version: 2
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
from pathlib import Path

try:
    from llm_router import pricing as _pricing
except ImportError:  # pragma: no cover — llm_router not importable from this interpreter
    # Copied to ~/.claude/hooks/ and run standalone; same reasoning as the guard
    # in cc-usage-track.py. With no price source the estimate table is empty and
    # the savings figures it feeds are suppressed rather than reported as zero.
    _pricing = None  # type: ignore[assignment]


# ── Registered-tool surface (CHZ-SURF-01) ────────────────────────────────────
# Tool names are tier-dependent (LLM_ROUTER_SLIM). NEVER put a raw tool name in
# output: under the DEFAULT `consolidated` tier the legacy llm_query /
# llm_analyze / llm_code / llm_research / llm_generate names are not registered,
# so naming one hands the caller "Error: No such tool available" — after which
# it silently does the work on the expensive model and the savings dashboard
# cannot distinguish that from "chose not to route".
def _load_tool_surface_fns():
    """(route_tool, route_call, route_call_with_complexity) from llm_router.tool_surface.

    Falls back to the stdlib-only copy the installer drops next to the hooks, then
    to the in-repo source, then to identity (correct only for tier `off`).
    """
    try:
        from llm_router.tool_surface import (
            route_call,
            route_call_with_complexity,
            route_tool,
        )
        return route_tool, route_call, route_call_with_complexity
    except ImportError:
        pass
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _here = _P(__file__).resolve().parent
        for _cand in (_here / "llm_router_tool_surface.py", _here.parent / "tool_surface.py"):
            if not _cand.exists():
                continue
            _spec = _ilu.spec_from_file_location("llm_router_tool_surface", _cand)
            _mod = _ilu.module_from_spec(_spec)
            sys.modules["llm_router_tool_surface"] = _mod  # dataclasses needs this
            _spec.loader.exec_module(_mod)
            return _mod.route_tool, _mod.route_call, _mod.route_call_with_complexity
    except Exception:  # noqa: BLE001 — a broken support module must not kill the hook
        pass
    return (
        lambda n, **k: n,
        lambda n, *a, **k: (f"{n}({', '.join(a)})" if a else n),
        lambda n, c, *a, **k: f"{n}(complexity='{c}'" + ("".join(', ' + x for x in a)) + ")",
    )


route_tool, route_call, route_call_with_complexity = _load_tool_surface_fns()

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
    # CHZ-SURF-01: under the consolidated default those management tools are
    # collapsed into these doors. Listing only the legacy names would let an
    # observability call be counted as a routed call — i.e. checking your savings
    # would increase your savings.
    "llm_router_status", "llm_router_admin", "llm_router_session",
}

# The doors that DO represent real routing work and must stay counted.
ROUTING_DOORS = {"llm", "llm_act"}

# Estimated Claude token costs per routed call (conservative averages) for a
# typical prompt+response.
#
# WP-03: computed from llm_router.pricing rather than pre-multiplied by hand. The
# old literals were 0.2625 for Opus and 0.033 for Sonnet — arithmetic on the
# retired $15/$75 and pre-intro $3/$15 tiers. Pre-multiplied constants are the
# worst variety of stale price: the rate is not even visible in the file, so
# grepping for "15.0" would never have found this one.
_EST_INPUT_TOKENS = 1500
_EST_OUTPUT_TOKENS = 2000

EST_CLAUDE_COST_PER_CALL = (
    {
        _family: _c
        for _family in ("opus", "sonnet")
        if (
            _c := _pricing.cost_usd(
                _family, input_tokens=_EST_INPUT_TOKENS, output_tokens=_EST_OUTPUT_TOKENS
            )
        )
        is not None
    }
    if _pricing is not None
    else {}
)
#: Conservative: compare to Sonnet. 0.0 only when there is no price source at
#: all, in which case every figure derived from it is a suppressed zero rather
#: than a claim of zero savings.
EST_SAVINGS_PER_CALL = EST_CLAUDE_COST_PER_CALL.get("sonnet", 0.0)


def _ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _read_count() -> int:
    try:
        with open(CALL_COUNT_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _write_count(count: int) -> None:
    """Write count atomically using a temp file + os.replace() to avoid partial reads.

    A simple open(..., "w") is not atomic: concurrent PostToolUse hooks firing
    simultaneously can interleave reads and writes, producing an incorrect count.
    os.replace() is atomic on POSIX (rename syscall), so the target file is
    always either the old or new value — never a half-written intermediate.
    """
    _ensure_state_dir()
    target = Path(CALL_COUNT_FILE)
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(str(count))
        os.replace(tmp, target)
    except OSError:
        # Clean up temp file if replace failed
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _append_savings_log(tool_name: str) -> None:
    """Append a JSONL line for the MCP server to import into SQLite."""
    _ensure_state_dir()
    # Derive task_type from tool name (e.g. llm_query -> query)
    # CHZ-SURF-01: strip any MCP qualification first (mcp__llm_router__llm_query).
    # The consolidated door is just `llm` and carries the specialization in its
    # ARGUMENTS, not its name, so there is no task to recover from the name —
    # record "routed" rather than inventing one.
    bare = tool_name.rsplit("__", 1)[-1]
    if bare.startswith("llm_"):
        task_type = bare.removeprefix("llm_")
    elif bare in ("llm", "llm_act"):
        task_type = "routed"
    else:
        task_type = bare
    # Session ID: read UUID written by session-start hook (never reuses PIDs)
    session_id_file = os.path.join(STATE_DIR, "session_id.txt")
    try:
        with open(session_id_file) as _f:
            session_id = _f.read().strip() or f"pid-{os.getppid()}"
    except OSError:
        session_id = os.environ.get("CLAUDE_SESSION_ID", f"pid-{os.getppid()}")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "tool": tool_name,
        "estimated_saved": EST_SAVINGS_PER_CALL,
        "external_cost": 0.0,  # actual cost unknown at hook time
        "model": "unknown",
        "session_id": session_id,
        "host": "claude_code",
    }
    try:
        with open(SAVINGS_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _oauth_refresh_and_write() -> None:
    """Fetch live Claude subscription usage via OAuth and write usage.json.

    Invoked when this script is run with no stdin payload — i.e. the statusline's
    periodic background refresh. Mirrors the fetch/parse in the session-start hook
    but is null-safe: the OAuth endpoint returns ``null`` (not a missing key) for
    inactive windows like ``seven_day_sonnet``, so we coerce ``None`` to ``{}``.
    """
    import subprocess
    import urllib.request

    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return
        token = json.loads(r.stdout.strip()).get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — HTTPS Anthropic API
            data = json.loads(resp.read().decode())
        session_pct = float((data.get("five_hour") or {}).get("utilization", 0.0))
        weekly_pct = float((data.get("seven_day") or {}).get("utilization", 0.0))
        sonnet_pct = float((data.get("seven_day_sonnet") or {}).get("utilization", 0.0))
    except Exception:
        return  # leave the last-known usage.json untouched on any failure

    snap = {
        "session_pct": round(session_pct, 1),
        "weekly_pct": round(weekly_pct, 1),
        "sonnet_pct": round(sonnet_pct, 1),
        "highest_pressure": round(max(session_pct, weekly_pct, sonnet_pct) / 100.0, 4),
        "updated_at": time.time(),
    }
    _ensure_state_dir()
    usage_path = os.path.join(STATE_DIR, "usage.json")
    tmp = usage_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, usage_path)
    except OSError:
        pass


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        # No hook payload → statusline background refresh. Fetch & write usage.json.
        _oauth_refresh_and_write()
        return

    payload = json.loads(raw)

    tool_name = payload.get("toolName", "")
    # CHZ-SURF-01: accept the consolidated doors, not just the `llm_` prefix.
    # Under the DEFAULT tier the completion door is named exactly `llm`, which
    # fails a startswith("llm_") test — so every routed call was dropped here and
    # never reached the savings log. That is an UNDERcount of real savings, and it
    # looks identical to "nothing was routed".
    bare_name = tool_name.rsplit("__", 1)[-1]  # mcp__llm_router__llm -> llm
    if not (bare_name.startswith("llm_") or bare_name in ROUTING_DOORS):
        return

    if bare_name in SKIP_TOOLS:
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
            f"Consider running /usage-pulse or calling {route_tool('llm_check_usage')} "
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
            f"[LLM_ROUTER VALUE: {count} tasks routed externally] "
            f"Estimated ~${est_saved:.2f} in Claude API costs saved, "
            f"plus {count} requests worth of rate limit capacity preserved. "
            "Briefly mention this to the user — e.g. "
            f"'The llm_router has handled {count} tasks externally so far, "
            f"saving roughly ${est_saved:.2f} in Claude costs and keeping "
            "your rate limit budget free for tasks that need Claude directly.' "
            "Keep it short and natural — one sentence max. "
            f"Suggest `{route_tool('llm_usage')}` for detailed breakdown."
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
