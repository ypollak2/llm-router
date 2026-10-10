"""N19 (capability filter) and N20 (bounded keep_alive). Ollama HTTP is stubbed; no real model is touched."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from llm_router import discover, warm
from llm_router.proxy import backends


class _Stub(BaseHTTPRequestHandler):
    caps: dict = {}
    show_status = 200
    shows: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).shows.append(body["model"])
        if self.show_status != 200:
            self.send_response(self.show_status)
            self.end_headers()
            return
        out = json.dumps({"capabilities": self.caps[body["model"]]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def ollama(monkeypatch):
    _Stub.caps = {"qwen3:8b": ["completion", "tools"], "nimble:9b": ["decision"], "emb:1": ["embedding"]}
    _Stub.show_status = 200
    _Stub.shows = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    monkeypatch.setattr(discover, "_capability_cache", {})
    from llm_router import config
    monkeypatch.setattr(config, "_config", None, raising=False)
    try:
        yield url
    finally:
        srv.shutdown()
        srv.server_close()


def test_n19_model_without_completion_is_excluded(ollama):
    assert discover.ollama_can_generate("ollama/qwen3:8b") is True
    assert discover.ollama_can_generate("ollama/nimble:9b") is False
    assert discover.ollama_can_generate("ollama/emb:1") is False


def test_n19_answer_is_cached(ollama):
    discover.ollama_can_generate("ollama/nimble:9b")
    discover.ollama_can_generate("ollama/nimble:9b")
    assert _Stub.shows.count("nimble:9b") == 1


def test_n19_fails_open_when_show_unavailable(ollama):
    _Stub.show_status = 500
    assert discover.ollama_can_generate("ollama/nimble:9b") is True
    assert discover.ollama_can_generate("ollama/nimble:9b") is True
    assert len(_Stub.shows) == 2  # a failed lookup is not cached


def test_n19_fails_open_without_base_url(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.setattr(discover, "_capability_cache", {})
    assert discover.ollama_can_generate("ollama/nimble:9b") is True


def test_n19_cached_model_list_drops_it(ollama, monkeypatch):
    cache = {m: {"provider": "ollama"} for m in ("ollama/qwen3:8b", "ollama/nimble:9b")}
    monkeypatch.setattr(discover, "_load_cache", lambda ttl=0: cache)
    assert discover.get_cached_ollama_models() == ["ollama/qwen3:8b"]


def test_n19_classifier_path_does_not_use_the_filter(ollama, monkeypatch):
    from llm_router import decision_classifier as dc
    monkeypatch.setattr(discover, "ollama_can_generate", lambda n: False)
    assert dc.payload(dc.DEFAULT_MODEL, "x")["model"] == dc.DEFAULT_MODEL


def test_n20_edit_keep_alive_default_is_bounded(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", raising=False)
    assert warm.edit_keep_alive() == 600
    monkeypatch.setenv("LLM_ROUTER_LOCAL_KEEP_ALIVE", "-1")
    assert warm.edit_keep_alive() == -1


def test_n20_proxy_default_is_bounded_and_explicit_minus_one_honoured():
    assert backends.DEFAULT_KEEP_ALIVE == 600
    from llm_router.proxy.server import parse_keep_alive
    assert parse_keep_alive("-1") == -1
