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


def to_ollama(body: dict, model: str, *, num_ctx: int, max_predict: int = 4096,
              keep_alive: str | int | None = None, stream: bool = False) -> dict:
    """The ``/api/chat`` payload for ``model`` (bare Ollama tag, no ``ollama/``)."""
    options: dict = {"num_ctx": num_ctx,
                     "num_predict": min(int(body.get("max_tokens") or max_predict), max_predict)}
    if isinstance(body.get("temperature"), (int, float)):
        options["temperature"] = body["temperature"]
    payload = {"model": model, "messages": to_ollama_messages(body),
               "tools": ollama_tools(body.get("tools") or []),
               "stream": stream, "think": False, "options": options}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    return payload


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


# A trimmed request may carry the ORIGINAL client tool list under this key
# (``local_agent.compact`` does): the model is shown a subset, but a call to any
# tool the client actually offered is valid for the client and is accepted.
# ``to_ollama`` never sends this key.
VALIDATE_TOOLS_KEY = "x_llm_router_validate_tools"


def from_ollama(resp: dict, body: dict) -> tuple[dict | None, str | None]:
    """``(anthropic_message, None)`` or ``(None, reason)`` -> caller falls back."""
    msg = resp.get("message") or {}
    text = _THINK_RE.sub("", msg.get("content") or "").strip()
    schemas = {t["name"]: t.get("input_schema") or {}
               for t in body.get(VALIDATE_TOOLS_KEY) or body.get("tools") or []
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
    if resp.get("done_reason") == "length":
        # Output hit the num_predict cap: a cut-off tool call or answer is
        # never served, however plausible the part that arrived looks.
        return None, "truncated at num_predict"
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
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


# Claude Haiku 4.5 (model id ``claude-haiku-4-5-20251001``), per
# platform.claude.com/docs/en/build-with-claude/extended-thinking and
# platform.claude.com/docs/en/about-claude/models/overview (fetched
# 2026-10-02): max output 64,000 tokens (vs 128K on Sonnet/Opus/Fable).
HAIKU_MAX_OUTPUT_TOKENS = 64_000

# Claude Haiku 5.5 (model id ``claude-haiku-5-5``), fetched 2026-10-10 from
# platform.claude.com/docs/en/models/haiku-5-5/overview ("Context window: 1M
# tokens · Max output: 128K tokens") and .../migration-guide.
HAIKU55_MODEL = "claude-haiku-5-5"
HAIKU55_MAX_OUTPUT_TOKENS = 128_000


def haiku_is_legacy(model: str | None) -> bool:
    """True for every Haiku id but 5.5: the 4.5 rules (``for_haiku`` strips thinking and
    effort, folds the system role, clamps to 64K) stay the default for any other id, so
    a policy that still configures ``claude-haiku-4-5`` keeps working."""
    return (model or "").strip().lower() != HAIKU55_MODEL


def haiku_max_output_tokens(model: str | None) -> int:
    return HAIKU_MAX_OUTPUT_TOKENS if haiku_is_legacy(model) else HAIKU55_MAX_OUTPUT_TOKENS


def _fold_blocks(content) -> list[dict]:
    """The content of a ``role: "system"`` message as user-message blocks: each text
    block wrapped in ``<system-reminder>`` tags unless it already is (Claude Code wraps
    its own), other blocks kept as they are."""
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
        [dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in content or []]
    out = []
    for b in blocks:
        text = b.get("text")
        if b.get("type") == "text" and isinstance(text, str) and not text.lstrip().startswith("<system-reminder>"):
            b = dict(b, text=f"<system-reminder>\n{text}\n</system-reminder>")
        out.append(b)
    return out


def _blocks_of(message: dict) -> list:
    content = message.get("content")
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def fold_system_messages(messages: list) -> list:
    """``messages`` with every mid-conversation ``role: "system"`` message moved into an
    ordinary user message, as ``<system-reminder>`` text blocks (M0.7). Haiku 4.5
    rejects the role itself ("role 'system' is not supported on this model").

    Placement, per message (consecutive system messages travel together, in order):

    - the next message is a user message: the text goes at its start, or, when it holds
      ``tool_result`` blocks, right after the last of them (they must lead);
    - otherwise the previous message is a user message: the text is appended to it;
    - otherwise (assistant on both sides) it becomes a user message of its own.

    The message-level ``effort`` control (``output_config``) is dropped with the message:
    Haiku has no effort setting. ``messages`` itself and every message that is not
    changed are left as they are (the caller keeps the original for its 4xx retry)."""
    if not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
        return messages
    out: list = []
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if not (isinstance(m, dict) and m.get("role") == "system"):
            out.append(m)
            i += 1
            continue
        folded: list[dict] = []
        while i < n and isinstance(messages[i], dict) and messages[i].get("role") == "system":
            folded += _fold_blocks(messages[i].get("content"))
            i += 1
        nxt = messages[i] if i < n else None
        prev = out[-1] if out else None
        if isinstance(nxt, dict) and nxt.get("role") == "user":
            blocks = _blocks_of(nxt)
            at = max((k for k, b in enumerate(blocks) if isinstance(b, dict) and b.get("type") == "tool_result"),
                     default=-1) + 1
            out.append(dict(nxt, content=blocks[:at] + folded + blocks[at:]))
            i += 1
        elif isinstance(prev, dict) and prev.get("role") == "user":
            out[-1] = dict(prev, content=_blocks_of(prev) + folded)
        else:
            out.append({"role": "user", "content": folded})
    return out


def for_haiku(body: dict, *, fold_system: bool = False, model: str | None = None) -> dict:
    """A copy of ``body`` with the fields Haiku 4.5 rejects outright removed
    or clamped, for the opt-in Haiku tier (``proxy/tiers.py``,
    ``ClaudeTierPolicy``'s ``haiku_rewrite``).

    Per platform.claude.com/docs/en/build-with-claude/extended-thinking
    (fetched 2026-10-02): "If your model supports only extended thinking
    (Claude Sonnet 4.5, Claude Opus 4.5, **Claude Haiku 4.5**, and earlier
    Claude 4 models) ... `type: "adaptive"` returns a 400 error" -- Claude
    Code's main-loop calls send exactly that type, so ``thinking`` (and any
    ``context_management`` ``clear_thinking_*`` edit, which requires thinking
    to be enabled) is dropped entirely rather than rewritten to the manual
    ``enabled``/``budget_tokens`` form: that form forces Claude to think on
    every call, a behavior change this narrow rewrite does not make.

    Per platform.claude.com/docs/en/about-claude/models/overview (fetched
    2026-10-02), the model comparison table lists Claude Haiku 4.5's "Default
    effort" as "Not supported" -- the only model in the current lineup where
    that row is not a level name -- so ``output_config`` (which carries only
    ``effort`` on a Claude Code request) is dropped too.

    ``max_tokens`` above Haiku's 64K output ceiling (same table, "Max
    output") is clamped down rather than treated as ineligible: Claude Code
    requests a fixed budget it does not need in full on an easy turn, so
    clamping is safe and keeps more turns eligible.

    ``fold_system`` (policy ``haiku_fold_system``, M0.7): also move every
    mid-conversation ``role: "system"`` message into a user message
    (``fold_system_messages``). The top-level ``system`` field is not touched, so
    the cache prefix is kept.

    ``model`` is the Haiku the tier serves. For ``claude-haiku-5-5`` (anything else is
    the 4.5 rule above) the rewrite is much narrower, per the migration guide
    (platform.claude.com/docs/en/models/haiku-5-5/migration-guide, fetched 2026-10-10):
    adaptive thinking, ``output_config.effort`` (all five levels) and mid-conversation
    ``role: "system"`` messages are accepted, so they stay (no fold: it would also drop
    the per-turn effort); only ``thinking.type: "enabled"`` with ``budget_tokens`` is a
    400 there, so a body that carries it loses ``thinking`` (adaptive is on by default),
    and ``max_tokens`` is clamped to 128K.
    """
    if not haiku_is_legacy(model):
        thinking = body.get("thinking")
        out = without_thinking(body) if isinstance(thinking, dict) and thinking.get("type") == "enabled" else dict(body)
        cap = HAIKU55_MAX_OUTPUT_TOKENS
        if isinstance(out.get("max_tokens"), int) and out["max_tokens"] > cap:
            out["max_tokens"] = cap
        return out
    out = without_thinking(body)
    out.pop("output_config", None)
    max_tokens = out.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > HAIKU_MAX_OUTPUT_TOKENS:
        out["max_tokens"] = HAIKU_MAX_OUTPUT_TOKENS
    if fold_system and isinstance(out.get("messages"), list):
        out["messages"] = fold_system_messages(out["messages"])
    return out


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


def sse_response_model(buf: bytes) -> str | None:
    """The ``model`` Anthropic reports in ``message_start``: which model
    actually answered, so a tier rewrite can be checked, not assumed."""
    for line in buf.decode("utf-8", "replace").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            d = json.loads(line[6:])
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "message_start":
            model = (d.get("message") or {}).get("model")
            return model if isinstance(model, str) else None
    return None
