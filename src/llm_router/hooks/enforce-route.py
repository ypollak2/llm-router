#!/usr/bin/env python3
# llm-router-hook-version: 3
"""PreToolUse[*] hook — enforce routing compliance.

When auto-route.py issues a ⚡ MANDATORY ROUTE directive, it writes a
pending state file to ~/.llm-router/pending_route_{session_id}.json.

This hook fires before every tool call and:
  1. If no pending state → allow (no routing was requested for this prompt).
  2. If the tool is an llm_* MCP tool → routing honored, clear state, allow.
  3. If the tool exactly matches the expected_tool in pending state → allow + clear.
     (Supports MCP server routing, e.g. mcp__obsidian__create_note)
  4. If the tool is NOT in _BLOCK_TOOLS → allow unconditionally.
     This covers: ToolSearch, Read, Glob, all mcp__* tools, Agent (schema load), etc.
  5. If the tool IS in _BLOCK_TOOLS → enforce based on LLM_ROUTER_ENFORCE:
       hard (default)   — block the call with a remediation message.
       soft             — log the violation, allow the call.
       off              — allow all calls regardless.

Blocklist approach (not allowlist): only tools where Claude is doing the work
itself (Bash, Edit, Write) need to be blocked. Everything else — including other
MCP plugins — is already routing away from Claude and should pass through freely.

Compliance log: ~/.llm-router/enforcement.log
Pending state:  ~/.llm-router/pending_route_{session_id}.json

Environment variables:
  LLM_ROUTER_ENFORCE  hard | soft | off   (default: hard)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_ROUTER_DIR = Path.home() / ".llm-router"
_LOG_PATH = _ROUTER_DIR / "enforcement.log"

# Blocklist: ONLY these tools are blocked before routing is satisfied.
# Everything else — ToolSearch, Read, Glob, all mcp__* tools from any plugin —
# passes through freely. This prevents deadlocks with schema discovery and
# ensures other MCP plugins (Obsidian, GitHub, etc.) are never blocked.
_BLOCK_TOOLS = frozenset({
    "Bash", "Edit", "MultiEdit", "Write", "NotebookEdit",
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

    enforce = os.environ.get("LLM_ROUTER_ENFORCE", "hard").lower()
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
        sys.exit(0)

    # 2. Exact match on the expected tool (e.g. mcp__obsidian__create_note)
    if tool_name == expected_tool or bare_name == expected_tool.split("__")[-1]:
        _clear_pending(session_id)
        sys.exit(0)

    # 3. MCP server routing: any tool from the expected server satisfies the directive
    #    e.g. expected_server="obsidian" → mcp__obsidian__search clears state
    if expected_server and tool_name.startswith(f"mcp__{expected_server}__"):
        _clear_pending(session_id)
        sys.exit(0)

    # ── Blocklist check — only block "Claude doing the work itself" tools ─────
    # Everything NOT in _BLOCK_TOOLS passes through unconditionally:
    # ToolSearch, Read, Glob, Grep, LS, all mcp__* tools, TodoWrite, etc.
    if tool_name not in _BLOCK_TOOLS:
        sys.exit(0)

    # ── Work tool used before routing ─────────────────────────────────────────
    _log_violation(session_id, tool_name, expected_tool)

    if enforce != "hard":
        sys.exit(0)  # soft mode: logged, allowed

    # Hard mode: block with clear remediation instructions
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
