"""One reader for a PostToolUse payload, across every host that sends one.

Hosts disagree about the spelling. Claude Code sends snake_case
(``tool_name`` / ``tool_input`` / ``tool_response``); some others send camelCase
(``toolName`` / ``toolInputs`` / ``toolResult``). Hooks in this tree had picked
one spelling each and split down the middle: context-capture, library-harvest
and cc-usage-track read snake_case and work, while bash-compress and
playwright-compress read camelCase and therefore never fired on Claude Code at
all — which is why ``compression_stats`` had no rows, and no table.

A hook that guesses wrong exits 0 and prints nothing, so the failure is
invisible from the outside: it looks exactly like "there was nothing to
compress". Hence a single reader rather than a fix applied twice.

This module is importable — it has no hyphen in its name, unlike the hook
scripts themselves.
"""
from __future__ import annotations

import json
from typing import Any

_NAME_KEYS = ("tool_name", "toolName")
_INPUT_KEYS = ("tool_input", "toolInputs", "toolInput")
_RESULT_KEYS = ("tool_response", "tool_result", "toolResult")

# stdout first: Claude Code's Bash response carries the command output there, and
# `text` may also be present holding a formatted rendering of the same bytes.
_TEXT_KEYS = ("stdout", "text", "output", "content")


def _first(payload: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in payload:
            return payload[k]
    return default


def tool_name(payload: dict) -> str:
    """Bare tool name, MCP server prefix stripped.

    ``mcp__plugin_playwright__browser_snapshot`` -> ``browser_snapshot``.
    """
    name = _first(payload, _NAME_KEYS, "") or ""
    if not isinstance(name, str):
        return ""
    return name.split("__")[-1] if "__" in name else name


def is_tool(payload: dict, *names: str) -> bool:
    """Case-insensitive tool match.

    Claude Code's shell tool is ``Bash``; other hosts call it ``bash`` or
    ``execute_shell_command``. ``"Bash".endswith("bash")`` is False, and that
    single capital was enough to disable bash-compress on its main host.
    """
    actual = tool_name(payload).lower()
    return any(actual == n.lower() for n in names)


def tool_input(payload: dict) -> dict:
    """Tool arguments as a dict. A JSON string is parsed; anything else -> {}."""
    raw = _first(payload, _INPUT_KEYS, {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def tool_output(payload: dict) -> str:
    """The tool's textual output, however this host chose to wrap it.

    Handles a bare string, a dict with stdout/text/output, and the content-array
    shape (``[{"type": "text", "text": ...}]``). stderr is appended when present
    because a failing command's diagnosis is usually the part worth keeping.
    """
    result = _first(payload, _RESULT_KEYS, "")
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""

    out = ""
    for key in _TEXT_KEYS:
        val = result.get(key)
        if isinstance(val, str) and val:
            out = val
            break
        if isinstance(val, list):
            parts = [
                item.get("text", "")
                for item in val
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if any(parts):
                out = "\n".join(p for p in parts if p)
                break

    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        out = f"{out}\nSTDERR:\n{stderr}" if out else stderr
    return out
