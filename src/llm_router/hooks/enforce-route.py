#!/usr/bin/env python3
# llm-router-hook-version: 1
"""PreToolUse[*] hook — enforce routing compliance.

When auto-route.py issues a ⚡ MANDATORY ROUTE directive, it writes a
pending state file to ~/.llm-router/pending_route_{session_id}.json.

This hook fires before every tool call and:
  1. If no pending state → allow (no routing was requested for this prompt).
  2. If the tool is an llm_* MCP tool → routing honored, clear state, allow.
  3. If the tool is context-gathering (Read, Glob, Grep, LS) → allow.
  4. Otherwise → enforce based on LLM_ROUTER_ENFORCE:
       soft  (default) — log the violation, allow the call.
       hard             — block the call with a remediation message.
       off              — allow all calls regardless.

Compliance log: ~/.llm-router/enforcement.log
Pending state:  ~/.llm-router/pending_route_{session_id}.json

Environment variables:
  LLM_ROUTER_ENFORCE  soft | hard | off   (default: soft)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_ROUTER_DIR = Path.home() / ".llm-router"
_LOG_PATH = _ROUTER_DIR / "enforcement.log"

# Tools that gather context — always allowed before routing
_CONTEXT_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead", "ListMcpResourcesTool",
    "ReadMcpResourceTool", "TodoRead", "mcp__serena__read_file",
    "mcp__serena__find_file", "mcp__serena__list_dir",
    "mcp__serena__search_for_pattern", "mcp__serena__get_symbols_overview",
    "mcp__serena__find_symbol", "mcp__serena__find_referencing_symbols",
})

# Tools that do actual work — blocked in hard mode before routing
_WORK_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
})


def _pending_path(session_id: str) -> Path:
    return _ROUTER_DIR / f"pending_route_{session_id}.json"


def _read_pending(session_id: str) -> dict | None:
    p = _pending_path(session_id)
    try:
        data = json.loads(p.read_text())
        # Expire state after 5 minutes (stale if Claude skipped routing)
        if time.time() - data.get("issued_at", 0) > 300:
            p.unlink(missing_ok=True)
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError):
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


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    enforce = os.environ.get("LLM_ROUTER_ENFORCE", "soft").lower()
    if enforce == "off":
        sys.exit(0)

    session_id = hook_input.get("session_id", "")
    tool_name = hook_input.get("tool_name", "")

    if not session_id or not tool_name:
        sys.exit(0)

    pending = _read_pending(session_id)
    if pending is None:
        sys.exit(0)  # No routing directive was issued

    expected_tool = pending.get("expected_tool", "llm_route")
    task_type = pending.get("task_type", "?")
    complexity = pending.get("complexity", "?")

    # Routing honored — LLM called an llm_* tool
    if tool_name.startswith("llm_"):
        _clear_pending(session_id)
        sys.exit(0)

    # Context-gathering tools are always allowed (reading files to understand context)
    if tool_name in _CONTEXT_TOOLS:
        sys.exit(0)

    # Work tool before routing — handle per enforce mode
    _log_violation(session_id, tool_name, expected_tool)

    if enforce != "hard":
        sys.exit(0)  # soft mode: logged, allowed

    # Hard mode: block
    block_reason = (
        f"[llm-router] Routing directive violated.\n\n"
        f"  Directive:     ⚡ MANDATORY ROUTE: {task_type}/{complexity} → call {expected_tool}\n"
        f"  Tool blocked:  {tool_name}\n\n"
        f"REQUIRED ACTION:\n"
        f"  1. Call {expected_tool}(prompt=\"...\") with the user's request as the prompt.\n"
        f"  2. Return its output as your response.\n"
        f"  3. Do NOT use {tool_name} directly to answer — the cheap model does the work.\n\n"
        f"To allow this call without routing, set LLM_ROUTER_ENFORCE=soft or off.\n"
        f"Compliance log: {_LOG_PATH}"
    )

    json.dump({"decision": "block", "reason": block_reason}, sys.stdout)


if __name__ == "__main__":
    main()
