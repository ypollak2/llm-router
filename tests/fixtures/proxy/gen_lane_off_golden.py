"""Regenerates ``golden_lane_off_main.json``: what the proxy sends upstream and returns for a fixed set
of requests (stored as sha256 of the exact bytes) with the tier rewrite on, ``haiku_rewrite`` on and the Opus pin, with NO lane A code involved.

It imports nothing from lane A, so it runs unchanged on a checkout from before lane A existed. The checked-in
golden was produced from main 224584a3 (the commit before Lane A PR1, #727):

    git worktree add /tmp/main-224584a3 224584a3
    PYTHONPATH=/tmp/main-224584a3/src python tests/fixtures/proxy/gen_lane_off_golden.py > golden_lane_off_main.json

``tests/test_proxy_lane_a_breaker.py::test_lane_off_is_byte_identical_to_the_frozen_main_golden`` replays the
same requests against the code under test with lane A off and compares byte for byte.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

OPUS = "claude-opus-5-5"
FIXTURE = Path(__file__).parent / "continuation_request.json"


def body(text="what does this function do?", *, prev="Read", model=OPUS, main=True, continuation=True, stream=True):
    b = json.loads(FIXTURE.read_text())
    b["model"] = model
    b["stream"] = stream
    msgs = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    if continuation:
        inp = {"command": "git status"} if prev == "Bash" else {"file_path": "/work/a.py"}
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": prev, "input": inp}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}]
    b["messages"] = msgs
    b["tools"] = [t for t in b["tools"] if t["name"] != "Agent"]
    if main:
        b["tools"].append({"name": "Agent", "description": "d", "input_schema": {"type": "object"}})
    b.pop("metadata", None)
    return b


def scenarios():
    return {
        "stream_sample": [body(), body("what is a mutex?", prev="Grep"), body(prev="Edit"), body(continuation=False),
                          body(main=False), body(model="claude-sonnet-5")],
        "non_stream_200": [body(stream=False)],
        "upstream_4xx": [body(stream=False)],
        "upstream_5xx": [body(stream=False)],
        "tool_use_continuation": [body(stream=False)],
    }


def upstream(case, log):
    def handle(request):
        log.append(request.content.decode())
        model = json.loads(request.content)["model"]
        if case == "upstream_4xx" or case == "upstream_5xx":
            status = 400 if case == "upstream_4xx" else 503
            return httpx.Response(status, stream=httpx.ByteStream(b'{"type": "error"}'),
                                  headers={"content-type": "application/json"})
        if case in ("non_stream_200", "tool_use_continuation"):
            content = ([{"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "/work/b.py"}}]
                       if case == "tool_use_continuation" else [{"type": "text", "text": "ok"}])
            obj = {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
                   "stop_reason": "tool_use" if case == "tool_use_continuation" else "end_turn",
                   "usage": {"input_tokens": 5, "output_tokens": 1}}
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(obj).encode()),
                                  headers={"content-type": "application/json"})
        events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                                      "content": [], "usage": {"input_tokens": 5, "output_tokens": 1}}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
                  {"type": "message_stop"}]
        sse = "".join("event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e)) for e in events).encode()
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})
    return handle


async def run(raw: dict) -> dict:
    """``raw``: the tier policy dict (lane A keys, if any, already in it)."""
    from llm_router.proxy import server as ps

    out = {}
    for case, bodies in scenarios().items():
        log: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            pf = Path(d) / "tiers.yaml"
            pf.write_text(yaml.safe_dump(raw))
            cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_ON,
                                 tier_policy=str(pf), ledger_path=Path(d) / "proxy_calls.jsonl")
            app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream(case, log))))
            responses = []
            h = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
                for b in bodies:
                    r = await c.post("/v1/messages?beta=true", content=json.dumps(copy.deepcopy(b)), headers=h)
                    responses.append({"status": r.status_code, "body": r.content.decode()})
        out[case] = {"upstream_requests": [digest(x) for x in log], "responses": [
            {"status": r["status"], "sha256": hashlib.sha256(r["body"].encode()).hexdigest()} for r in responses]}
    return out


def digest(raw: str) -> dict:
    return {"model": json.loads(raw)["model"], "bytes": len(raw), "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def policy(lane_a=None) -> dict:
    from llm_router.proxy import tiers as pt

    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    raw.pop("lane_a", None)
    raw.update({"haiku_rewrite": True, "pinned_models": [OPUS]})
    if lane_a is not None:
        raw["lane_a"] = lane_a
    return raw


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(policy())), indent=1, sort_keys=True))
    print("ran from", sys.modules["llm_router"].__file__, file=sys.stderr)
