#!/usr/bin/env python3
# llm-router-hook-version: 12
"""PreToolUse[*] hook — enforce routing compliance.

When auto-route.py issues a ⚡ MANDATORY ROUTE directive, it writes a
pending state file to ~/.llm-router/pending_route_{session_id}.json.

This hook fires before every tool call and:
  1. If no pending state → allow (no routing was requested for this prompt).
  2. If the tool is an llm_* MCP tool → routing honored, clear state, allow.
  3. If the tool exactly matches the expected_tool in pending state → allow + clear.
     (Supports MCP server routing, e.g. mcp__obsidian__create_note)
  4. If the tool is NOT in the task-specific blocklist → allow unconditionally.
     This covers: ToolSearch, all mcp__* tools, Agent (schema load), etc.
     For code tasks: Read/Glob/Grep/LS are also allowed (needed for editing).
     For Q&A tasks: Read/Glob/Grep/LS are blocked (Claude shouldn't self-answer).
  5. Detect coding sessions early: Mark as "coding" on first Read/Glob/Grep/LS/Edit/Write
     → Downgrade enforcement to soft for rest of session (allows legitimate investigation).
  6. Track violations and auto-pivot: Counter increments on each blocked tool call.
     After 2 violations → auto-downgrade to soft enforcement to prevent stuck patterns.
  7. If the tool IS in the task-specific blocklist → enforce based on LLM_ROUTER_ENFORCE:
       smart (default)  — hard for Q&A tasks (query/research/generate/analyze),
                          soft for code tasks (file editing allowed).
       soft             — log the violation, allow the call.
       hard             — block the call with a remediation message.
       off              — allow all calls regardless.

Enforcement modes:
  smart (default) — Balances cost savings with developer productivity:
                    • query / research / generate / analyze tasks → hard block
                      (Claude cannot answer directly — routes to cheap models)
                    • code tasks → soft (file tools are needed for actual editing)
                    Target: >80% of question-answering goes through router.
  soft            — Route hints appear in context; Claude can follow voluntarily.
                    Bash/Edit/Write are never blocked. Lowest friction.
  hard            — Bash/Edit/Write are blocked for ALL task types until an
                    llm_* tool is called. Maximum cost enforcement.
                    Set: export LLM_ROUTER_ENFORCE=hard
  off             — Enforcement completely disabled. No pending state is checked.

Compliance log: ~/.llm-router/enforcement.log
Pending state:  ~/.llm-router/pending_route_{session_id}.json

Environment variables:
  LLM_ROUTER_ENFORCE  smart | soft | hard | off   (default: smart)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ROUTER_DIR = Path.home() / ".llm-router"
_LOG_PATH = _ROUTER_DIR / "enforcement.log"
_PENDING_TTL = 60  # seconds — matches auto-route.py _PENDING_ROUTE_TTL_SEC

# Base blocklist: always blocked before routing is satisfied (all task types).
_BASE_BLOCK_TOOLS = frozenset({
    "Bash", "Edit", "MultiEdit", "Write", "NotebookEdit",
})

# Q&A task types: Claude answering by reading local files is the same as
# Claude answering directly — both bypass the cheap model. Block file-reading
# tools so the content must be passed to llm_analyze/llm_query instead.
_QA_TASK_TYPES = frozenset({"query", "research", "generate", "analyze"})
_QA_ONLY_BLOCK_TOOLS = frozenset({"Glob", "Read", "Grep", "LS"})


def _block_tools_for(task_type: str) -> frozenset:
    """Return the appropriate blocklist for the given task type."""
    if task_type in _QA_TASK_TYPES:
        return _BASE_BLOCK_TOOLS | _QA_ONLY_BLOCK_TOOLS
    return _BASE_BLOCK_TOOLS


# ── Session-Type Tracking ─────────────────────────────────────────────────────
# Written to ~/.llm-router/session_{id}.json when Claude's first file edit in
# a session is detected. Once marked "coding", enforcement downgrades to soft.

def _session_type_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"session_{session_id}.json"


def _is_coding_session(session_id: str) -> bool:
    """Return True if this session has already been identified as coding work."""
    try:
        data = _read_json_retry(_session_type_path(session_id))
        if data is None:
            return False
        return data.get("session_type") == "coding"
    except OSError:
        return False


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON to *path* via a same-directory temp file + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _mark_session_coding(session_id: str) -> None:
    """Mark session as coding — future directives won't block file-edit tools."""
    try:
        _write_json_atomic(
            _session_type_path(session_id),
            {"session_type": "coding", "marked_at": time.time()},
        )
    except OSError:
        pass


