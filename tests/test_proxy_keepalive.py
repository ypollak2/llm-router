"""SSE keepalive for a long local step (llm_router.proxy.keepalive, LLM_ROUTER_PROXY_KEEPALIVE)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm_router.proxy import deadline, keepalive
from llm_router.proxy.translate import from_ollama
from tests.test_proxy_local_mode import Backend, _first_call, _ollama, _overrides_readable, _post, env  # noqa: F401  (fixtures)

CLAUDE_SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_claude","content":[]}}\n\n'
              b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
              b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (keepalive.ENV_FLAG, keepalive.ENV_AFTER, keepalive.ENV_INTERVAL, deadline.ENV_FLAG, deadline.ENV_CAP):
        monkeypatch.delenv(k, raising=False)


class SlowBackend(Backend):
    """Whole reply after ``delay`` s (the proxy's backend is not token-streaming), or a timeout after it."""

    def __init__(self, reply, delay, *, timeout=False):
        super().__init__(reply)
        self.delay, self.timeout = delay, timeout

    async def complete(self, body, timeout_s):
        await asyncio.sleep(self.delay)
        if self.timeout:
            raise asyncio.TimeoutError
        m, err = from_ollama(self.replies[0], body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


def _upstream(status=200, body=CLAUDE_SSE, ctype="text/event-stream"):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, stream=httpx.ByteStream(body), headers={"content-type": ctype})

    handler.requests = seen
    return handler


def _stream_body():
    return dict(_first_call(), stream=True)


def _events(resp) -> list[tuple[str, dict]]:
    out, name = [], None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            out.append((name, json.loads(line[6:])))
    return out


def _names(resp) -> list[str]:
    return [n for n, _ in _events(resp)]


def _on(monkeypatch, after="0.05", interval="0.05"):
    monkeypatch.setenv(keepalive.ENV_FLAG, "on")
    monkeypatch.setenv(keepalive.ENV_AFTER, after)
    monkeypatch.setenv(keepalive.ENV_INTERVAL, interval)


def test_flag_default_off_and_knobs(monkeypatch):
    assert keepalive.enabled() is False
    monkeypatch.setenv(keepalive.ENV_FLAG, "1")
    assert keepalive.enabled() is True
    assert (keepalive.after_s(), keepalive.interval_s()) == (15.0, 10.0)
    monkeypatch.setenv(keepalive.ENV_INTERVAL, "nan")
    assert keepalive.interval_s() == 10.0


def test_error_frame_uses_upstream_type_and_message():
    f = keepalive.error_frame(429, json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}).encode())
    assert f.startswith(b"event: error\ndata: ")
    d = json.loads(f.split(b"data: ")[1])
    assert d == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    assert json.loads(keepalive.error_frame(529, b"junk").split(b"data: ")[1])["error"]["type"] == "overloaded_error"
    assert json.loads(keepalive.error_frame(502, b"").split(b"data: ")[1])["error"]["type"] == "api_error"


def test_cap_rises_to_600_only_with_keepalive(monkeypatch):
    assert deadline.compute(10_000_000, [])["deadline_s"] == 240.0
    assert deadline.compute(10_000_000, [], keepalive=True)["deadline_s"] == 600.0
    monkeypatch.setenv(deadline.ENV_CAP, "300")          # an explicit cap still wins
    assert deadline.compute(10_000_000, [], keepalive=True)["deadline_s"] == 300.0


@pytest.mark.timing
async def test_slow_local_pings_then_served_message(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    env.up = up = _upstream()
    r = await _post(env.app(SlowBackend(_ollama("hello"), 0.4)), _stream_body())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    names = _names(r)
    pings = names.count("ping")
    assert 4 <= pings <= 9                               # 0.4 s at 0.05 s cadence, from t=0.05 s
    assert names[:pings] == ["ping"] * pings             # every ping precedes the message
    assert names[pings:].count("message_start") == 1 and names[-1] == "message_stop"
    assert up.requests == []                             # served locally, Claude never called
    (row,) = env.rows()
    assert row["decision"] == "served" and row["committed_early"] is True and row["keepalive_pings"] == pings


@pytest.mark.timing
async def test_fast_local_never_commits_early(env, monkeypatch):  # noqa: F811
    _on(monkeypatch, after="5")
    r = await _post(env.app(SlowBackend(_ollama("hello"), 0.0)), _stream_body())
    assert "ping" not in _names(r) and _names(r).count("message_start") == 1
    (row,) = env.rows()
    assert "committed_early" not in row and "keepalive_pings" not in row


@pytest.mark.timing
async def test_slow_local_timeout_falls_back_to_claude_in_same_stream(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    env.up = up = _upstream()
    r = await _post(env.app(SlowBackend(_ollama("x"), 0.3, timeout=True)), _stream_body())
    assert r.status_code == 200
    names = _names(r)
    pings = names.count("ping")
    assert pings >= 3 and names[:pings] == ["ping"] * pings
    assert names[pings:] == ["message_start", "content_block_start", "message_stop"]   # exactly one message_start
    assert "msg_claude" in r.text and len(up.requests) == 1
    (row,) = env.rows()
    assert row["decision"] == "fallback" and row["reason"] == "budget_exceeded"
    assert row["committed_early"] is True and row["keepalive_pings"] == pings and row["upstream_status"] == 200


@pytest.mark.timing
async def test_fallback_upstream_429_after_commit_becomes_error_event(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    err = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
    env.up = _upstream(429, err, "application/json")
    r = await _post(env.app(SlowBackend(_ollama("x"), 0.3, timeout=True)), _stream_body())
    assert r.status_code == 200                           # committed before the upstream answered
    events = _events(r)
    assert [n for n, _ in events].count("message_start") == 0
    n, d = events[-1]
    assert n == "error" and d["error"] == {"type": "rate_limit_error", "message": "slow down"}
    assert [n for n, _ in events[:-1]] == ["ping"] * (len(events) - 1)
    (row,) = env.rows()
    assert row["upstream_status"] == 429 and row["committed_early"] is True


async def test_flag_off_is_identical(env):  # noqa: F811
    r = await _post(env.app(SlowBackend(_ollama("hello"), 0.1)), _stream_body())
    assert "ping" not in _names(r) and _names(r).count("message_start") == 1
    (row,) = env.rows()
    assert "committed_early" not in row and "keepalive_pings" not in row


@pytest.mark.timing
async def test_non_streaming_is_unchanged(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    r = await _post(env.app(SlowBackend(_ollama("hello"), 0.3)), dict(_first_call(), stream=False))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["content"][0]["text"] == "hello"
    (row,) = env.rows()
    assert "committed_early" not in row


async def test_streaming_flag_raises_size_cap(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv(deadline.ENV_FLAG, "1")
    monkeypatch.setenv(keepalive.ENV_FLAG, "1")
    monkeypatch.setattr(deadline, "recent_samples", lambda *a, **k: [])
    seen = {}
    real = deadline.compute

    def spy(tokens, samples=None, *, keepalive=False):
        seen["k"] = keepalive
        return real(tokens, samples, keepalive=keepalive)

    monkeypatch.setattr(deadline, "compute", spy)
    await _post(env.app(SlowBackend(_ollama("hi"), 0.0)), _stream_body())
    assert seen["k"] is True
    seen.clear()
    await _post(env.app(SlowBackend(_ollama("hi"), 0.0)), dict(_first_call(), stream=False))
    assert seen["k"] is False
