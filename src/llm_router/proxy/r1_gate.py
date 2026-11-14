"""R1 "tool-call gate" for local-served SUB steps (flag ``LLM_ROUTER_PROXY_R1_GATE``, off by default).

PLAN-COND2-ADMIT90 phase C. A local reply is served only if it carries at least one
``tool_use`` block that names a tool offered in the request and whose input is a JSON
object holding every ``required`` key of that tool's ``input_schema`` (no deeper schema
check). Anything else is discarded BEFORE delivery and the step goes to Claude through the
proxy's existing local-failure fallback. Parser error text (ollama#17276, #18563) is
therefore never forwarded.

Reason codes: r1_text, r1_empty, r1_unknown_tool, r1_invalid_input, r1_parse_error.
"""

from __future__ import annotations

import json
import os
import re

R1_TEXT = "r1_text"
R1_EMPTY = "r1_empty"
R1_UNKNOWN_TOOL = "r1_unknown_tool"
R1_INVALID_INPUT = "r1_invalid_input"
R1_PARSE_ERROR = "r1_parse_error"

# Ollama's tool-call parser failure text, returned as the answer.
_PARSE_ERROR_RE = re.compile(
    r"error parsing tool call|failed to parse tool call|unexpected end of JSON input"
    r"|invalid character .{1,20} looking for beginning of",
    re.IGNORECASE)


def enabled() -> bool:
    # literal name below: env_registry's scan needs it
    return os.environ.get("LLM_ROUTER_PROXY_R1_GATE", "").strip().lower() in ("1", "on", "true", "yes")


def _offered(body: dict) -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    for t in body.get("tools") or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            schema = t.get("input_schema")
            out[t["name"]] = schema if isinstance(schema, dict) else None
    return out


def _input_ok(inp, schema: dict | None) -> bool:
    if isinstance(inp, str):  # a backend that left the arguments as JSON text
        try:
            inp = json.loads(inp)
        except (ValueError, RecursionError):
            return False
    if not isinstance(inp, dict):
        return False
    required = (schema or {}).get("required")
    if isinstance(required, list):
        return all(isinstance(k, str) and k in inp for k in required)
    return True


def classify_upstream(err, reason) -> str:
    """The r1 reason for a local attempt that failed before the gate ran. Reason codes only, never error text."""
    e = str(err or "")
    if e.startswith("unknown tool"):
        return R1_UNKNOWN_TOOL
    if e.startswith("tool args not JSON") or (reason or "validation") == "validation" and re.match(r"^\w+: ", e):
        return R1_INVALID_INPUT
    if e == "empty response":
        return R1_EMPTY
    return f"r1_other:{reason or 'validation'}"


def check(message: dict | None, body: dict) -> tuple[bool, str | None]:
    """``(serve, reason)``; ``reason`` is None when served. Never raises."""
    try:
        blocks = [b for b in (message or {}).get("content") or [] if isinstance(b, dict)]
        if any(b.get("type") == "text" and _PARSE_ERROR_RE.search(str(b.get("text") or "")) for b in blocks):
            return False, R1_PARSE_ERROR
        if not any((b.get("type") == "text" and str(b.get("text") or "").strip()) or b.get("type") == "tool_use"
                   for b in blocks):
            return False, R1_EMPTY
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        if not calls:
            return False, R1_TEXT
        offered = _offered(body)
        reason = None
        for c in calls:
            if c.get("name") not in offered:
                reason = reason or R1_UNKNOWN_TOOL
            elif not _input_ok(c.get("input"), offered[c["name"]]):
                reason = reason or R1_INVALID_INPUT
            else:
                return True, None  # >= 1 well-formed call serves; the gate does not vet every call
        return False, reason
    except Exception:  # noqa: BLE001 -- a gate error must not serve unchecked output
        return False, R1_INVALID_INPUT
