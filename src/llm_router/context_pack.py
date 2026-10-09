"""One context pack for every door that sends a conversation to a model.

PLAN v16 P1.1 (R-CTX-1, R-CTX-2, R-CTX-4). Before this module each door built
its own context: the hook kept 6 text turns and dropped every tool block, the
MCP door passed a caller string, the proxy kept the first and the last two
turns, and Codex, the gateway and the SDK flattened messages to text. Local
quality was therefore measured on partial context of unknown size (PLAN C4).

`build_pack` is the single builder. A door gathers what it has (a message list,
a transcript path, a session id) and gets back a frozen `ContextPack`:

* ``request``      the current request, verbatim. Never trimmed, never elided.
* ``recent``       the last 7 messages, verbatim, tool_use / tool_result blocks
                   rendered as text. A tool block longer than 4,000 chars keeps
                   its first and last 1,500 chars around a ``[…truncated N
                   chars…]`` marker. In ``mode="full"`` this is the whole
                   conversation, unelided.
* ``summary``      the rolling summary from P1.2. P1.2 is not built yet, so this
                   is always ``None`` today.
* ``project``      repository knowledge and state: OKF, the semantic pack and
                   ``repo_facts`` (branch, ``git status --porcelain`` count),
                   produced by ``context_injection.inject`` — the one choke point
                   allowed to attach repository context. Empty without
                   ``project_root`` (no cwd guessing: OKF-SCOPE-05).
* ``instructions`` a digest of CLAUDE.md / AGENTS.md (headings and rule lines,
                   at most 1,500 tokens), cached by file content hash.
* ``mode``         ``"full"`` when the whole conversation + request +
                   instructions fit in 0.8 x ``target_window`` and the whole
                   conversation was read; otherwise ``"pack"``.
* ``tokens``       ``token_budget.estimate_tokens`` summed over every section.
* ``stable_hash``  sha256 over the sections; identical inputs, identical hash.

Fail open: an error while gathering the conversation, the project or the
instructions yields an empty section, never an exception to the caller. The
request is always present.

Only the last 2 MB of a transcript file is read (``TAIL_BYTES``). When the file
is longer than that the conversation is known to be incomplete, so the pack can
never claim ``mode="full"``.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from llm_router.token_budget import estimate_tokens

#: The seven context doors of PLAN v16 P1.1 task 3. The door PRs wire each one;
#: `tests/test_context_pack_doors.py` (door PRs) enumerates exactly these.
DOORS = ("hook", "mcp", "proxy", "codex", "agent", "sdk", "gateway")

RECENT_N = 7
TOOL_CAP_CHARS = 4_000
TOOL_KEEP_CHARS = 1_500
TAIL_BYTES = 2 * 1024 * 1024
FULL_FRACTION = 0.8  # applied as whole * 5 <= window * 4 in build_pack
INSTRUCTIONS_MAX_TOKENS = 1_500

_INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")
_SKIP_ROLES = frozenset({"system", "developer"})
# Host-injected Codex user items: environment and instructions, not conversation.
_CODEX_INJECTED = ("<environment_context>", "<user_instructions>")

# Indirection so the tail-read mechanism test can count bytes actually read.
_open = open


@dataclass(frozen=True)
class ContextPack:
    request: str
    recent: list[dict[str, str]] = field(default_factory=list)
    summary: str | None = None
    project: str = ""
    instructions: str = ""
    mode: str = "pack"
    tokens: int = 0
    stable_hash: str = ""


# ---------------------------------------------------------------------------
# Rendering message content (shared by every message format)
# ---------------------------------------------------------------------------

def elide(text: str) -> str:
    """Head 1,500 + marker + tail 1,500 for text longer than 4,000 chars."""
    if len(text) <= TOOL_CAP_CHARS:
        return text
    cut = len(text) - 2 * TOOL_KEEP_CHARS
    return f"{text[:TOOL_KEEP_CHARS]}[…truncated {cut} chars…]{text[-TOOL_KEEP_CHARS:]}"


def _as_text(value: Any) -> str:
    """Text of a tool payload: a string, a list of content parts, or JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out = []
        for part in value:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind in ("text", "input_text", "output_text"):
                    out.append(str(part.get("text", "")))
                elif kind in ("image", "image_url", "input_image"):
                    out.append("[image]")
                elif kind in ("document", "input_file"):
                    out.append("[document]")
        return "\n".join(p for p in out if p)
    if isinstance(value, dict) and "content" in value:
        return _as_text(value["content"])
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _render_block(block: Any, *, keep_tools: bool, cap: bool) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind in ("text", "input_text", "output_text"):
        return str(block.get("text", ""))
    if not keep_tools:
        return ""
    if kind == "tool_use":
        raw = block.get("input")
        body = raw if isinstance(raw, str) else json.dumps(
            raw, ensure_ascii=False, sort_keys=True)
        return f"[tool_use: {block.get('name', '')}] {elide(body) if cap else body}"
    if kind == "tool_result":
        body = _as_text(block.get("content"))
        tag = "[tool_result (error)]" if block.get("is_error") else "[tool_result]"
        return f"{tag} {elide(body) if cap else body}"
    if kind in ("image", "image_url", "input_image"):
        return "[image]"
    if kind in ("document", "input_file"):
        return "[document]"
    return ""  # thinking, redacted_thinking, unknown: not conversation text


