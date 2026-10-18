"""F87: no client-facing error carries provider exception text (it can echo the prompt).

Rule from #608 / F76: the client gets the exception CLASS and a fixed message; the scrubbed
detail goes only to the local ``llm_router.gateway`` logger. Covered here: the gateway's
native ``POST /route`` and ``POST /ground``, and the zero-dep ``route_server`` handler
(``/route``, ``/feedback``, bad JSON).
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

PROMPT = "SEEDED-PROMPT-ZXQ-do-not-echo"
SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


class ProviderBoom(Exception):
    pass


def _boom():
    return ProviderBoom(f"upstream rejected: {PROMPT} key={SECRET}")


def _assert_clean(text: str, cls: str = "ProviderBoom") -> None:
    assert PROMPT not in text and SECRET not in text, text
    assert cls in text, text


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1")


def test_gateway_route_does_not_echo_the_provider_error(client, monkeypatch, caplog):
    async def failing(payload):
        raise _boom()

    monkeypatch.setattr(route_server, "route_payload_async", failing)
    with caplog.at_level("WARNING", logger="llm_router.gateway"):
        r = client.post("/route", json={"prompt": PROMPT})
    assert r.status_code == 502
    _assert_clean(r.text)
    assert "routing failed" in r.json()["detail"]
    assert SECRET not in caplog.text            # the local log is scrubbed too
    assert "ProviderBoom" in caplog.text


def test_gateway_ground_does_not_echo_the_error(client, monkeypatch, caplog):
    from llm_router import grounding

    def failing(*a, **k):
        raise _boom()

    monkeypatch.setattr(grounding, "grounding_violations", failing)
    with caplog.at_level("WARNING", logger="llm_router.gateway"):
        r = client.post("/ground", json={"draft": "see src/x.py", "prompt": PROMPT})
    assert r.status_code == 502
    _assert_clean(r.text)
    assert "grounding check failed" in r.json()["detail"]
    assert SECRET not in caplog.text and "ProviderBoom" in caplog.text


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


def test_route_server_route_does_not_echo_the_provider_error(rs_url, monkeypatch, caplog):
    def failing(payload):
        raise _boom()

    monkeypatch.setattr(route_server, "route_payload", failing)
    with caplog.at_level("WARNING", logger="llm_router.gateway"):
        status, body = _post(rs_url + "/route", json.dumps({"prompt": PROMPT}).encode())
    assert status == 502
    _assert_clean(body)
    assert "routing failed" in json.loads(body)["error"]
    assert SECRET not in caplog.text and "ProviderBoom" in caplog.text


def test_route_server_feedback_does_not_echo_the_error(rs_url, monkeypatch):
    from llm_router import quality_feedback

    def failing(payload):
        raise _boom()

    monkeypatch.setattr(quality_feedback, "record_loophole_verdict", failing)
    status, body = _post(rs_url + "/feedback", json.dumps({"prompt": PROMPT}).encode())
    assert status == 500
    _assert_clean(body)
    assert "feedback failed" in json.loads(body)["error"]


def test_route_server_bad_json_names_the_class_only(rs_url):
    status, body = _post(rs_url + "/route", b'{"prompt": ' + PROMPT.encode())
    assert status == 400
    assert PROMPT not in body
    assert "JSONDecodeError" in body and "bad json" in body