def _pending_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"pending_route_{session_id}.json"


def _read_json_retry(path: Path, retries: int = 3, retry_delay_sec: float = 0.01) -> dict | None:
    """Read JSON from *path*, retrying transient decode failures from concurrent writes."""
    for attempt in range(retries):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            if attempt == retries - 1:
                return None
            time.sleep(retry_delay_sec)
        except OSError:
            return None
    return None


def _read_pending(session_id: str) -> dict | None:
    p = _pending_path(session_id)
    try:
        data = _read_json_retry(p)
        if data is None:
            return None
        # Use expires_at if present (new format), else fall back to issued_at + TTL
        expires = data.get("expires_at") or (data.get("issued_at", 0) + _PENDING_TTL)
        remaining = expires - time.time()
        if remaining <= 0:
            # Log expiration for visibility
            try:
                _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                with _LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(
                        f"[{ts}] PENDING EXPIRED session={session_id[:12]} "
                        f"ttl={_PENDING_TTL}s\n"
                    )
            except OSError:
                pass
            p.unlink(missing_ok=True)
            return None
        # Store remaining time in data for error messages
        data["_remaining_seconds"] = int(remaining)
        return data
    except (OSError, KeyError):
        return None


def _clear_pending(session_id: str) -> None:
    _pending_path(session_id).unlink(missing_ok=True)


def _log_violation(session_id: str, tool: str, expected: str) -> None:
    try:
        _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] VIOLATION session={session_id[:12]} "
                f"expected={expected} got={tool}\n"
            )
    except OSError:
        pass


def _violation_counter_path(session_id: str) -> Path:
    """Path to violation counter file for this session."""
    return _ROUTER_DIR / f"violations_{session_id}.json"


def _read_violation_count(session_id: str) -> int:
    """Read violation count for session, return 0 if not found."""
    try:
        data = _read_json_retry(_violation_counter_path(session_id))
        return data.get("count", 0) if data else 0
    except (OSError, KeyError):
        return 0


def _increment_violation_count(session_id: str) -> int:
    """Increment violation counter and return new count."""
    try:
        path = _violation_counter_path(session_id)
        data = _read_json_retry(path) or {}
        count = data.get("count", 0) + 1
        _write_json_atomic(path, {"count": count, "last_violation_at": time.time()})
        return count
    except OSError:
        return 0


def _clear_violation_count(session_id: str) -> None:
    """Clear violation counter when routing is satisfied."""
    try:
        _violation_counter_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _read_pressure() -> dict[str, float]:
    """Read subscription pressure from ~/.llm-router/usage.json.

    Returns: Dict with 'sonnet' and 'weekly' keys as fractions 0.0–1.0.
    """
    try:
        data = json.loads((Path.home() / ".llm-router" / "usage.json").read_text())

        def _frac(k: str) -> float:
            v = float(data.get(k, 0.0))
            return v / 100.0 if v > 1.0 else v

        return {"sonnet": _frac("sonnet_pct"), "weekly": _frac("weekly_pct")}
    except Exception:
        return {"sonnet": 0.0, "weekly": 0.0}


def _downgrade_pending_for_pressure(pending: dict) -> dict:
    """Downgrade pending route complexity if subscription budget is exhausted.

    When Sonnet or weekly pressure ≥95%, reduce task complexity to stay within
    cheaper model tiers (complex→moderate, moderate→simple).

    Preserves the original requested_complexity for mismatch tracking:
    - If requested_complexity is already set (from auto-route), keep it
    - If not set, save current complexity as requested before downgrading

    Args:
        pending: Routing directive dict with 'complexity' key

    Returns:
        Updated pending dict (original if no downgrade needed)
    """
    pressure = _read_pressure()
    if pressure["sonnet"] < 0.95 and pressure["weekly"] < 0.95:
        return pending

    complexity = pending.get("complexity", "simple")
    # Preserve the original requested_complexity if not already set
    result = dict(pending)
    if "requested_complexity" not in result:
        result["requested_complexity"] = complexity

    if complexity == "complex":
        return {**result, "complexity": "moderate"}
    if complexity == "moderate":
        return {**result, "complexity": "simple"}
    return result