def extract_turn_text(content: Any, *, keep_tools: bool = True, cap: bool = False) -> str:
    """Text of one transcript message ``content`` (a string or a block list).

    The generalized form of ``hooks/auto-route.py::_extract_turn_text``: with
    ``keep_tools=False`` it returns exactly what that function returns (text
    blocks only); with ``keep_tools=True`` tool_use and tool_result blocks are
    rendered as text, and ``cap=True`` elides any tool block over 4,000 chars.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_render_block(b, keep_tools=keep_tools, cap=cap) for b in content]
        return "\n".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------------------
# Message formats -> canonical (role, blocks, group id)
# ---------------------------------------------------------------------------

@dataclass
class _Msg:
    role: str
    blocks: list[Any]
    gid: str | None = None

    def render(self, cap: bool) -> str:
        return extract_turn_text(self.blocks, keep_tools=True, cap=cap).strip()


def _blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    return []


def _from_chat(m: dict) -> _Msg | None:
    """Anthropic Messages, OpenAI chat, or an already-rendered {role, content}."""
    role = m.get("role")
    if not isinstance(role, str) or role in _SKIP_ROLES:
        return None
    if role == "tool":  # OpenAI tool result
        return _Msg("user", [{"type": "tool_result", "content": m.get("content")}])
    blocks = _blocks(m.get("content"))
    for call in m.get("tool_calls") or []:  # OpenAI assistant tool calls
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            blocks.append({"type": "tool_use", "name": fn.get("name", ""),
                           "input": fn.get("arguments", "")})
    return _Msg(role, blocks)


def _from_item(item: dict) -> _Msg | None:
    """A Responses-API input item or a Codex rollout ``response_item`` payload."""
    kind = item.get("type")
    if kind == "message" or (kind is None and "role" in item):
        role = item.get("role")
        if role not in ("user", "assistant"):
            return None
        blocks = _blocks(item.get("content"))
        first = extract_turn_text(blocks, keep_tools=False).lstrip()
        if role == "user" and first.startswith(_CODEX_INJECTED):
            return None
        return _Msg(role, blocks)
    if kind in ("function_call", "custom_tool_call"):
        return _Msg("assistant", [{"type": "tool_use", "name": item.get("name", ""),
                                   "input": item.get("arguments", item.get("input", ""))}])
    if kind == "local_shell_call":
        return _Msg("assistant", [{"type": "tool_use", "name": "local_shell",
                                   "input": item.get("action", {})}])
    if kind in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
        return _Msg("user", [{"type": "tool_result", "content": item.get("output", "")}])
    return None


def _from_message(m: Any) -> _Msg | None:
    if not isinstance(m, dict):
        return None
    if m.get("type") in (None, "message") and "role" in m:
        return _from_chat(m)
    return _from_item(m)


def _from_cc_line(entry: Any) -> _Msg | None:
    """One Claude Code transcript JSONL entry."""
    if not isinstance(entry, dict) or entry.get("isMeta"):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
        return None
    gid = msg.get("id") if isinstance(msg.get("id"), str) else None
    return _Msg(msg["role"], _blocks(msg.get("content")), gid)


def _from_rollout_line(entry: Any) -> _Msg | None:
    """One Codex rollout JSONL line: wrapped ``response_item`` or a bare item."""
    if not isinstance(entry, dict):
        return None
    if entry.get("type") == "response_item":
        payload = entry.get("payload")
        return _from_item(payload) if isinstance(payload, dict) else None
    if entry.get("type") in ("session_meta", "event_msg", "turn_context", "compacted"):
        return None
    return _from_item(entry)


def _only_tool_results(m: _Msg) -> bool:
    return bool(m.blocks) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in m.blocks)


def _joins(a: _Msg, b: _Msg) -> bool:
    """True when adjacent entries ``a`` and ``b`` are parts of one message.

    Claude Code writes each content block of one assistant response as its own
    line with the same ``message.id``, and each result of parallel tool calls
    as its own user line; the API sees one message in both cases. One message
    is one turn, not one block.
    """
    if a.role != b.role:
        return False
    if a.gid and a.gid == b.gid:
        return True
    return a.role == "user" and _only_tool_results(a) and _only_tool_results(b)


def _merge(msgs: Iterable[_Msg | None]) -> list[_Msg]:
    """Drop empties; join consecutive entries of one message (``_joins``)."""
    out: list[_Msg] = []
    for m in msgs:
        if m is None or not m.render(cap=False):
            continue
        if out and _joins(out[-1], m):
            out[-1].blocks.extend(m.blocks)
        else:
            out.append(_Msg(m.role, list(m.blocks), m.gid))
    return out


# ---------------------------------------------------------------------------
# Reading transcripts (last 2 MB only)
# ---------------------------------------------------------------------------

def _read_tail(path: str | os.PathLike, limit: int = TAIL_BYTES) -> tuple[bytes, bool]:
    """The last ``limit`` bytes of ``path`` and whether that is the whole file.

    A partial first line is dropped: it is the tail of a record that started
    before the window.
    """
    with _open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - limit)
        fh.seek(start)
        data = fh.read(limit)
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data, start == 0


def _parse_lines(data: bytes, parse, *, complete: bool) -> list[_Msg]:
    lines = data.split(b"\n")

    def entries(seq):
        for raw in seq:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield parse(json.loads(raw))
            except (ValueError, TypeError, AttributeError):
                continue

    if complete:
        return _merge(entries(lines))
    # Incomplete conversation: mode can only be "pack", so only the newest
    # RECENT_N (+1 for a trailing duplicate of the request) messages matter.
    # Walk backwards and stop once one more message than needed is collected.
    # The oldest collected message is always dropped: it may be a partial group,
    # cut either by this stop or by the start of the 2 MB window.
    want = RECENT_N + 2
    rev: list[_Msg] = []
    for m in entries(reversed(lines)):
        if m is None or not m.render(cap=False):
            continue
        if rev and _joins(m, rev[-1]):
            rev[-1].blocks[:0] = m.blocks
            continue
        if len(rev) >= want:
            break
        rev.append(_Msg(m.role, list(m.blocks), m.gid))
    rev.reverse()
    return rev[1:]


def _transcript(path: str | os.PathLike, *, codex: bool) -> tuple[list[_Msg], bool]:
    data, complete = _read_tail(path)
    parse = _from_rollout_line if codex else _from_cc_line
    return _parse_lines(data, parse, complete=complete), complete


def _resolve_session_transcript(session_id: str | None,
                                projects_dir: Path | None = None) -> Path | None:
    """``<projects_dir>/*/<session_id>.jsonl``, newest first; None when absent."""
    from llm_router.call_identity import clean_id

    sid = clean_id(session_id)
    if not sid or sid.startswith("."):
        return None
    if projects_dir is None:
        from llm_router.northstar import claude_projects_dir
        projects_dir = claude_projects_dir()
    try:
        hits = [p for p in Path(projects_dir).glob(f"*/{sid}.jsonl") if p.is_file()]
    except OSError:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _from_list(messages: list | None) -> tuple[list[_Msg], bool]:
    """A caller-supplied message list is the whole conversation; no list at
    all means the door saw none, which cannot qualify the pack for "full"."""
    if messages is None:
        return [], False
    return _merge(_from_message(m) for m in messages), True


