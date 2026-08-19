"""The HTTP /route endpoint that lets external processes (e.g. LoopHole's
`llm_router:` provider) route through LLM Router's router over HTTP."""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from llm_router import route_server


class _FakeResp:
    content = "hello from llm_router"
    model = "ollama/qwen2.5-coder:7b"
    provider = "ollama"
    cost_usd = 0.0
    input_tokens = 5
    output_tokens = 3
    complexity = "simple"


def _patch_router(monkeypatch, capture=None):
    import llm_router.router as R

    async def _fake_route(task_type, prompt, **kw):
        if capture is not None:
            capture["task_type"] = task_type
            capture["prompt"] = prompt
            capture.update(kw)
        return _FakeResp()

    monkeypatch.setattr(R, "route_and_call", _fake_route)


def test_route_payload_maps_request_and_response(monkeypatch):
    cap = {}
    _patch_router(monkeypatch, cap)
    out = route_server.route_payload(
        {"prompt": "hi", "complexity": "simple", "system": "be terse"})
    assert out["text"] == "hello from llm_router"
    assert out["model"].endswith("qwen2.5-coder:7b") and out["provider"] == "ollama"
    # request mapping
    assert cap["prompt"] == "hi"
    assert cap["complexity_hint"] == "simple"
    assert cap["system_prompt"] == "be terse"
    assert str(cap["task_type"].value) == "code"        # default task_type


def test_missing_prompt_raises():
    with pytest.raises(ValueError):
        route_server.route_payload({"prompt": "   "})


def test_http_health_and_route_end_to_end(monkeypatch):
    _patch_router(monkeypatch)
    srv = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
        ("127.0.0.1", 0), route_server.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:{}".format(srv.server_address[1])
        health = json.loads(urllib.request.urlopen(base + "/health", timeout=5).read())
        assert health == {"ok": True}

        body = json.dumps({"prompt": "go", "complexity": "complex"}).encode()
        req = urllib.request.Request(base + "/route", data=body,
                                     headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert out["text"] == "hello from llm_router"
    finally:
        srv.shutdown()
        srv.server_close()
