"""F91: follow-ups to F87 (#611).

1. Gateway ``POST /route`` answers 429 + ``Retry-After`` for a provider RateLimitError
   (also one behind ``__cause__``); the router's aggregate RuntimeError stays 502.
2. A 400 echoes only router-authored errors (``RouterInputError``); any other ValueError
   gets ``<Class>: invalid request``.
3. The local log holds the exception class and a length, never the exception text.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import llm_router.route_server as route_server  # noqa: E402
from llm_router.gateway import app  # noqa: E402
from llm_router.types import RouterInputError  # noqa: E402

PROMPT = "SEEDED-PROMPT-QWV-do-not-echo"
SECRET = "sk-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"


class RateLimitError(Exception):
    """Shaped like litellm / openai / anthropic ``RateLimitError`` (found by class name)."""

    def __init__(self, msg="slow down", retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class LibraryValueError(ValueError):
    pass


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1")


def _fail(monkeypatch, exc):
    async def failing(payload):
        raise exc

    monkeypatch.setattr(route_server, "route_payload_async", failing)


# ── 1. /route 429 + Retry-After ───────────────────────────────────────────────
def test_route_provider_rate_limit_is_429_with_retry_after(client, monkeypatch):
    _fail(monkeypatch, RateLimitError(f"{PROMPT} {SECRET}", retry_after=7))
    r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 429
    ra = r.headers.get("retry-after")
    assert ra is not None and ra.isdigit() and int(ra) == 7
    assert r.json()["detail"] == "RateLimitError: upstream rate limit; retry after 7 s"
    assert PROMPT not in r.text and SECRET not in r.text


def test_route_rate_limit_without_retry_after_has_no_header(client, monkeypatch):
    _fail(monkeypatch, RateLimitError())
    r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 429
    assert "retry-after" not in r.headers
    assert r.json()["detail"] == "RateLimitError: upstream rate limit"


def test_route_rate_limit_behind_cause_is_429(client, monkeypatch):
    try:
        try:
            raise RateLimitError(retry_after=3)
        except RateLimitError as inner:
            raise LookupError("wrapped") from inner
    except LookupError as wrapped:
        _fail(monkeypatch, wrapped)
    r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 429 and r.headers.get("retry-after") == "3"
    assert r.json()["detail"].endswith("; retry after 3 s")


def test_route_aggregate_runtime_error_stays_502(client, monkeypatch):
    _fail(monkeypatch, RuntimeError("All models failed: RateLimitError: quota exceeded"))
    r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 502
    assert "retry-after" not in r.headers
    assert r.json()["detail"] == "RuntimeError: routing failed"


# ── 2. 400: router-authored echoed, anything else is not ──────────────────────
def _planted():
    return f"bad value {PROMPT} key={SECRET}"


@pytest.mark.parametrize("exc", [
    LibraryValueError(_planted()),
    json.JSONDecodeError(_planted(), "doc", 0),
    ValueError(_planted()),
])
def test_gateway_400_does_not_echo_a_library_value_error(client, monkeypatch, caplog, exc):
    _fail(monkeypatch, exc)
    with caplog.at_level("WARNING", logger="llm_router.gateway"):
        r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 400
    assert r.json()["detail"] == f"{type(exc).__name__}: invalid request"
    assert PROMPT not in r.text and SECRET not in r.text
    assert PROMPT not in caplog.text and SECRET not in caplog.text


def test_gateway_400_still_echoes_a_router_authored_error(client, monkeypatch):
    msg = "Invalid model_override format: 'x'. Use 'provider/model' format."
    _fail(monkeypatch, RouterInputError(msg))
    r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 400 and r.json()["detail"] == msg


def test_gateway_wire_endpoint_400_does_not_echo_a_library_value_error(client, monkeypatch):
    _fail(monkeypatch, LibraryValueError(_planted()))
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": PROMPT}]})
    assert r.status_code == 400
    assert PROMPT not in r.text and SECRET not in r.text
    assert "LibraryValueError: invalid request" in r.text


def test_real_validation_sites_raise_router_input_error():
    import asyncio

    for payload, text in [({"prompt": "  "}, "missing 'prompt'"),
                          ({"prompt": "x", "tools": "no"}, "'tools' must be a list"),
                          ({"prompt": "x", "messages": "no"}, "'messages' must be a list")]:
        with pytest.raises(RouterInputError, match=text):
            asyncio.run(route_server.route_payload_async(payload))


# ── zero-dep route_server ─────────────────────────────────────────────────────
@pytest.fixture
def rs_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), route_server.make_handler())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _post(url, data: bytes):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_route_server_400_library_value_error_not_echoed_router_one_is(rs_url, monkeypatch, caplog):
    def failing(payload):
        raise LibraryValueError(_planted())

    monkeypatch.setattr(route_server, "route_payload", failing)
    with caplog.at_level("WARNING", logger="llm_router.gateway"):
        status, body = _post(rs_url + "/route", json.dumps({"prompt": PROMPT}).encode())
    assert status == 400
    assert json.loads(body)["error"] == "LibraryValueError: invalid request"
    assert PROMPT not in body and SECRET not in body
    assert PROMPT not in caplog.text and SECRET not in caplog.text

    def router_authored(payload):
        raise RouterInputError("missing 'prompt'")

    monkeypatch.setattr(route_server, "route_payload", router_authored)
    status, body = _post(rs_url + "/route", b"{}")
    assert status == 400 and json.loads(body)["error"] == "missing 'prompt'"


# ── 3. the log holds class + length, never text ───────────────────────────────
def test_route_failure_log_has_class_and_length_but_no_text(client, monkeypatch, caplog):
    boom = RuntimeError(f"upstream: {PROMPT} (no key here)")
    _fail(monkeypatch, boom)
    with caplog.at_level("WARNING", logger="llm_router.gateway"):
        client.post("/route", json={"prompt": PROMPT})
    assert "RuntimeError" in caplog.text
    assert str(len(str(boom))) in caplog.text
    assert PROMPT not in caplog.text and "upstream" not in caplog.text
