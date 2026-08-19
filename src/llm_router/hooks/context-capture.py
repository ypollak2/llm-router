#!/usr/bin/env python3
# llm_router-hook-version: 1
"""PostToolUse hook — capture tool activity into the Session Context
Accumulator's durable per-session store.

Part of the Session Context Accumulator: every routed model call (both the
MCP server path's build_context_messages and the hook draft path's
execute_chain/execute_agent) is meant to see what actually happened this
session instead of fabricating it. record_event() below is that write side;
session_store.build_session_context() (read side) is consumed from
auto-route.py and llm_router.context.

Fail-open throughout: any error here must never block the tool call it fires
after. Always exits 0, matching agent-depth-release.py's structural pattern.
"""

from __future__ import annotations

import json
import os
import sys

# Tools whose PostToolUse activity is not useful session context — either
# internal llm_router routing calls (mcp__llm_router__*, which would otherwise
# self-poison the very context they're meant to build) or high-frequency /
# low-signal UI tools that would drown out real work.
_NOISY_TOOL_PREFIXES = ("mcp__llm_router__",)
_NOISY_TOOLS = {
    "TodoWrite",
    "TodoRead",
    "BashOutput",
    "KillShell",
}
_NOISY_TOOL_SUBSTRINGS = ("screenshot", "zoom", "computer")

_MIN_CONTENT_CHARS = 20


def _get_session_id() -> str | None:
    env_session = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if env_session:
        return env_session
    try:
        from llm_router import session_store

        return session_store.resolve_session_id()
    except Exception:
        return None


def _stringify(value: object, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except Exception:
            text = str(value)
    return text[:limit]


def _is_noisy_tool(tool_name: str) -> bool:
    if not tool_name:
        return True
    if any(tool_name.startswith(p) for p in _NOISY_TOOL_PREFIXES):
        return True
    if tool_name in _NOISY_TOOLS:
        return True
    lowered = tool_name.lower()
    return any(s in lowered for s in _NOISY_TOOL_SUBSTRINGS)


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    if not isinstance(hook_input, dict):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    if _is_noisy_tool(tool_name):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {}) or {}
    tool_result = hook_input.get("tool_response", hook_input.get("tool_result", ""))

    inputs_str = _stringify(tool_input, 200)
    result_str = _stringify(tool_result, 500)

    # Never let an already-injected context block get re-recorded — that's
    # exactly the self-poisoning loop the sentinel wrapper exists to prevent.
    try:
        from llm_router.session_store import SENTINEL_OPEN

        if SENTINEL_OPEN in result_str or SENTINEL_OPEN in inputs_str:
            sys.exit(0)
    except Exception:
        pass

    content = f"{tool_name}({inputs_str}) -> {result_str}"

    try:
        from llm_router.compaction import collapse_whitespace, truncate_long_code

        content = collapse_whitespace(content)
        content = truncate_long_code(content)
    except Exception:
        pass  # best-effort compaction only; record_event compacts too

    if len(content.strip()) < _MIN_CONTENT_CHARS:
        sys.exit(0)

    session_id = _get_session_id()
    if not session_id:
        sys.exit(0)

    try:
        from llm_router import session_store

        session_store.record_event(
            session_id,
            "tool_call",
            content,
            role="tool",
            tool=tool_name,
        )
    except Exception:
        pass  # fail-open — never blocks the tool call this hook fires after

    sys.exit(0)


if __name__ == "__main__":
    main()
