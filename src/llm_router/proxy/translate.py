"""Anthropic Messages <-> Ollama chat translation, reply validation, and SSE.

Request side (``to_ollama``):
  * ``system`` may be a string or a list of text blocks (Claude Code sends a
    list with ``cache_control``); both become one system message.
  * ``role: system`` messages inside ``messages`` stay system messages.
  * assistant ``thinking`` / ``redacted_thinking`` blocks are dropped: their
    signatures mean nothing to another model.
  * ``tool_use`` -> ``tool_calls``; ``tool_result`` -> a ``tool`` message
    carrying the tool name (resolved from the matching ``tool_use`` id).
  * images and documents in older turns become a short placeholder.
  * ``thinking``, ``context_management``, ``output_config`` and ``metadata`` are
    Anthropic-only and are not sent.

Reply side (``from_ollama``): a reply is accepted only if every tool call names a
tool from the request, its arguments are a JSON object, required keys are
present and primitive types/enums match. Anything else returns an error string
and the caller falls back to Anthropic.

Served ids carry the ``SERVED_TOOL_ID_PREFIX`` / ``SERVED_MSG_ID_PREFIX`` markers
so a later request can tell that its history contains a proxy-served turn.
"""

from __future__ import annotations

import json
import re
import uuid

SERVED_TOOL_ID_PREFIX = "toolu_lr"
SERVED_MSG_ID_PREFIX = "msg_lr"

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_MEDIA_PLACEHOLDER = "[{kind} omitted by llm-router proxy]"


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") in ("image", "document"):
            parts.append(_MEDIA_PLACEHOLDER.format(kind=b["type"]))
    return "\n".join(parts)


def system_text(system) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system or []
                       if isinstance(b, dict) and b.get("type") == "text")


