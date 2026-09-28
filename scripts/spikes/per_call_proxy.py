"""SPIKE (2026-09-28): per-call proxy for Claude Code's /v1/messages traffic.

Not production code. Answers the questions in
docs/spikes/per-call-proxy-2026-09-28.md.

Run:
  PYTHONPATH=src python scripts/spikes/per_call_proxy.py --port 8787 \
      --log /path/calls.jsonl --route {off,continuation}

Point an ISOLATED Claude Code at it with ANTHROPIC_BASE_URL=http://127.0.0.1:8787.

Modes
  off           pure pass-through to https://api.anthropic.com (auth + call shape).
  continuation  route ONE step class -- a main-loop call whose newest user turn
                is only tool_result blocks (i.e. "given this tool output, what
                next?") -- to the first tool-capable non-Claude model in
                llm-router's OWN runtime chain (router._build_and_filter_chain
                for the classifier's task_type/complexity). Everything else,
                and every routed call that fails validation, goes to Anthropic.

Credentials are never logged: only the header NAME and a token-kind label.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import uuid

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

UPSTREAM = "https://api.anthropic.com"
OLLAMA = "http://127.0.0.1:11434"
HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}

ARGS: argparse.Namespace
_client: httpx.AsyncClient
_chain_cache: dict[tuple[str, str], list[str]] = {}


# ── logging ──────────────────────────────────────────────────────────────────
def _token_kind(v: str) -> str:
    v = v.strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    if v.startswith("sk-ant-oat"):
        return "oauth-access-token"
    if v.startswith("sk-ant-api"):
        return "api-key"
    return f"other(len={len(v)})"


def _auth_summary(headers) -> dict:
    out = {}
    for k in ("authorization", "x-api-key"):
        if k in headers:
            out[k] = _token_kind(headers[k])
    out["anthropic-beta"] = headers.get("anthropic-beta", "")
    out["header_names"] = sorted(h for h in headers.keys())
    return out


def _log(rec: dict) -> None:
    with open(ARGS.log, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _shape(body: dict) -> dict:
    msgs = _non_system(body.get("messages") or [])
    last = msgs[-1] if msgs else {}
    lc = last.get("content")
    last_types = [b.get("type") for b in lc] if isinstance(lc, list) else ["text"]
    # which tool produced the newest tool_result(s)
    prev_tools = []
    if len(msgs) >= 2 and isinstance(msgs[-2].get("content"), list):
        prev_tools = [b.get("name") for b in msgs[-2]["content"] if b.get("type") == "tool_use"]
    sys_ = body.get("system")
    sys_chars = len(sys_) if isinstance(sys_, str) else sum(len(b.get("text", "")) for b in (sys_ or []))
    tools = body.get("tools") or []
    return {
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "n_messages": len(msgs),
        "n_tools": len(tools),
        "tool_names": [t.get("name") for t in tools],
        "tools_chars": len(json.dumps(tools)),
        "system_chars": sys_chars,
        "last_user_block_types": last_types,
        "prev_tool_uses": prev_tools,
        "max_tokens": body.get("max_tokens"),
        "has_thinking": "thinking" in body,
    }


# ── step classification ─────────────────────────────────────────────────────
def _non_system(msgs: list) -> list:
    # Claude Code interleaves role="system" messages (budget/system reminders)
    # into `messages`, including AFTER the newest tool_result.
    return [m for m in msgs if m.get("role") != "system"]


def is_continuation(body: dict) -> bool:
    """The routed step class: main-loop call whose newest user turn is only tool_result."""
    if not body.get("tools"):
        return False  # side calls (titles, quota probes) carry no tools
    msgs = _non_system(body.get("messages") or [])
    if len(msgs) < 3 or msgs[-1].get("role") != "user":
        return False
    c = msgs[-1].get("content")
    if not isinstance(c, list) or not c:
        return False
    kinds = {b.get("type") for b in c}
    # Claude Code may append system-reminder text blocks next to tool_result
    return "tool_result" in kinds and kinds <= {"tool_result", "text"}


def _classify_text(body: dict) -> str:
    """Classify on the newest tool output + the original ask, not the 30KB preamble."""
    msgs = _non_system(body.get("messages") or [])
    first = msgs[0].get("content") if msgs else ""
    if isinstance(first, list):
        first = " ".join(b.get("text", "") for b in first if b.get("type") == "text")
    last = msgs[-1].get("content") if msgs else []
    tr = []
    for b in last if isinstance(last, list) else []:
        if b.get("type") == "tool_result":
            cc = b.get("content")
            tr.append(cc if isinstance(cc, str) else " ".join(x.get("text", "") for x in cc or []))
    return (first[-1500:] + "\n" + "\n".join(tr)[:1500]).strip()


async def policy_chain(text: str) -> tuple[str, str, list[str]]:
    from llm_router.classify import GATEWAY_POLICY, classify_signals
    from llm_router.config import get_config
    from llm_router.profiles import complexity_to_profile
    from llm_router.router import _build_and_filter_chain

    s = classify_signals(text, GATEWAY_POLICY)
    key = (s.task_type.value, s.complexity.value)
    if key not in _chain_cache:
        c = s.complexity
        _chain_cache[key] = await _build_and_filter_chain(
            s.task_type, complexity_to_profile(c), None, c, c, get_config())
    return key[0], key[1], _chain_cache[key]


def first_tool_capable(chain: list[str]) -> str | None:
    # Spike adapters: only ollama/* speaks tool calls here. codex/* is a
    # subprocess CLI (no tool channel); anthropic/* is the forward path.
    for m in chain:
        if m.startswith("ollama/"):
            return m
    return None


# ── Anthropic -> Ollama translation ─────────────────────────────────────────
def _tr_text(cc) -> str:
    if isinstance(cc, str):
        return cc
    return "\n".join(x.get("text", "") for x in cc or [] if x.get("type") == "text")


def to_ollama(body: dict, model: str) -> dict:
    sys_ = body.get("system")
    sys_text = sys_ if isinstance(sys_, str) else "\n\n".join(b.get("text", "") for b in (sys_ or []))
    out = [{"role": "system", "content": sys_text}] if sys_text else []
    id2name: dict[str, str] = {}
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        if m["role"] == "system":
            out.append({"role": "system", "content": _tr_text(c)})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in c if b.get("type") == "text")
            calls = []
            for b in c:
                if b.get("type") == "tool_use":
                    id2name[b["id"]] = b["name"]
                    calls.append({"function": {"name": b["name"], "arguments": b.get("input") or {}}})
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            texts = []
            for b in c:
                t = b.get("type")
                if t == "tool_result":
                    content = _tr_text(b.get("content"))
                    if b.get("is_error"):
                        content = "ERROR: " + content
                    out.append({"role": "tool", "tool_name": id2name.get(b.get("tool_use_id"), ""),
                                "content": content})
                elif t == "text":
                    texts.append(b.get("text", ""))
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    tools = [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", "")[:2000],
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
        for t in body.get("tools") or [] if "name" in t and "input_schema" in t]
    return {"model": model.split("/", 1)[1], "messages": out, "tools": tools, "stream": False,
            "think": False,
            "options": {"num_ctx": ARGS.num_ctx, "num_predict": min(body.get("max_tokens") or 4096, 4096)}}


def _schema_ok(inp, schema: dict) -> str | None:
    if not isinstance(inp, dict):
        return "input not an object"
    for r in schema.get("required", []):
        if r not in inp:
            return f"missing required '{r}'"
    props = schema.get("properties", {})
    tmap = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
            "array": list, "object": dict}
    for k, v in inp.items():
        if k not in props:
            if schema.get("additionalProperties") is False:
                return f"unknown property '{k}'"
            continue
        t = props[k].get("type")
        if isinstance(t, str) and t in tmap and not isinstance(v, tmap[t]):
            return f"'{k}' should be {t}"
    return None


def from_ollama(resp: dict, body: dict, model: str) -> tuple[dict | None, str | None]:
    """Return (anthropic_message, error). error => caller falls back."""
    msg = resp.get("message") or {}
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
    schemas = {t["name"]: t.get("input_schema", {}) for t in body.get("tools") or [] if "name" in t}
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        name, args = fn.get("name"), fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return None, f"tool args not JSON for {name}"
        if name not in schemas:
            return None, f"unknown tool '{name}'"
        err = _schema_ok(args, schemas[name])
        if err:
            return None, f"{name}: {err}"
        blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": name,
                       "input": args})
    if not blocks:
        return None, "empty response"
    has_tool = any(b["type"] == "tool_use" for b in blocks)
    return {
        "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
        "model": body.get("model"),  # echo requested model; Claude Code checks nothing else
        "content": blocks, "stop_sequence": None,
        "stop_reason": "tool_use" if has_tool else ("max_tokens" if resp.get("done_reason") == "length" else "end_turn"),
        "usage": {"input_tokens": resp.get("prompt_eval_count", 0),
                  "output_tokens": resp.get("eval_count", 0),
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }, None


def sse_from_message(m: dict) -> bytes:
    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"
    start = dict(m, content=[], stop_reason=None)
    start["usage"] = dict(m["usage"], output_tokens=1)
    parts = [ev("message_start", {"type": "message_start", "message": start})]
    for i, b in enumerate(m["content"]):
        if b["type"] == "text":
            parts.append(ev("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}}))
            parts.append(ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "text_delta", "text": b["text"]}}))
        else:
            parts.append(ev("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "tool_use", "id": b["id"],
                                                                      "name": b["name"], "input": {}}}))
            parts.append(ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "input_json_delta",
                                                              "partial_json": json.dumps(b["input"])}}))
        parts.append(ev("content_block_stop", {"type": "content_block_stop", "index": i}))
    parts.append(ev("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": m["stop_reason"], "stop_sequence": None},
                                      "usage": {"output_tokens": m["usage"]["output_tokens"]}}))
    parts.append(ev("message_stop", {"type": "message_stop"}))
    return "".join(parts).encode()


# ── upstream forward ────────────────────────────────────────────────────────
def _usage_from_sse(buf: bytes) -> tuple[dict, str | None, list[str]]:
    usage, stop, blocks = {}, None, []
    for line in buf.decode("utf-8", "replace").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            d = json.loads(line[6:])
        except ValueError:
            continue
        if d.get("type") == "message_start":
            usage.update(d["message"].get("usage") or {})
        elif d.get("type") == "message_delta":
            usage.update({k: v for k, v in (d.get("usage") or {}).items() if v is not None})
            stop = d.get("delta", {}).get("stop_reason")
        elif d.get("type") == "content_block_start":
            cb = d["content_block"]
            blocks.append(cb["type"] + (":" + cb["name"] if cb.get("name") else ""))
    return usage, stop, blocks


async def forward(request: Request, raw: bytes, rec: dict) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["accept-encoding"] = "identity"  # else httpx asks for gzip and aiter_raw relays it undecoded
    url = UPSTREAM + request.url.path + (("?" + request.url.query) if request.url.query else "")
    req = _client.build_request(request.method, url, headers=headers, content=raw)
    t0 = time.time()
    up = await _client.send(req, stream=True)
    rec["upstream_status"] = up.status_code
    resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP | {"content-encoding"}}

    async def body_iter():
        buf = bytearray()
        try:
            async for chunk in up.aiter_raw():
                buf.extend(chunk)
                yield chunk
        finally:
            await up.aclose()
            rec["latency_s"] = round(time.time() - t0, 2)
            ctype = up.headers.get("content-type", "")
            if "event-stream" in ctype:
                u, stop, blocks = _usage_from_sse(bytes(buf))
            else:
                try:
                    j = json.loads(bytes(buf))
                    u, stop = j.get("usage") or {}, j.get("stop_reason")
                    blocks = [b.get("type") + (":" + b["name"] if b.get("name") else "")
                              for b in j.get("content") or []] if isinstance(j.get("content"), list) else []
                    if up.status_code >= 400:
                        rec["error_body"] = str(j)[:300]
                except ValueError:
                    u, stop, blocks = {}, None, []
            rec.update(usage=u, stop_reason=stop, resp_blocks=blocks)
            _log(rec)

    return StreamingResponse(body_iter(), status_code=up.status_code, headers=resp_headers)


async def try_route(body: dict, rec: dict) -> dict | None:
    task, cx, chain = await policy_chain(_classify_text(body))
    target = first_tool_capable(chain)
    rec["route"] = {"task_type": task, "complexity": cx, "chain_head": chain[:4], "target": target}
    if not target:
        rec["route"]["outcome"] = "no-tool-capable-model-in-chain"
        return None
    payload = to_ollama(body, target)
    t0 = time.time()
    try:
        r = await _client.post(OLLAMA + "/api/chat", json=payload, timeout=ARGS.local_timeout)
        r.raise_for_status()
        m, err = from_ollama(r.json(), body, target)
    except Exception as e:  # noqa: BLE001 - spike: any failure is a counted fallback
        m, err = None, f"{type(e).__name__}: {e}"[:200]
    rec["route"]["latency_s"] = round(time.time() - t0, 2)
    if err:
        rec["route"]["outcome"] = "fallback"
        rec["route"]["error"] = err
        return None
    rec["route"]["outcome"] = "served"
    rec["route"]["resp_blocks"] = [b["type"] + (":" + b["name"] if b.get("name") else "") for b in m["content"]]
    rec["route"]["usage"] = m["usage"]
    rec["route"]["resp_preview"] = json.dumps(m["content"])[:400]
    return m


async def handle(request: Request) -> Response:
    raw = await request.body()
    rec = {"ts": time.time(), "method": request.method, "path": request.url.path,
           "req_bytes": len(raw), "auth": _auth_summary(request.headers)}
    body = None
    if request.url.path.endswith("/v1/messages") and request.method == "POST":
        try:
            body = json.loads(raw)
            rec["shape"] = _shape(body)
            if ARGS.dump_dir:
                rec["dump"] = f"{ARGS.dump_dir}/{int(rec['ts']*1000)}.json"
                with open(rec["dump"], "w") as f:
                    json.dump(body, f)
        except ValueError:
            pass
    if body is not None and ARGS.route == "continuation" and is_continuation(body):
        m = await try_route(body, rec)
        if m is not None:
            _log(rec)
            if body.get("stream"):
                return Response(sse_from_message(m), media_type="text/event-stream")
            return Response(json.dumps(m), media_type="application/json")
    return await forward(request, raw, rec)


def main() -> None:
    global ARGS, _client
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--log", required=True)
    ap.add_argument("--route", choices=["off", "continuation"], default="off")
    ap.add_argument("--dump-dir", default="", help="write request bodies here (scratch only; no headers)")
    ap.add_argument("--num-ctx", type=int, default=65536)
    ap.add_argument("--local-timeout", type=float, default=180.0)
    ARGS = ap.parse_args()
    _client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    app = Starlette(routes=[Route("/{p:path}", handle, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])
    uvicorn.run(app, host="127.0.0.1", port=ARGS.port, log_level="warning")


if __name__ == "__main__":
    main()
