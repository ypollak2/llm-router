"""Step classes: which ``/v1/messages`` calls the proxy may try to serve.

A step class is a predicate over the request body. Only classes named in the
proxy's enabled set are ever considered; every other call passes through.

``continuation``
    A main-loop call whose newest non-system user turn holds only
    ``tool_result`` blocks (plus reminder text Claude Code appends next to
    them). The question it asks is "given this tool output, what next?". The
    first call of a task is never a continuation. Measured in the spike
    (2026-09-28, n=22 calls over 6 golden tasks): 16 of 22 steps had this
    shape.

Calls that carry a forced ``tool_choice``, images or documents in the newest
turn, or no client tools are never eligible: the serving model could not honour
them faithfully, and a pass-through costs nothing.

Step KINDS (GE1 action census). The ledger's ``step_class`` field records what
kind of call every request was, whether or not it may be served
(:func:`step_kind`): ``continuation`` (the shape above), ``turn_first`` (the
first call of a human turn: the newest user turn has text), ``subagent_first``
(a sub-agent's first call) and ``side_call`` (no client tools: titles, probes,
summaries). Only ``STEP_CLASSES`` may be served; the other kinds are labels.
A continuation that cannot be served faithfully says why in
:func:`step_ineligible`.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from llm_router.proxy import tool_classes

STEP_CONTINUATION = "continuation"
STEP_TURN_FIRST = "turn_first"
STEP_SIDE_CALL = "side_call"
STEP_SUBAGENT_FIRST = "subagent_first"
STEP_KINDS = (STEP_CONTINUATION, STEP_TURN_FIRST, STEP_SIDE_CALL, STEP_SUBAGENT_FIRST)

#: Claude Code gives the main thread a sub-agent launcher and gives sub-agents none
#: (a sub-agent cannot spawn another). ``Task`` is the older name of ``Agent``.
_AGENT_LAUNCHERS = frozenset({"Agent", "Task"})


def non_system(messages: list) -> list:
    """Claude Code interleaves ``role: system`` reminders into ``messages``,
    including AFTER the newest tool_result. They are not turns."""
    return [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]


def _newest_turn_kinds(body: dict) -> set | None:
    """Block types of the newest user turn, or ``None`` when it cannot answer tools."""
    msgs = non_system(body.get("messages") or [])
    if len(msgs) < 3 or msgs[-1].get("role") != "user":
        return None
    content = msgs[-1].get("content")
    if not isinstance(content, list) or not content:
        return None
    return {b.get("type") for b in content if isinstance(b, dict)}


def _is_continuation(body: dict) -> bool:
    kinds = _newest_turn_kinds(body)
    return kinds is not None and "tool_result" in kinds and kinds <= {"tool_result", "text"}


def _is_continuation_kind(body: dict) -> bool:
    """The continuation KIND for the ledger: as :func:`_is_continuation`, but images or
    documents beside the tool results still make a continuation (one that
    :func:`step_ineligible` marks ``media``). Serving keeps the strict predicate."""
    kinds = _newest_turn_kinds(body)
    return (kinds is not None and "tool_result" in kinds
            and kinds <= {"tool_result", "text", "image", "document"})


STEP_CLASSES: dict[str, Callable[[dict], bool]] = {
    STEP_CONTINUATION: _is_continuation,
}


def _newest_turn_has_media(body: dict) -> bool:
    msgs = non_system(body.get("messages") or [])
    if not msgs or not isinstance(msgs[-1].get("content"), list):
        return False
    for block in msgs[-1]["content"]:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("image", "document"):
            return True
        inner = block.get("content")
        if block.get("type") == "tool_result" and isinstance(inner, list):
            if any(isinstance(x, dict) and x.get("type") in ("image", "document") for x in inner):
                return True
    return False


def _has_client_tools(body: dict) -> bool:
    return any(isinstance(t, dict) and t.get("name") and "input_schema" in t
               for t in body.get("tools") or [])


def step_class(body: dict, enabled: frozenset[str] | set[str]) -> str | None:
    """The first enabled step class this request belongs to, else ``None``."""
    if not _has_client_tools(body):
        return None  # side calls (titles, probes) carry no tools
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
        return None
    if _newest_turn_has_media(body):
        return None
    for name, pred in STEP_CLASSES.items():
        if name in enabled and pred(body):
            return name
    return None


def step_kind(body: dict) -> str:
    """What kind of call ``body`` is (one of :data:`STEP_KINDS`), for the ledger.

    ``subagent_first`` is inferred from the tool list: a first call whose tools hold
    no ``Agent``/``Task`` launcher. A main session started with that tool disabled is
    therefore counted as a sub-agent; both are actions, so the census's action total
    does not move."""
    if not _has_client_tools(body):
        return STEP_SIDE_CALL
    if _is_continuation_kind(body):
        return STEP_CONTINUATION
    if is_first_call(body):
        names = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
        return STEP_TURN_FIRST if names & _AGENT_LAUNCHERS else STEP_SUBAGENT_FIRST
    return STEP_TURN_FIRST


def step_ineligible(body: dict) -> str | None:
    """A LABEL, not a gate: what in a continuation the serving model could not
    honour faithfully. ``media`` (images or documents in the newest turn) and
    ``forced_tool_choice`` also stop serving (:func:`step_class`). ``server_tool``
    (a tool with no ``input_schema``, run by Anthropic, not the client) does not:
    :func:`step_class` still serves the call when a client tool exists and the
    translator drops the server tools, so a row can hold ``decision=served``
    beside ``step_ineligible=server_tool``. ``None`` for a continuation with none
    of these and for every other kind of call."""
    if step_kind(body) != STEP_CONTINUATION:
        return None
    if _newest_turn_has_media(body):
        return "media"
    if any(isinstance(t, dict) and "input_schema" not in t for t in body.get("tools") or []):
        return "server_tool"
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
        return "forced_tool_choice"
    return None


def _prev_tool_uses(body: dict) -> list[dict]:
    msgs = non_system(body.get("messages") or [])
    if len(msgs) < 2 or not isinstance(msgs[-2].get("content"), list):
        return []
    return [b for b in msgs[-2]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]


def prev_tool_class(body: dict) -> str | None:
    """The ``tool_classes`` class of the tool calls the newest tool results answer,
    or ``None`` when the call is not a continuation (a side call included). A Bash
    command is read here, in memory, and never returned or stored."""
    if step_kind(body) != STEP_CONTINUATION:
        return None
    return tool_classes.step_tool_class((b.get("name"), b.get("input")) for b in _prev_tool_uses(body))


def prev_tools(body: dict) -> list[str]:
    """Names of the tool calls the newest tool results answer (shape only)."""
    return [b.get("name", "") for b in _prev_tool_uses(body)]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def classify_text(body: dict, limit: int = 1500) -> str:
    """What the router's classifier sees: the original ask plus the newest tool
    output, not the ~30 KB preamble every call re-sends."""
    msgs = non_system(body.get("messages") or [])
    first = _text_of(msgs[0].get("content")) if msgs else ""
    outputs = []
    last = msgs[-1].get("content") if msgs else None
    for block in last if isinstance(last, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            outputs.append(inner if isinstance(inner, str) else _text_of(inner))
    return (first[-limit:] + "\n" + "\n".join(outputs)[:limit]).strip()


_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_MODEL_COMMAND = "<command-name>/model</command-name>"


def _human_text(content) -> str:
    """A user turn's own text: tool results and Claude Code's
    ``<system-reminder>`` blocks removed."""
    return _REMINDER_RE.sub("", _text_of(content)).strip()


def newest_human_text(body: dict) -> str:
    """The conversation's newest human prompt, untruncated: the newest user
    message that has text once tool results and ``<system-reminder>`` blocks are
    removed. ``tier_text`` classifies the tail of this; ``prompt_key.key`` hashes
    all of it, so the ledger's ``text_sha`` matches a hash of the typed prompt."""
    for m in reversed(non_system(body.get("messages") or [])):
        if m.get("role") != "user":
            continue
        text = _human_text(m.get("content"))
        if text:
            return text
    return ""


