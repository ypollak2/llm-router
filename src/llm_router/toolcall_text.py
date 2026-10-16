"""Neutralise tool-call-shaped text in routed answers (F4, designs D-139 and D-152).

A routed model sometimes hallucinates tool use in its ANSWER: an XML function-call
block, ``<invoke>``/``<parameter>`` tags, a ``tool_use`` JSON object, or a
``Bash(...)``-style call line. The routed model cannot execute any of it, and an
orchestrating agent reading the answer might take it for an instruction.

``neutralise(text)`` removes those blocks and puts ONE visible marker line where the
first one was, carrying the exact count. It is never silent. The transform rewrites
answers, so a false positive is data loss: every rule below errs toward keeping text.

Rules (each one is pinned by tests/test_f4_toolcall_text.py):

* Markdown code fences are CONTENT and are ALWAYS kept: a fenced shell command, a
  snippet, or a fence that documents a tool-call format. A fence whose whole body is a
  tool call is only counted (``fenced_seen``), never removed.
* XML: ``function_calls`` / ``function_call`` / ``tool_calls`` / ``tool_call`` /
  ``tool_use`` / ``invoke`` (optional ``antml:`` prefix, any case) that carries an
  arguments shape (``<parameter name=``, ``<name>`` or a ``"name":`` key). An element
  with only ``name=`` (BPEL ``<invoke name=... />``) is not a tool call. An unclosed
  wrapper runs to the end of the text; an unclosed ``invoke`` stops at the next blank line.
* JSON object at the start of a line: ``type`` tool_use/function_call/tool_call plus
  ``name``, an OpenAI ``tool_calls``/``function_call`` member with a name, or an assistant
  message whose content holds a named tool_use.
* Call lines: a line that IS the whole call (``Bash(command="ls")``) and either uses known
  ``key=value`` arguments or sits in an answer with another tool-call signal. A single
  positional call (``Write("hello world")``) alone is kept. A contiguous run is one block.
* Never touched: text indented 4+ spaces, text right after a backtick, bullets, prose.
* Adjacent blocks separated only by whitespace count as one.
* Fails open: any exception returns the text unchanged and records it with ``failopen``.
* Work is bounded: only the first 256 KB are scanned (cut on a line), every pass is
  linear, JSON decoding is limited to 64 attempts of 64 KB each.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from bisect import bisect_left, bisect_right
from typing import Any, NamedTuple

MARKER_TEMPLATE = (
    "[llm-router: removed {n} tool-call-shaped block(s) the routed model cannot execute]"
)
MARKER_LINE_RE = re.compile(
    r"^\[llm-router: removed (\d+) tool-call-shaped block\(s\) the routed model cannot execute\]$",
    re.MULTILINE,
)
SCAN_CAP = 256 * 1024
_JSON_WINDOW = 64 * 1024
_JSON_ATTEMPTS = 64
LEDGER_NAME = "toolcall_neutralised.jsonl"


class Neutralised(NamedTuple):
    text: str
    removed: int
    fenced_seen: int


_NAMES = "function_calls|function_call|tool_calls|tool_call|tool_use|invoke"
_WRAPPERS = {"function_calls", "function_call", "tool_calls", "tool_call", "tool_use"}
_OPEN_RE = re.compile(rf"<((?:antml:)?(?:{_NAMES}))\b[^>]{{0,500}}>", re.I)
_CLOSE_RE = re.compile(rf"</((?:antml:)?(?:{_NAMES}))\s*>", re.I)
_SHAPE_RE = re.compile(r"<(?:antml:)?parameter\b[^>]{0,200}\bname\s*=|<name>|\"name\"\s*:", re.I)
_BLANK_RE = re.compile(r"\n[ \t]*\r?\n")
_JSON_MARK_RE = re.compile(r'"(?:tool_use|tool_calls|function_call|tool_call)"', re.I)
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CALL_RE = re.compile(
    r"^ {0,3}(?:[●⏺▶•]\s+)?"
    r"(?P<tool>Bash|Edit|MultiEdit|Read|Write|Grep|Glob|Task|WebFetch|WebSearch|NotebookEdit|TodoWrite)"
    r"\((?P<args>.+)\)[ \t]*$"
)
_KV_ARGS_RE = re.compile(
    r"^\s*(?:(?:command|file_path|path|pattern|old_string|new_string|content|url|query|prompt|"
    r"description|subagent_type|notebook_path|todos|edits|replace_all|timeout|"
    r"run_in_background|offset|limit|glob|output_mode)\s*=|\{\s*\")"
)
_TOOL_TYPES = {"tool_use", "function_call", "tool_call"}
_decoder = json.JSONDecoder()
_LEAD_BLANKS_RE = re.compile(r"\A(?:[ \t]*\r?\n)+")
_TRAIL_BLANKS_RE = re.compile(r"(?:\r?\n[ \t]*)+\Z")


def _trim_blank_lines(s: str) -> str:
    """Drop blank lines at both ends, keeping the indentation of the first real line."""
    return _TRAIL_BLANKS_RE.sub("", _LEAD_BLANKS_RE.sub("", s))


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


def _xml_spans(s: str) -> list[tuple[int, int]]:
    closers: dict[str, list[int]] = {}
    for m in _CLOSE_RE.finditer(s):
        closers.setdefault(m.group(1).lower(), []).append(m.end())
    shape = [m.start() for m in _SHAPE_RE.finditer(s)]
    blanks = [m.start() for m in _BLANK_RE.finditer(s)]
    newlines = [m.start() for m in re.finditer("\n", s)]
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        m = _OPEN_RE.search(s, pos)
        if not m:
            return spans
        pos = m.end()
        if m.group(0).endswith("/>"):
            continue
        name = m.group(1).lower().removeprefix("antml:")
        ends = closers.get(m.group(1).lower(), [])
        j = bisect_right(ends, m.end())
        closed = j < len(ends)
        k = bisect_left(newlines, m.start())
        line_start = newlines[k - 1] + 1 if k else 0
        lead = s[line_start:line_start + 5]
        if lead.startswith("    ") or lead.startswith("\t"):
            continue  # indented code is content
        if closed:
            end = ends[j]
        elif name in _WRAPPERS:
            end = len(s)
        else:
            b = bisect_right(blanks, m.end() - 1)
            end = blanks[b] if b < len(blanks) else len(s)
        if m.start() > 0 and s[m.start() - 1] == "`":
            pos = end if closed else m.end()  # inline-code example: skip it whole
            continue
        p = bisect_left(shape, m.start())
        if p >= len(shape) or shape[p] >= end:
            continue
        spans.append((m.start(), end))
        pos = end


def _json_spans(s: str) -> list[tuple[int, int]]:
    marks = [m.start() for m in _JSON_MARK_RE.finditer(s)]
    if not marks:
        return []
    spans: list[tuple[int, int]] = []
    attempts = 0
    pos = 0
    for line in s.splitlines(keepends=True):
        line_start = pos
        pos += len(line)
        if spans and line_start < spans[-1][1]:
            continue
        body = line.lstrip(" ")
        if len(line) - len(body) > 3 or not body.startswith(("{", "[")):
            continue
        start = line_start + (len(line) - len(body))
        i = bisect_left(marks, start)
        if i >= len(marks) or marks[i] >= start + _JSON_WINDOW:
            continue
        attempts += 1
        if attempts > _JSON_ATTEMPTS:
            break
        try:
            obj, n = _decoder.raw_decode(s[start:start + _JSON_WINDOW])
        except (ValueError, RecursionError):
            continue
        if _is_tool_json(obj):
            spans.append((start, start + n))
    return spans


def _call_line_spans(s: str, signal: bool) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    run: list[tuple[int, int, bool]] = []
    prev_end = -1
    pos = 0

    def flush() -> None:
        if run and (signal or any(kv for _, _, kv in run)):
            spans.append((run[0][0], run[-1][1]))
        run.clear()

    for line in s.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        m = _CALL_RE.match(body)
        if m and pos != prev_end and run:
            flush()
        if m:
            run.append((pos, pos + len(body), bool(_KV_ARGS_RE.match(m.group("args")))))
            prev_end = pos + len(line)
        else:
            flush()
        pos += len(line)
    flush()
    return spans


def _fence_is_tool_call(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    try:
        if _is_tool_json(json.loads(stripped)):
            return True
    except (ValueError, RecursionError):
        pass
    spans = _xml_spans(body)
    if not spans:
        return False
    rest = body
    for a, b in reversed(spans):
        rest = rest[:a] + rest[b:]
    return not rest.strip()


def _segments(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split into plain ranges and fence BODY ranges, line by line (linear)."""
    plain: list[tuple[int, int]] = []
    bodies: list[tuple[int, int]] = []
    cursor = pos = 0
    fence: tuple[str, int] | None = None
    body_start = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if fence is None:
            m = _FENCE_OPEN_RE.match(stripped)
            if m:
                plain.append((cursor, pos))
                fence = (m.group(1)[0], len(m.group(1)))
                body_start = pos + len(line)
        else:
            c = stripped.lstrip(" ")
            core = c.rstrip(" \t")
            if len(stripped) - len(c) <= 3 and core and set(core) == {fence[0]} and len(core) >= fence[1]:
                bodies.append((body_start, pos))
                fence = None
                cursor = pos + len(line)
        pos += len(line)
    if fence is not None:
        bodies.append((body_start, pos))
        cursor = pos
    plain.append((cursor, len(text)))
    return plain, bodies


