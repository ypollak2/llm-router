"""F33: follow-ups from the review of #558 (F14).

1. ``route_and_stream`` ends cleanly after ``route.aborted`` (it used to fall through to a
   stray ``ValueError("Unknown media task type")``).
2. The zero-dependency ``route_server`` ``/route`` refuses an image in ``prompt`` or the
   ``images`` field with HTTP 400, like the gateway routes (it used to answer 502 for a
   list prompt and silently drop the ``images`` field).
"""
from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from llm_router import route_server
from llm_router.types import RoutingProfile, TaskType

IMG = "iVBORw0KGgo="
IMG_PART = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + IMG}}


# ── route_and_stream: the aborted route ends cleanly ─────────────────────────

def _run_stream(monkeypatch, chain):
    from llm_router import providers, router

    async def fake_stream(*, model, messages, **kw):
        raise RuntimeError("provider down")
        yield  # pragma: no cover - makes this an async generator

    async def fake_ctx(**kw):
        return [{"role": "user", "content": kw.get("caller_context") or ""}]

    monkeypatch.setattr(router, "get_model_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(providers, "call_llm_stream_events", fake_stream)
    monkeypatch.setattr(router, "build_context_messages", fake_ctx)

    events: list[dict] = []

    async def go():
        async for ev in router.route_and_stream(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED):
            events.append(ev)

    asyncio.run(go())  # must not raise
    return events


def test_all_models_failed_ends_with_route_aborted_and_no_exception(monkeypatch):
    events = _run_stream(monkeypatch, ["openai/gpt-4o", "gemini/gemini-2.5-flash"])
    assert events, "the generator produced no events"
    last = events[-1]
    assert last["type"] == "route.aborted" and last["outcome"] == "all_models_failed"
    assert [e["type"] for e in events].count("route.aborted") == 1


def test_internal_error_ends_with_route_aborted_and_no_exception(monkeypatch):
    from llm_router import router

    def boom(*a, **k):
        raise RuntimeError("provider lookup blew up")

    events: list[dict] = []

    async def go():
        monkeypatch.setattr(router, "get_model_chain", lambda *a, **k: ["openai/gpt-4o"])
        monkeypatch.setattr(router, "provider_from_model", boom)  # inside the top-level try
        async for ev in router.route_and_stream(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED):
            events.append(ev)

    asyncio.run(go())  # must not raise
    assert events[-1]["type"] == "route.aborted"
    assert events[-1]["outcome"] == "internal_error"


# ── route_server /route: image refusal ───────────────────────────────────────

class _FakeResp:
    content = "x"
    model = "ollama/q"
    provider = "ollama"
    cost_usd = 0.0
    input_tokens = 1
    output_tokens = 1
    complexity = "simple"


@pytest.fixture
def server(monkeypatch):
    import llm_router.router as R

    calls: list[dict] = []

    async def fake_route(task_type, prompt, **kw):
        calls.append({"prompt": prompt})
        return _FakeResp()

    monkeypatch.setattr(R, "route_and_call", fake_route)
    srv = route_server.ThreadingHTTPServer(("127.0.0.1", 0), route_server.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, calls
    srv.shutdown()


def _post(srv, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_port}/route", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("payload", [
    {"prompt": [{"type": "text", "text": "what is this"}, IMG_PART]},
    {"prompt": [IMG_PART, {"type": "text", "text": "what is this"}]},
    {"prompt": "what is this", "images": [IMG]},
], ids=["parts-text-first", "parts-image-first", "images-field"])
def test_route_server_refuses_image_with_400(server, payload):
    srv, calls = server
    status, body = _post(srv, payload)
    assert status == 400, (status, body)
    assert body["error"] == route_server.IMAGES_UNSUPPORTED
    assert calls == [], "a refused request must not reach the router"


def test_route_server_text_and_history_images_still_served(server):
    srv, calls = server
    status, body = _post(srv, {"prompt": "hello"})
    assert status == 200 and body["text"] == "x"
    # images only in the history (``messages``) are flattened, never refused (D-165 (4))
    status, _ = _post(srv, {"prompt": "hello", "messages": [
        {"role": "user", "content": [IMG_PART]}, {"role": "assistant", "content": "ok"}]})
    assert status == 200
    assert len(calls) == 2


def test_route_server_and_gateway_refuse_with_the_same_text():
    pytest.importorskip("fastapi")
    from llm_router import gateway

    assert gateway._IMAGES_UNSUPPORTED == route_server.IMAGES_UNSUPPORTED