def ollama_tools(tools: list) -> list[dict]:
    """Client tools only; Anthropic server tools (web search etc.) have no
    ``input_schema`` and cannot be executed by the client."""
    return [{"type": "function", "function": {
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
        for t in tools or [] if isinstance(t, dict) and t.get("name") and "input_schema" in t]


def to_ollama_messages(body: dict) -> list[dict]:
    sys_text = system_text(body.get("system"))
    out: list[dict] = [{"role": "system", "content": sys_text}] if sys_text else []
    id2name: dict[str, str] = {}
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "system":
            out.append({"role": "system", "content": _blocks_text(content)})
            continue
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content or []
                           if isinstance(b, dict) and b.get("type") == "text")
            calls = []
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    id2name[b.get("id", "")] = b.get("name", "")
                    calls.append({"function": {"name": b.get("name", ""),
                                               "arguments": b.get("input") or {}}})
            msg: dict = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        texts = []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "tool_result":
                result = _blocks_text(b.get("content"))
                if b.get("is_error"):
                    result = "ERROR: " + result
                out.append({"role": "tool", "tool_name": id2name.get(b.get("tool_use_id", ""), ""),
                            "content": result})
            elif kind == "text":
                texts.append(b.get("text", ""))
            elif kind in ("image", "document"):
                texts.append(_MEDIA_PLACEHOLDER.format(kind=kind))
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def to_ollama(body: dict, model: str, *, num_ctx: int, max_predict: int = 4096) -> dict:
    """The ``/api/chat`` payload for ``model`` (bare Ollama tag, no ``ollama/``)."""
    options: dict = {"num_ctx": num_ctx,
                     "num_predict": min(int(body.get("max_tokens") or max_predict), max_predict)}
    if isinstance(body.get("temperature"), (int, float)):
        options["temperature"] = body["temperature"]
    return {"model": model, "messages": to_ollama_messages(body),
            "tools": ollama_tools(body.get("tools") or []),
            "stream": False, "think": False, "options": options}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def schema_error(args, schema: dict) -> str | None:
    """Top-level JSON-schema check: object, required keys, primitive types, enums.

    Deliberately shallow. Nested schemas are not checked; the client (Claude
    Code) validates tool input itself and reports a tool error, which the next
    step sees like any other tool error."""
    if not isinstance(args, dict):
        return "input not an object"
    for key in schema.get("required", []) or []:
        if key not in args:
            return f"missing required '{key}'"
    props = schema.get("properties", {}) or {}
    for key, value in args.items():
        if key not in props:
            if schema.get("additionalProperties") is False:
                return f"unknown property '{key}'"
            continue
        spec = props[key] if isinstance(props[key], dict) else {}
        t = spec.get("type")
        if isinstance(t, str) and t in _TYPES:
            ok = isinstance(value, _TYPES[t])
            if t in ("integer", "number") and isinstance(value, bool):
                ok = False
            if not ok:
                return f"'{key}' should be {t}"
        if isinstance(spec.get("enum"), list) and value not in spec["enum"]:
            return f"'{key}' not in enum"
    return None


def new_tool_id() -> str:
    return SERVED_TOOL_ID_PREFIX + uuid.uuid4().hex[:22]


def new_msg_id() -> str:
    return SERVED_MSG_ID_PREFIX + uuid.uuid4().hex[:22]


def from_ollama(resp: dict, body: dict) -> tuple[dict | None, str | None]:
    """``(anthropic_message, None)`` or ``(None, reason)`` -> caller falls back."""
    msg = resp.get("message") or {}
    text = _THINK_RE.sub("", msg.get("content") or "").strip()
    schemas = {t["name"]: t.get("input_schema") or {} for t in body.get("tools") or []
               if isinstance(t, dict) and t.get("name") and "input_schema" in t}
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        name, args = fn.get("name"), fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return None, f"tool args not JSON for {name}"
        if name not in schemas:
            return None, f"unknown tool '{name}'"
        err = schema_error(args, schemas[name])
        if err:
            return None, f"{name}: {err}"
        blocks.append({"type": "tool_use", "id": new_tool_id(), "name": name, "input": args})
    if not blocks:
        return None, "empty response"
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    elif resp.get("done_reason") == "length":
        stop = "max_tokens"
    else:
        stop = "end_turn"
    return {
        "id": new_msg_id(), "type": "message", "role": "assistant",
        # Echo the requested model: Claude Code renders it and checks nothing
        # else. The ledger records the model that actually served the call.
        "model": body.get("model"),
        "content": blocks, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": int(resp.get("prompt_eval_count") or 0),
                  "output_tokens": int(resp.get("eval_count") or 0),
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }, None


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def sse_from_message(m: dict) -> bytes:
    """Render a complete message as the Anthropic streaming event sequence.

    The served reply is rendered only after it passed validation: a streamed
    tool call cannot be retracted once the client has seen it, so the proxy
    never streams unvalidated tokens."""
    start = dict(m, content=[], stop_reason=None)
    start["usage"] = dict(m["usage"], output_tokens=1)
    parts = [_event("message_start", {"type": "message_start", "message": start})]
    for i, b in enumerate(m["content"]):
        if b["type"] == "text":
            parts.append(_event("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "text", "text": ""}}))
            parts.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "text_delta", "text": b["text"]}}))
        else:
            parts.append(_event("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            parts.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}))
        parts.append(_event("content_block_stop", {"type": "content_block_stop", "index": i}))
    parts.append(_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": m["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": m["usage"]["output_tokens"]}}))
    parts.append(_event("message_stop", {"type": "message_stop"}))
    return "".join(parts).encode()


def has_served_turn(body: dict) -> bool:
    """True when any assistant turn in the history was served by this proxy."""
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and str(b.get("id", "")).startswith(SERVED_TOOL_ID_PREFIX)):
                return True
    return False


def without_thinking(body: dict) -> dict:
    """A copy with extended thinking off, for the one retry after Anthropic
    rejects a mixed history over thinking blocks.

    ``context_management``'s ``clear_thinking_*`` edits require thinking to be
    enabled, so they are removed together with ``thinking``; other edits stay.
    Thinking blocks already in the history are left as they are (the API
    accepts prior thinking blocks when thinking is off)."""
    out = {k: v for k, v in body.items() if k != "thinking"}
    cm = out.get("context_management")
    if isinstance(cm, dict):
        edits = [e for e in cm.get("edits") or []
                 if not (isinstance(e, dict) and str(e.get("type", "")).startswith("clear_thinking"))]
        if edits:
            out["context_management"] = dict(cm, edits=edits)
        else:
            out.pop("context_management", None)
    return out


def is_thinking_rejection(status: int, body_text: str) -> bool:
    """An Anthropic 400 that names thinking blocks (mixed-history rejection)."""
    return status == 400 and "thinking" in body_text.lower()


def parse_sse_usage(buf: bytes) -> tuple[dict, str | None, str | None]:
    """``(usage, stop_reason, message_id)`` from a complete SSE body."""
    usage: dict = {}
    stop = None
    msg_id = None
    for line in buf.decode("utf-8", "replace").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            d = json.loads(line[6:])
        except ValueError:
            continue
        if d.get("type") == "message_start":
            message = d.get("message") or {}
            msg_id = message.get("id")
            usage.update(message.get("usage") or {})
        elif d.get("type") == "message_delta":
            usage.update({k: v for k, v in (d.get("usage") or {}).items() if v is not None})
            stop = (d.get("delta") or {}).get("stop_reason")
    return usage, stop, msg_id