def tier_text(body: dict, limit: int = 3000) -> str:
    """What the tier decision classifies: the conversation's newest human
    prompt. Tool output is left out on purpose, so the class (and with it the
    tier) holds steady through a tool loop instead of moving with each
    result's length, which would re-write the prompt cache mid-task."""
    return newest_human_text(body)[-limit:]


def user_pinned_model(body: dict) -> bool:
    """True when the conversation shows the user ran ``/model``: Claude Code
    records a local command in the transcript it sends, so the model in the
    request is the user's explicit choice and is never downgraded."""
    for m in non_system(body.get("messages") or []):
        if m.get("role") == "user" and _MODEL_COMMAND in _text_of(m.get("content")):
            return True
    return False


def is_first_call(body: dict) -> bool:
    """The conversation's first model call: one user turn, no reply yet."""
    return len(non_system(body.get("messages") or [])) == 1


def has_client_tools(body: dict) -> bool:
    return _has_client_tools(body)


def session_id_of(body: dict) -> str | None:
    """Claude Code's session id, from ``metadata.user_id`` (a JSON string).

    Only ``session_id`` is kept; ``device_id`` and ``account_uuid`` in the same
    field are never read into a ledger row."""
    raw = (body.get("metadata") or {}).get("user_id")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    sid = parsed.get("session_id") if isinstance(parsed, dict) else None
    return sid if isinstance(sid, str) else None