def _gather(door: str, *, messages: list | None = None, transcript_path: str | None = None,
            session_id: str | None = None, context: str | None = None,
            projects_dir: Path | None = None) -> tuple[list[_Msg], bool]:
    """(messages, complete) for ``door``. Fail-open: ([], False) on any error."""
    try:
        if door in ("proxy", "sdk", "gateway"):
            return _from_list(messages)
        if door == "codex":
            if transcript_path:
                return _transcript(transcript_path, codex=True)
            return _from_list(messages)
        if door == "mcp":
            if not transcript_path:
                if session_id is None:
                    from llm_router.call_identity import call_session_id
                    session_id = call_session_id()
                found = _resolve_session_transcript(session_id, projects_dir)
                transcript_path = str(found) if found else None
            if transcript_path:
                return _transcript(transcript_path, codex=False)
            if messages is not None:
                return _from_list(messages)
            # The caller's context string is what the caller chose to share,
            # not the conversation, so it never qualifies the pack for "full".
            return _merge([_Msg("user", _blocks(context))] if context else []), False
        # hook, agent, and any other door: the Claude Code transcript first.
        if not transcript_path and session_id:
            found = _resolve_session_transcript(session_id, projects_dir)
            transcript_path = str(found) if found else None
        if transcript_path:
            return _transcript(transcript_path, codex=False)
        return _from_list(messages)
    except Exception:                                        # noqa: BLE001
        return [], False