def _find_spans(text: str) -> tuple[list[tuple[int, int]], int]:
    plain, bodies = _segments(text)
    fenced_seen = sum(1 for a, b in bodies if _fence_is_tool_call(text[a:b]))
    spans: list[tuple[int, int]] = []
    segs = [(a, b) for a, b in plain if b > a]
    for a, b in segs:
        seg = text[a:b]
        for finder in (_xml_spans, _json_spans):
            spans.extend((a + s, a + e) for s, e in finder(seg))
    signal = bool(spans)
    for a, b in segs:
        spans.extend((a + s, a + e) for s, e in _call_line_spans(text[a:b], signal))
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and (s < merged[-1][1] or not text[merged[-1][1]:s].strip()):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged], fenced_seen


def _neutralise(text: str) -> Neutralised:
    tail = ""
    if len(text) > SCAN_CAP:
        cut = text.rfind("\n", 0, SCAN_CAP)
        cut = SCAN_CAP if cut < 0 else cut + 1
        text, tail = text[:cut], text[cut:]
    spans, fenced_seen = _find_spans(text)
    if not spans:
        return Neutralised(text + tail, 0, fenced_seen)
    marker = MARKER_TEMPLATE.format(n=len(spans))
    head = _TRAIL_BLANKS_RE.sub("", text[: spans[0][0]])
    out = f"{head}\n\n{marker}" if head.strip() else marker
    for i, (_, end) in enumerate(spans):
        nxt = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        kept = _trim_blank_lines(text[end:nxt])
        if kept.strip():
            out += "\n\n" + kept
    if tail:
        out += "\n" + tail
    return Neutralised(out, len(spans), fenced_seen)


