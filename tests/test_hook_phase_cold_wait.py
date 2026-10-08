"""M4.1: Ollama's own `load_duration` reaches the hook's `phases_ms.cold_wait`.

A cold-model wait inside a hook is invisible in wall time (it looks like a slow
model). `call_ollama` reads `load_duration` (nanoseconds) from Ollama's final
chunk and adds it as the `cold_wait` phase. Outside a hook process (nothing is
being timed: the MCP server, tests) the call is a no-op.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_router import hook_latency as hl
from llm_router.hooks import direct_executor

MODEL = "qwen3.5:latest"


class _Fake(BaseHTTPRequestHandler):
    load_duration: object = 2_500_000_000

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        final = {"message": {"content": ""}, "done": True, "prompt_eval_count": 5, "eval_count": 9}
        if type(self).load_duration is not None:
            final["load_duration"] = type(self).load_duration
        lines = [{"message": {"content": "A complete answer, with a full stop."}, "done": False}, final]
        body = "".join(json.dumps(x) + "\n" for x in lines).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    _Fake.load_duration = 2_500_000_000
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://%s:%d" % server.server_address
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", url)
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    hl._pending = None
    hl._phases.clear()
    try:
        yield _Fake
    finally:
        server.shutdown()
        server.server_close()
        hl._pending = None
        hl._phases.clear()


def test_load_duration_becomes_the_cold_wait_phase_in_milliseconds(fake):
    hl.begin("auto-route", "UserPromptSubmit")
    content, _usage = direct_executor.call_ollama("hi", MODEL, 5)
    assert content
    assert hl._phases["cold_wait"] == pytest.approx(2500.0)


def test_two_calls_in_one_hook_add_up(fake):
    hl.begin("auto-route", "UserPromptSubmit")
    direct_executor.call_ollama("hi", MODEL, 5)
    direct_executor.call_ollama("hi again", MODEL, 5)
    assert hl._phases["cold_wait"] == pytest.approx(5000.0)


def test_outside_a_hook_process_nothing_accumulates(fake):
    content, _usage = direct_executor.call_ollama("hi", MODEL, 5)
    assert content
    assert hl._phases == {}


@pytest.mark.parametrize("value", [None, 0, "slow", -5])
def test_a_missing_or_junk_load_duration_records_no_phase_and_does_not_break_the_call(fake, value):
    fake.load_duration = value
    hl.begin("auto-route", "UserPromptSubmit")
    content, _usage = direct_executor.call_ollama("hi", MODEL, 5)
    assert content
    assert "cold_wait" not in hl._phases
