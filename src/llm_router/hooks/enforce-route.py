#!/usr/bin/env python3
# llm-router-hook-version: 9
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
  5. If the tool IS in the task-specific blocklist → enforce based on LLM_ROUTER_ENFORCE:
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
        if time.time() > expires:
            p.unlink(missing_ok=True)
            return None
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

    # ── Blocklist check ───────────────────────────────────────────────────────
    # For code tasks: only Bash/Edit/Write are blocked (file reads are needed).
    # For Q&A tasks: also block Read/Glob/Grep/LS — Claude reading files and
    # reasoning about them is equivalent to answering directly; the file
    # contents should be passed to llm_analyze/llm_query instead.
    if tool_name not in _block_tools_for(task_type):
        sys.exit(0)

    # ── Work tool used before routing ─────────────────────────────────────────
    _log_violation(session_id, tool_name, expected_tool)

    if enforce == "soft":
        sys.exit(0)  # soft mode: logged, allowed

    # Smart/hard: first Edit/Write in a session proves it's coding work.
    # Soft-fail: log, mark session as coding, clear pending state, allow the edit.
    # All subsequent tool calls in this session will bypass enforcement.
    if tool_name in {"Edit", "Write", "MultiEdit"}:
        _mark_session_coding(session_id)
        _clear_pending(session_id)
        sys.exit(0)

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
    if is_file_reader:
        action = (
            f"  1. Call {expected_tool}(prompt=\"...\", context=\"<paste file contents here>\").\n"
            f"  2. Do NOT use {tool_name} to read files and reason about them yourself —\n"
            f"     that is equivalent to answering directly. Pass the content to the cheap model."
        )
    else:
        action = (
            f"  1. Call {expected_tool}(prompt=\"...\") with the user's request as the prompt.\n"
            f"  2. Return its output as your response.\n"
            f"  3. Do NOT use {tool_name} directly to answer — the cheap model does the work."
        )
    block_reason = (
        f"[llm-router] Routing directive violated.\n\n"
        f"  Directive:     ⚡ MANDATORY ROUTE: {task_type}/{complexity} → call {expected_tool}\n"
        f"  Tool blocked:  {tool_name}\n\n"
        f"REQUIRED ACTION:\n"
        f"{action}\n\n"
        f"To allow this call without routing, set LLM_ROUTER_ENFORCE=soft.\n"
        f"Compliance log: {_LOG_PATH}"
    )

    json.dump({"decision": "block", "reason": block_reason}, sys.stdout)


if __name__ == "__main__":
    main()