def messages_for(door: str, *, messages: list | None = None,
                 transcript_path: str | None = None, session_id: str | None = None,
                 context: str | None = None, projects_dir: Path | None = None,
                 cap: bool = False) -> list[dict[str, str]]:
    """The conversation a door can see, as ``[{role, content}]`` with tool blocks.

    hook / agent: the Claude Code transcript (``transcript_path``, else resolved
    from ``session_id``). proxy / sdk / gateway: the ``messages`` argument
    (Anthropic, OpenAI chat or Responses-API items). mcp: the transcript of the
    calling session (``call_identity``), else ``messages``, else the caller's
    ``context`` string. codex: the rollout file at ``transcript_path``. Never
    raises; returns ``[]`` when nothing is readable.
    """
    msgs, _ = _gather(door, messages=messages, transcript_path=transcript_path,
                      session_id=session_id, context=context, projects_dir=projects_dir)
    return [{"role": m.role, "content": m.render(cap)} for m in msgs]


# ---------------------------------------------------------------------------
# Project and instructions sections (fail-open)
# ---------------------------------------------------------------------------

def _project(request: str, root: str | None, session_id: str | None) -> str:
    """OKF + semantic pack + repo state, via the one context choke point."""
    if not root or not request:
        return ""
    try:
        from llm_router.context_injection import inject

        enriched = inject(request, root=root, retrieval_sid=session_id, session_root=root)
        if enriched == request or not enriched.endswith(request):
            return ""
        return enriched[: len(enriched) - len(request)].strip()
    except Exception:                                        # noqa: BLE001
        return ""


_DIGEST_CACHE: dict[str, str] = {}
_DIGEST_CACHE_MAX = 64  # long-lived processes (proxy, MCP) see many file edits


def _digest_text(text: str) -> str:
    """Headings and rule lines (list items, bold leads); code fences skipped."""
    out: list[str] = []
    fence = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if fence or not s:
            continue
        if s.startswith("#") or s.startswith("**") or s[:2] in ("- ", "* ", "+ "):
            out.append(line.rstrip())
            continue
        head = s.split(" ", 1)[0]
        if len(head) >= 2 and head[:-1].isdigit() and head[-1] in ".)":
            out.append(line.rstrip())
    return "\n".join(out)