def neutralise(text: str) -> Neutralised:
    """Return ``Neutralised(text, removed, fenced_seen)``; text is unchanged when removed is 0.
    Fails open on ANY exception (RecursionError included)."""
    if not text:
        return Neutralised(text, 0, 0)
    try:
        return _neutralise(text)
    except Exception as exc:  # noqa: BLE001 -- a bug here must never lose a paid answer
        from llm_router import failopen

        failopen.record("CHZ-FO-F4-NEUTRALISE", exc)
        return Neutralised(text, 0, 0)


def _ledger_path():
    from llm_router import paths

    return paths.state_path(LEDGER_NAME)


def append_ledger(door: str, removed: int, fenced_seen: int) -> None:
    """Append-only count record (count, fenced_seen, door, ts; never any text)."""
    try:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            from llm_router import paths

            if not paths.is_isolated():
                return  # a test must not write the real state directory
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": round(time.time(), 3), "door": door, "removed": removed,
             "fenced_seen": fenced_seen},
            separators=(",", ":"),
        ) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 -- telemetry never changes the answer
        from llm_router import failopen

        failopen.record("CHZ-FO-F4-LEDGER", exc)


def read_ledger() -> list[dict]:
    """Every count record written so far (the reader for ``append_ledger``)."""
    try:
        with open(_ledger_path(), encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []


def neutralise_response(resp: Any, door: str = "") -> Any:
    """Return ``resp`` with ``content`` neutralised and the counts stored in
    ``toolcall_blocks_removed`` / ``toolcall_fenced_seen``, and one ledger record appended;
    the same object when there was nothing to remove or count."""
    res = neutralise(getattr(resp, "content", "") or "")
    if not (res.removed or res.fenced_seen):
        return resp
    append_ledger(door, res.removed, res.fenced_seen)
    return dataclasses.replace(
        resp, content=res.text, toolcall_blocks_removed=res.removed,
        toolcall_fenced_seen=res.fenced_seen,
    )


def neutralise_text(text: str, door: str) -> str:
    """Neutralise a bare answer string (doors that have no LLMResponse) and ledger the counts."""
    res = neutralise(text or "")
    if res.removed or res.fenced_seen:
        append_ledger(door, res.removed, res.fenced_seen)
    return res.text


def keep_marker(before: str, after: str) -> str:
    """A later text pass (the response router rewrites prose paragraphs) must not drop the
    marker: put it back at the top if ``before`` had it and ``after`` lost it."""
    m = MARKER_LINE_RE.search(before)
    if m and not MARKER_LINE_RE.search(after):
        return f"{m.group(0)}\n\n{after}"
    return after
