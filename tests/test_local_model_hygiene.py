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
    for _m in ("qwen3-coder:30b", "qwen3.6:35b-a3b-coding", "qwen3.8:latest", "llmr-edit:latest",
               "llmr-classifier-38:latest"):  # real caps of llmr-classifier-38: completion, vision, tools, thinking
        _Stub.caps[_m] = ["completion", "vision", "tools", "thinking"]
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
    assert len(_Stub.shows) == 1  # unknown is remembered (fail-open) for 60 s: no second /api/show


def test_n19_unknown_verdict_expires_and_definite_verdict_is_rechecked(ollama, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    _Stub.show_status = 404
    discover.ollama_can_generate("ollama/nimble:9b")
    clock[0] += 61
    discover.ollama_can_generate("ollama/nimble:9b")
    assert len(_Stub.shows) == 2
    _Stub.show_status = 200
    clock[0] += 61
    assert discover.ollama_can_generate("ollama/nimble:9b") is False
    clock[0] += 3601
    _Stub.caps["nimble:9b"] = ["completion"]  # model replaced
    assert discover.ollama_can_generate("ollama/nimble:9b") is True


def test_n19_budget_models_fallback_is_filtered(ollama, monkeypatch):
    from llm_router.config import RouterConfig
    monkeypatch.setattr(discover, "get_cached_ollama_models", lambda: [])
    monkeypatch.setattr("llm_router.config.probe_ollama", lambda url: True)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    cfg = RouterConfig(ollama_base_url=ollama, ollama_budget_models="qwen3:8b,nimble:9b")
    assert cfg.all_ollama_models() == ["ollama/qwen3:8b"]


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


def test_n20_qwen_default_window_does_not_override_the_server_context(monkeypatch):
    from llm_router import local_models
    for m in ("qwen3.8:latest", "qwen3.5:latest", "ollama/qwen3.8:latest"):
        assert local_models.num_ctx(m) == 32768  # was 131072; /api/ps showed it overriding OLLAMA_CONTEXT_LENGTH


def test_n19_classifier_alias_is_excluded_without_calling_ollama(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert discover.ollama_can_generate("ollama/llmr-classifier:latest") is False
    assert discover.ollama_can_generate("ollama/llmr-edit:latest") is True


# N19b: the exact-name rule let llmr-classifier-38 and llamacpp:<hash> into code chains.
@pytest.mark.parametrize("name", [
    "ollama/llmr-classifier-38:latest", "ollama/llmr-classifier-38", "llmr-classifier-v2:latest",
    "ollama/LLMR-Classifier:latest",
    "ollama/llamacpp:44a38603922e14f1e3ae68c5d900cc39f2a925a9749d12ab6d268d75a01a399f",
])
def test_n19b_classifier_variants_and_unnamed_imports_excluded_without_calling_ollama(monkeypatch, name):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.setattr(discover, "_capability_cache", {})
    assert discover.ollama_can_generate(name) is False


def test_n19b_stub_reporting_completion_still_excluded(ollama):
    # every model the stub does not list answers caps=["completion"], like the real alias
    assert discover.ollama_can_generate("ollama/llmr-classifier-38:latest") is False
    assert "llmr-classifier-38:latest" not in _Stub.shows


def test_n19b_coder_models_kept(ollama):
    for m in ("ollama/qwen3-coder:30b", "ollama/qwen3.6:35b-a3b-coding", "ollama/qwen3.8:latest", "ollama/llmr-edit:latest"):
        assert discover.ollama_can_generate(m) is True


def test_n19b_configured_classifier_model_excluded_by_role(ollama, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_MODEL", "my-router-brain")
    assert discover.ollama_can_generate("ollama/my-router-brain:latest") is False
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "other-decider:1")
    assert discover.ollama_can_generate("ollama/other-decider:1") is False


def test_n19b_cached_model_list_drops_variants(ollama, monkeypatch):
    names = ("ollama/qwen3-coder:30b", "ollama/llmr-classifier-38:latest", "ollama/llamacpp:" + "a" * 64)
    monkeypatch.setattr(discover, "_load_cache", lambda ttl=0: {m: {"provider": "ollama"} for m in names})
    assert discover.get_cached_ollama_models() == ["ollama/qwen3-coder:30b"]


def test_n19b_classifier_path_unaffected(monkeypatch):
    from llm_router import local_classifier as lc
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_MODEL", "llmr-classifier-38")
    assert discover.ollama_can_generate("ollama/llmr-classifier-38") is False
    assert lc._model() == "llmr-classifier-38"
    assert lc.DEFAULT_MODEL == "llmr-classifier"