def _digest_file(path: Path) -> str:
    data = path.read_bytes()
    key = hashlib.sha256(data).hexdigest()
    hit = _DIGEST_CACHE.get(key)
    if hit is None:
        hit = _digest_text(data.decode("utf-8", errors="replace"))
        if len(_DIGEST_CACHE) >= _DIGEST_CACHE_MAX:
            _DIGEST_CACHE.clear()
        _DIGEST_CACHE[key] = hit
    return hit


def _instruction_paths(root: str | None) -> list[tuple[str, Path]]:
    # Project files only. The user's private ~/.claude/CLAUDE.md is not read:
    # a pack can reach an external model, and no privacy gate covers that file.
    if not root:
        return []
    found = [(rel, Path(root) / rel) for rel in _INSTRUCTION_FILES]
    seen: set[Path] = set()
    out = []
    for label, p in found:
        try:
            if not p.is_file():
                continue
            real = p.resolve()
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            out.append((label, p))
    return out


def _instructions(root: str | None) -> str:
    """The project's CLAUDE.md, .claude/CLAUDE.md and AGENTS.md, in that order,
    capped at INSTRUCTIONS_MAX_TOKENS on whole lines."""
    try:
        lines: list[str] = []
        for label, path in _instruction_paths(root):
            try:
                digest = _digest_file(path)
            except OSError:
                continue
            if digest:
                lines.append(f"[{label}]")
                lines.extend(digest.splitlines())
        out: list[str] = []
        used = 0
        for line in lines:
            cost = len(line) + 1
            if (used + cost) // 4 > INSTRUCTIONS_MAX_TOKENS:
                break
            out.append(line)
            used += cost
        return "\n".join(out)
    except Exception:                                        # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def _hash(request: str, recent: list[dict[str, str]], summary: str | None,
          project: str, instructions: str, mode: str) -> str:
    blob = json.dumps([request, recent, summary, project, instructions, mode],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_pack(request: str, messages: list | None = None, *,
               session_id: str | None = None, transcript_path: str | None = None,
               project_root: str | None = None, target_window: int,
               door: str) -> ContextPack:
    """Build the context pack for one request at one door. Never raises for
    conversation, project or instructions errors; ``request`` is kept verbatim."""
    if isinstance(request, (bytes, bytearray)):
        request = bytes(request).decode("utf-8", errors="replace")
    elif not isinstance(request, str):
        request = "" if request is None else str(request)
    msgs, complete = _gather(door, messages=messages, transcript_path=transcript_path,
                             session_id=session_id)
    # The current request is passed separately; a trailing copy of it in the
    # conversation would send it twice.
    if msgs and msgs[-1].role == "user" and msgs[-1].render(cap=False) == request.strip():
        msgs = msgs[:-1]

    instructions = _instructions(project_root)
    project = _project(request, project_root, session_id)

    try:
        window = int(target_window)
    except (TypeError, ValueError, OverflowError):
        window = 0
    full = [{"role": m.role, "content": m.render(cap=False)} for m in msgs]
    conv_tokens = sum(estimate_tokens(m["content"]) for m in full)
    whole = conv_tokens + estimate_tokens(request) + (
        estimate_tokens(instructions) if instructions else 0)
    # Integer form of whole <= 0.8 * window (no float overflow on huge windows).
    if complete and window > 0 and whole * 5 <= window * 4:
        mode, recent = "full", full
    else:
        mode = "pack"
        recent = [{"role": m.role, "content": m.render(cap=True)} for m in msgs[-RECENT_N:]]

    summary: str | None = None  # P1.2 (rolling summary) is not built yet
    tokens = estimate_tokens(request) + sum(estimate_tokens(m["content"]) for m in recent)
    tokens += sum(estimate_tokens(s) for s in (project, instructions) if s)
    return ContextPack(
        request=request, recent=recent, summary=summary, project=project,
        instructions=instructions, mode=mode, tokens=tokens,
        stable_hash=_hash(request, recent, summary, project, instructions, mode),
    )