def _tool_history_path(session_id: str) -> Path:
    """Path to tool call history for loop detection."""
    return _ROUTER_DIR / f"tool_history_{session_id}.json"


def _record_tool_call(session_id: str, tool_name: str) -> None:
    """Record tool call timestamp for loop detection."""
    try:
        path = _tool_history_path(session_id)
        data = _read_json_retry(path) or {"calls": []}

        # Keep only calls from last 2 minutes
        cutoff = time.time() - 120
        data["calls"] = [
            call for call in data.get("calls", [])
            if call.get("timestamp", 0) > cutoff
        ]

        # Add new call
        data["calls"].append({
            "tool": tool_name,
            "timestamp": time.time()
        })

        _write_json_atomic(path, data)
    except OSError:
        pass


def _detect_investigation_loop(session_id: str, tool_name: str) -> dict | None:
    """Detect if Claude is in an investigation loop (3+ same-tool calls in 2min).

    Returns: {"tool": name, "count": N} if loop detected, else None
    """
    try:
        path = _tool_history_path(session_id)
        data = _read_json_retry(path) or {"calls": []}

        # Count recent calls to this tool
        cutoff = time.time() - 120
        recent_calls = [
            call for call in data.get("calls", [])
            if call.get("tool") == tool_name and call.get("timestamp", 0) > cutoff
        ]

        if len(recent_calls) >= 3:
            return {"tool": tool_name, "count": len(recent_calls)}
        return None
    except OSError:
        return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    enforce = os.environ.get("LLM_ROUTER_ENFORCE", "").lower()
    if not enforce:
        # Fall back to ~/.llm-router/routing.yaml so users who set
        # `enforce: hard` there get the expected behaviour without
        # needing a separate env-var export.
        try:
            _yaml = _ROUTER_DIR / "routing.yaml"
            if _yaml.exists():
                for _line in _yaml.read_text().splitlines():
                    if _line.strip().startswith("enforce:"):
                        enforce = _line.split(":", 1)[1].strip().lower()
                        break
        except Exception:
            pass
    if not enforce:
        enforce = "smart"
    # shadow / off = pure observation (treat as off)
    if enforce in ("off", "shadow"):
        sys.exit(0)
    # suggest = soft (log violation but never block)
    if enforce == "suggest":
        enforce = "soft"

    session_id = hook_input.get("session_id", "")
    tool_name = hook_input.get("tool_name", "")

    if not session_id or not tool_name:
        sys.exit(0)

    pending = _read_pending(session_id)
    if pending is None:
        sys.exit(0)  # No routing directive was issued
    pending = _downgrade_pending_for_pressure(pending)

    # ── Session Budget Kill-Switch ────────────────────────────────────────────────
    # Check if this session has exceeded its LLM spend budget.
    # If so, hard-block all non-file tools to prevent runaway costs.
    session_budget_limit = float(os.environ.get("LLM_ROUTER_SESSION_BUDGET", "5.00"))
    session_spend_path = _ROUTER_DIR / f"session_{session_id}_spend.json"
    try:
        spend_data = _read_json_retry(session_spend_path) or {"total_usd": 0.0}
        session_spend = spend_data.get("total_usd", 0.0)

        if session_budget_limit > 0 and session_spend > session_budget_limit:
            # Hard block all non-file tools
            if tool_name not in {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS", "Bash"}:
                block_reason = (
                    f"[llm-router] SESSION BUDGET EXCEEDED\n\n"
                    f"  Spent:    ${session_spend:.2f}\n"
                    f"  Limit:    ${session_budget_limit:.2f}\n"
                    f"  Status:   🔴 HARD BLOCKED\n\n"
                    f"  To continue:\n"
                    f"  1. Contact your admin to reset the session budget\n"
                    f"  2. Or unset LLM_ROUTER_SESSION_BUDGET to disable the limit"
                )
                print(block_reason, file=sys.stderr)
                sys.exit(1)
        elif session_budget_limit > 0 and session_spend > (session_budget_limit * 0.8):
            # Warning at 80% threshold — don't block, just warn
            pct_used = (session_spend / session_budget_limit) * 100
            # Inject warning into context via env var for hook consumer
            os.environ["_SESSION_BUDGET_WARNING"] = f"⚠️  Session budget at {pct_used:.0f}% (${session_spend:.2f}/${session_budget_limit:.2f})"
    except (OSError, ValueError):
        pass  # If spend tracking file doesn't exist or is invalid, allow the call

    # Session-type check: if Claude has called Edit/Write in this session already,
    # it's confirmed coding work — downgrade enforcement to soft for the whole session.
    if _is_coding_session(session_id) and enforce in ("smart", "hard"):
        enforce = "soft"

    expected_tool = pending.get("expected_tool", "llm_route")
    expected_server = pending.get("expected_server", "")  # for MCP server routing
    task_type = pending.get("task_type", "?")
    complexity = pending.get("complexity", "?")

    # ── Routing satisfied checks ──────────────────────────────────────────────

    # Tool names may be short ("llm_query") or fully-qualified MCP names
    # ("mcp__llm-router__llm_query") — accept both forms.
    bare_name = tool_name.split("__")[-1] if "__" in tool_name else tool_name

    # 1. Any llm_* tool honors routing (llm_code, llm_query, llm_route, etc.)
    if bare_name.startswith("llm_"):
        _clear_pending(session_id)
        _clear_violation_count(session_id)  # Reset violations on successful routing
        sys.exit(0)

    # 2. Exact match on the expected tool (e.g. mcp__obsidian__create_note)
    if tool_name == expected_tool or bare_name == expected_tool.split("__")[-1]:
        _clear_pending(session_id)
        _clear_violation_count(session_id)  # Reset violations on successful routing
        sys.exit(0)

    # 3. MCP server routing: any tool from the expected server satisfies the directive
    #    e.g. expected_server="obsidian" → mcp__obsidian__search clears state
    if expected_server and tool_name.startswith(f"mcp__{expected_server}__"):
        _clear_pending(session_id)
        _clear_violation_count(session_id)  # Reset violations on successful routing
        sys.exit(0)

    # ── Early file-operation detection ────────────────────────────────────────
    # Mark session as coding on first file-operation tool (Read/Edit/Write/etc).
    # This prevents stuck patterns where investigation tools keep failing.
    # Soft-fail: allow the operation, mark session as coding, clear pending state.
    # All subsequent tool calls in this session will bypass enforcement.
    if tool_name in {"Edit", "Write", "MultiEdit", "Read", "Glob", "Grep", "LS"}:
        _mark_session_coding(session_id)
        _clear_pending(session_id)
        _clear_violation_count(session_id)
        sys.exit(0)

    # ── Blocklist check ───────────────────────────────────────────────────────
    # For code tasks: only Bash/Edit/Write are blocked (file reads are needed).
    # For Q&A tasks: also block Read/Glob/Grep/LS — Claude reading files and
    # reasoning about them is equivalent to answering directly; the file
    # contents should be passed to llm_analyze/llm_query instead.
    if tool_name not in _block_tools_for(task_type):
        sys.exit(0)

    # ── Work tool used before routing ─────────────────────────────────────────
    _record_tool_call(session_id, tool_name)  # Track for loop detection
    _log_violation(session_id, tool_name, expected_tool)
    violation_count = _increment_violation_count(session_id)

    # Detect investigation loops (same tool called 3+ times in 2 minutes)
    loop_detected = _detect_investigation_loop(session_id, tool_name)

    if enforce == "soft":
        sys.exit(0)  # soft mode: logged, allowed

    # ── Stuck-pattern detection: auto-pivot after 2 violations ───────────────────
    # If Claude has already violated the routing directive twice in this session,
    # auto-downgrade to soft enforcement to prevent deadlocks and allow routing.
    # This avoids the stuck pattern where investigation tools keep failing.
    if violation_count >= 2:
        _mark_session_coding(session_id)  # Downgrade enforcement for rest of session
        sys.exit(0)  # Allow this tool call; soft-fail it

    if enforce == "smart":
        # In smart mode, NEVER block Read/Glob/Grep/LS.
        # Blocking file-read tools caused irrecoverable deadlocks where Claude
        # couldn't read the hook source to fix it. The cost saving from blocking
        # reads is low; the deadlock risk is too high.
        # Only Bash/Edit/Write are blocked in smart mode (prevents direct answers
        # and code execution, but keeps investigation tools available).
        if tool_name not in _BASE_BLOCK_TOOLS:
            sys.exit(0)
        if task_type not in _QA_TASK_TYPES:
            sys.exit(0)  # code task in smart mode — allow file tools
        # Fall through to hard block for Q&A tasks (Bash/Edit/Write only)

    # Hard mode: block with clear remediation instructions
    is_file_reader = tool_name in _QA_ONLY_BLOCK_TOOLS

    # Context-aware remediation guidance
    if task_type in ("research", "research/web"):
        action = (
            f"  1. Call {expected_tool}(prompt=\"{{'Use the user request as-is'}}\") with the query.\n"
            f"  2. Return the search results or analysis directly from the cheap model.\n"
            f"  3. Reasoning about web results yourself defeats the point — let the cheap model do it."
        )
    elif task_type in ("query", "analyze"):
        action = (
            f"  1. Call {expected_tool}(prompt=\"{{'User request here'}}\") for the analysis.\n"
            f"  2. Return the result as-is — do not re-analyze.\n"
            f"  3. Reading and reasoning yourself = full cost; routing = cost saving."
        )
    elif task_type in ("generate", "code"):
        action = (
            f"  1. Call {expected_tool}(prompt=\"{{'User request here'}}\") to generate the solution.\n"
            f"  2. Return its output without modification.\n"
            f"  3. Do NOT generate your own solution — use the routed model."
        )
    elif is_file_reader:
        action = (
            f"  1. Extract the file content and pass it to {expected_tool}.\n"
            f"     Example: {expected_tool}(prompt=\"analyze this\", context=file_content)\n"
            f"  2. Do NOT use {tool_name} to reason about files — pass content to cheap model.\n"
            f"     That avoids expensive token burn on analysis Ollama can handle free."
        )
    else:
        action = (
            f"  1. Call {expected_tool}(prompt=\"...\") with the user's actual request.\n"
            f"  2. Return its output — do not bypass the router.\n"
            f"  3. Reason: {task_type} tasks are routed for cost efficiency."
        )

    # Show violation count and escalation path
    escalation = ""
    if violation_count == 1:
        escalation = f"\n⚠️  Violation 1/2 — One more blocked tool will auto-downgrade enforcement & allow routing."
    elif violation_count >= 2:
        escalation = f"\n🔴 Violation {violation_count}/2+ — This session will auto-downgrade to soft enforcement after this turn.\n" \
                     f"    CALL {expected_tool} NOW to avoid being soft-blocked."

    # Detect investigation loops (same tool called 3+ times in 2 minutes)
    loop_warning = ""
    if loop_detected:
        loop_warning = (
            f"\n🔄 INVESTIGATION LOOP DETECTED: {tool_name} called {loop_detected['count']} times in 2 minutes\n"
            f"    This is a stuck pattern. You are retrying the same approach.\n"
            f"    Call {expected_tool} immediately to break the loop."
        )

    # Show routing window countdown
    remaining = pending.get("_remaining_seconds", _PENDING_TTL)
    window_warning = ""
    if remaining < 15:
        window_warning = f"\n⏰ ROUTING WINDOW CLOSING: {remaining}s remaining before directive expires"
    elif remaining < 30:
        window_warning = f"\n⏰ Routing window: {remaining}s remaining"

    block_reason = (
        f"[llm-router] Routing directive BLOCKED.{escalation}{loop_warning}{window_warning}\n\n"
        f"  Directive:     ⚡ MANDATORY ROUTE: {task_type}/{complexity} → call {expected_tool}\n"
        f"  Tool attempted: {tool_name}\n"
        f"  Session violations: {violation_count} this session\n\n"
        f"WHY THIS MATTERS:\n"
        f"  Routing saves 50–100x on this task. Using {tool_name} instead of {expected_tool}\n"
        f"  burns full model cost with no savings. For {complexity} tasks, that's expensive.\n\n"
        f"NEXT STEP (required):\n"
        f"{action}\n\n"
        f"Debug options:\n"
        f"  • View compliance log: {_LOG_PATH}\n"
        f"  • Soft-fail for testing: export LLM_ROUTER_ENFORCE=soft\n"
        f"  • Disable entirely: export LLM_ROUTER_ENFORCE=off"
    )

    json.dump({"decision": "block", "reason": block_reason}, sys.stdout)


if __name__ == "__main__":
    main()
