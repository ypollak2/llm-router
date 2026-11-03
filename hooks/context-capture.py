#!/usr/bin/env python3
# llm_router-hook-version: 3
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


# Tools whose OUTPUT is what a later prompt refers back to ("what did the tests
# say", "which branch am I on"), versus tools whose output is bulk the prompt
# never cites.
_HIGH_SIGNAL = frozenset({"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"})


def _capture_budget(tool_name: str) -> tuple[int, int]:
    """(input_chars, result_chars) to store for this tool."""
    if tool_name in _HIGH_SIGNAL:
        return 600, 1800
    return 200, 500


def _is_noisy_tool(tool_name: str) -> bool:
    if not tool_name:
        return True
    if any(tool_name.startswith(p) for p in _NOISY_TOOL_PREFIXES):
        return True
    if tool_name in _NOISY_TOOLS:
        return True
    lowered = tool_name.lower()
    return any(s in lowered for s in _NOISY_TOOL_SUBSTRINGS)


# -- stdout is the host's JSON channel: library logging goes to stderr (docs/BUGS.md BIND-1) --------
# structlog's default sink is STDOUT. A log line in front of (or after) this hook's JSON makes Claude Code read
# the whole output as plain text and drop the decision or context it carried; agent-route.py lost 317 of 317
# Codex handoffs that way. configure_logging_lazily() points structlog at stdlib logging (stderr) the moment it
# is first imported, as auto-route.py does. The fallback covers a host without the llm_router package.
def _init_hook_logging() -> None:
    # LiteLLM keeps its own handler, which writes every record below WARNING to sys.stdout (looked up when the record
    # is emitted) and takes its level, DEBUG by default, from LITELLM_LOG when it is imported. ERROR keeps it off the
    # decision channel, and the hook FORCES it: an explicit LITELLM_LOG=DEBUG in the user's environment used to win
    # here, put LiteLLM's records in front of this hook's JSON, and the host dropped the whole output
    # (docs/bugs/BIND-10.md). The variable is set before the import, and the loggers on their own in case LiteLLM is
    # already imported. Only the script entry changes the process: a test that calls main() must not.
    if __name__ == "__main__":
        import logging as _ll_logging
        import os as _ll_os

        _ll_os.environ.update(LITELLM_LOG="ERROR")
        for _ll_name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            _ll_logging.getLogger(_ll_name).setLevel(_ll_logging.ERROR)
    try:
        from llm_router.logging import configure_logging_lazily

        configure_logging_lazily()
    except Exception:  # noqa: BLE001 -- logging setup must never abort the hook
        import logging as _logging

        _root = _logging.getLogger()
        if not _root.handlers:
            _root.addHandler(_logging.StreamHandler(sys.stderr))


def main() -> None:
    _init_hook_logging()
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

    # PLAN v16 P1.5 task 2: an edited source file goes on the semantic index's
    # dirty queue (one appended line; the re-index runs in a detached child every
    # 10 files). Before the session-id and content checks below, because whether
    # to re-index has nothing to do with whether the event is worth recording.
    if tool_name in ("Edit", "Write", "MultiEdit") and isinstance(tool_input, dict):
        try:
            from llm_router.semantic import refresh

            _edited = tool_input.get("file_path")
            if isinstance(_edited, str) and _edited:
                refresh.note_edit(_edited, hook_input.get("cwd") or os.getcwd())
        except Exception:
            pass  # fail-open — a missed re-index is picked up by the next full index

    # 200/500 was uniform across every tool, and it bit: measured 2026-09-15 on
    # 371 stored events, 29% sat at the 700-char ceiling, p90 710, max 782. The
    # token that makes a tool call worth remembering — a filename, a branch, an
    # exit code — is routinely what gets cut.
    #
    # Widened only for the tools whose content a later prompt actually points at.
    # A Read of a 2,000-line file still gets the narrow budget: its VALUE is the
    # path, which is in the input, not the body.
    _in_cap, _out_cap = _capture_budget(tool_name)
    inputs_str = _stringify(tool_input, _in_cap)
    result_str = _stringify(tool_result, _out_cap)

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

    # OKF-INDEX-01: also enrich the knowledge store from Claude's OWN work.
    # Enrichment used to happen only on a successful routed call, which is a
    # deadlock — the store stays empty because nothing routes, and nothing routes
    # because the store is empty. `okf index` seeds it; this keeps it current as
    # files change, without waiting for a routed answer that may never come.
    #
    # Same verified-only material as every other writer: real paths and symbol
    # names pulled by the shared extractors, never prose. A tool call that yields
    # no extractable structure is skipped rather than summarised.
    try:
        from llm_router import okf

        files, symbols = okf._extract_files_and_symbols("", content)
        if files and symbols:
            # OKF-SCOPE-02: say which project. This hook fires on EVERY tool call,
            # so it is the highest-volume writer in the system — and it calls
            # `_write_source_concept` directly, bypassing `enrich_from_response`.
            # Leaving `root` unset here would keep that volume on the cwd fallback
            # while the unit tests around the function itself went green.
            okf._write_source_concept(
                files[0], "Defines: " + ", ".join(symbols), symbols, "",
                okf._knowledge_dir(), root=okf.project_root(),
            )
            okf.invalidate_cache()
    except Exception:
        pass  # fail-open — enrichment is never worth failing a tool call over

    sys.exit(0)


if __name__ == "__main__":
    main()
