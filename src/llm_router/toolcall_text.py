"""Neutralise tool-call-shaped text in routed answers (F4, design D-139).

A routed model sometimes hallucinates tool use in its ANSWER: an XML function-call
block, ``<invoke>``/``<parameter>`` tags, a ``tool_use`` JSON object, or a
``Bash(...)``-style call line. The routed model cannot execute any of it, and an
orchestrating agent reading the answer might take it for an instruction.

``neutralise(text)`` removes those blocks and puts ONE visible marker line where the
first one was, carrying the exact count. It is never silent. Stdlib only, so a hook
could import it.

Rules (each one is pinned by tests/test_f4_toolcall_text.py):

* A block is XML (``function_calls`` / ``function_call`` / ``tool_calls`` / ``tool_call`` /
  ``tool_use`` / ``invoke``, with an optional ``antml:`` prefix) that really carries a
  call shape (``<invoke name=``, ``<parameter name=``, ``<name>`` or a ``"name":`` key);
  an unclosed one runs to the end of the text; or a JSON object that is a tool call
  (``type`` tool_use/function_call/tool_call plus ``name``, an OpenAI ``tool_calls`` or
  ``function_call`` member, or an assistant message whose content holds a tool_use); or a
  run of call lines at the start of a line (``Bash(...)``, ``Edit(file_path=...)``).
* Markdown code fences are CONTENT. A fenced shell command, a Python snippet, a config
  file that is the answer to "how do I..." is kept. A fence is removed only when its whole
  body is tool-call-shaped (an XML block or a tool_use JSON object), and then it counts as
  one block. Tool-call text inside a fence that also holds other content is kept.
* Indented code (4+ spaces), text after a backtick, bullets and prose that merely
  mention a tool are never touched.
* Adjacent blocks separated only by whitespace count as one block.
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

MARKER_TEMPLATE = (
    "[llm-router: removed {n} tool-call-shaped block(s) the routed model cannot execute]"
)
MARKER_LINE_RE = re.compile(
    r"^\[llm-router: removed (\d+) tool-call-shaped block\(s\) the routed model cannot execute\]$",
    re.MULTILINE,
)

_FENCE_RE = re.compile(
    r"^[ ]{0,3}(?P<f>`{3,}|~{3,})[^\n]*\n(?P<body>.*?)(?:^[ ]{0,3}(?P=f)[`~]*[ \t]*$\n?|\Z)",
    re.MULTILINE | re.DOTALL,
)
_XML_OPEN_RE = re.compile(
    r"<((?:antml:)?(?:function_calls|function_call|tool_calls|tool_call|tool_use|invoke))\b[^>]*>"
)
_XML_SHAPE_RE = re.compile(
    r"<(?:antml:)?(?:invoke|parameter)\b[^>]*\bname\s*=|<name>|\"name\"\s*:"
)
_CALL_LINE_RE = re.compile(
    r"^[ ]{0,3}(?:[●⏺▶•]\s+)?"
    r"(?P<tool>Bash|Edit|MultiEdit|Read|Write|Grep|Glob|Task|WebFetch|WebSearch|NotebookEdit|TodoWrite)"
    r"\((?P<args>.+)\)[ \t]*$"
)
_ARGS_SHAPED_RE = re.compile(r"""^\s*(?:\w+\s*=|\{|["']|[~./]|[\w-]+[./])""")
_TOOL_TYPES = {"tool_use", "function_call", "tool_call"}
_decoder = json.JSONDecoder()


def _is_tool_json(obj: Any) -> bool:
    if isinstance(obj, list):
        return bool(obj) and all(_is_tool_json(o) for o in obj)
    if not isinstance(obj, dict):
        return False
    if obj.get("type") in _TOOL_TYPES and "name" in obj:
        return True
    calls = obj.get("tool_calls")
    if isinstance(calls, list) and any(
        isinstance(c, dict)
        and ("name" in c or (isinstance(c.get("function"), dict) and "name" in c["function"]))
        for c in calls
    ):
        return True
    fc = obj.get("function_call")
    if isinstance(fc, dict) and "name" in fc:
        return True
    content = obj.get("content")
    return isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_use" and "name" in c for c in content
    )


def _xml_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        m = _XML_OPEN_RE.search(text, pos)
        if not m:
            return spans
        close = f"</{m.group(1)}>"
        i = text.find(close, m.end())
        end = len(text) if i < 0 else i + len(close)
        if m.start() > 0 and text[m.start() - 1] == "`":
            pos = end if i >= 0 else m.end()  # inline-code example: skip it whole
            continue
        if not _XML_SHAPE_RE.search(text[m.start():end]):
            pos = m.end()
            continue
        spans.append((m.start(), end))
        pos = end


def _json_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        start = pos + (len(line) - len(line.lstrip(" ")))
        pos += len(line)
        if len(line) - len(line.lstrip(" ")) > 3 or not line.lstrip(" ").startswith(("{", "[")):
            continue
        if spans and start < spans[-1][1]:
            continue
        try:
            obj, end = _decoder.raw_decode(text, start)
        except ValueError:
            continue
        if _is_tool_json(obj):
            spans.append((start, end))
    return spans


def _call_line_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        m = _CALL_LINE_RE.match(body)
        if m and (m.group("tool") == "Bash" or _ARGS_SHAPED_RE.match(m.group("args"))):
            spans.append((pos, pos + len(body)))
        pos += len(line)
    return spans


def _fence_is_tool_call(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    try:
        if _is_tool_json(json.loads(stripped)):
            return True
    except ValueError:
        pass
    spans = _xml_spans(body)
    if not spans:
        return False
    rest = body
    for a, b in reversed(spans):
        rest = rest[:a] + rest[b:]
    return not rest.strip()


def _find_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    plain: list[tuple[int, int]] = []
    for m in _FENCE_RE.finditer(text):
        plain.append((cursor, m.start()))
        cursor = m.end()
        if _fence_is_tool_call(m.group("body")):
            spans.append((m.start(), m.end()))
    plain.append((cursor, len(text)))
    for a, b in plain:
        seg = text[a:b]
        for finder in (_xml_spans, _json_spans, _call_line_spans):
            spans.extend((a + s, a + e) for s, e in finder(seg))
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and (s < merged[-1][1] or not text[merged[-1][1]:s].strip()):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def neutralise(text: str) -> tuple[str, int]:
    """Return ``(clean_text, n_blocks_removed)``; the text is unchanged when n is 0."""
    if not text:
        return text, 0
    spans = _find_spans(text)
    if not spans:
        return text, 0
    marker = MARKER_TEMPLATE.format(n=len(spans))
    out = text[: spans[0][0]].rstrip(" \t\n")
    out = f"{out}\n\n{marker}" if out else marker
    for i, (_, end) in enumerate(spans):
        nxt = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        kept = text[end:nxt].strip(" \t\n")
        if kept:
            out += "\n\n" + kept
    return out, len(spans)


def neutralise_response(resp: Any) -> Any:
    """Return ``resp`` with its ``content`` neutralised and the count recorded in
    ``toolcall_blocks_removed``; the same object when nothing was removed."""
    new, n = neutralise(getattr(resp, "content", "") or "")
    if not n:
        return resp
    return dataclasses.replace(resp, content=new, toolcall_blocks_removed=n)


def keep_marker(before: str, after: str) -> str:
    """A later text pass (the response router rewrites prose paragraphs) must not drop the
    marker: put it back at the top if ``before`` had it and ``after`` lost it."""
    m = MARKER_LINE_RE.search(before)
    if m and not MARKER_LINE_RE.search(after):
        return f"{m.group(0)}\n\n{after}"
    return after
