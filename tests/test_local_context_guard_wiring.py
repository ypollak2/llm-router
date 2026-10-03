"""Per-site wiring for `local_context_guard`: each local call path refuses an
oversized prompt BEFORE it reaches Ollama, and treats Ollama's own
`prompt_eval_count` correctly afterwards.

The guard's own behaviour (window priority, estimate, fail-open) is in
tests/test_local_context_guard.py; the proxy sites are in tests/test_proxy.py.
This file covers `hooks/direct_executor.call_ollama` (and through it the
zero-Claude edit path), `hooks/agent_loop.run_agent_loop` and
`providers.call_llm` / `call_llm_stream_events`. Ollama is a local fake server
or a monkeypatched transport; no test touches a real model.
"""
from __future__ import annotations

import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_router import failopen
from llm_router import local_context_guard as lcg
from llm_router.hooks import agent_loop, direct_executor
from llm_router.paths import is_isolated

MODEL = "qwen3-coder:30b"
WINDOW = 2048


class _Fake(BaseHTTPRequestHandler):
    posts: list = []
    prompt_eval_count = 5
    reply = "The answer is forty-two, as the original question required. And a second sentence follows here"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        type(self).posts.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        lines = [
            {"message": {"content": type(self).reply}, "done": False},
            {"message": {"content": ""}, "done": True,
             "prompt_eval_count": type(self).prompt_eval_count, "eval_count": 9},
        ]
        body = "".join(json.dumps(x) + "\n" for x in lines).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    assert is_isolated()
    failopen.reset_cache()
    failopen.clear()
    lcg.reset_cache()
    _Fake.posts = []
    _Fake.prompt_eval_count = 5
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://%s:%d" % server.server_address
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", url)
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", str(WINDOW))
    monkeypatch.delenv("LLM_ROUTER_AGENT_NUM_CTX", raising=False)
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_NUM_CTX", raising=False)
    try:
        yield _Fake
    finally:
        server.shutdown()
        server.server_close()
        lcg.reset_cache()
        failopen.reset_cache()


def _codes() -> dict:
    failopen.reset_cache()
    return dict(failopen.snapshot().by_code)


def _chat_posts():
    return [p for path, p in _Fake.posts if path == "/api/chat"]


# ── hooks/direct_executor.call_ollama (also the zero-Claude edit path) ───────


def test_call_ollama_refuses_an_oversized_prompt_without_calling_ollama(fake):
    content, usage = direct_executor.call_ollama("x" * 60_000, MODEL, 5)
    assert (content, usage) == (None, {})
    assert _chat_posts() == []
    assert _codes().get("CHZ-FO-LOCAL-CTX-OVERFLOW") == 1


def test_call_ollama_passes_a_fitting_prompt(fake):
    content, usage = direct_executor.call_ollama("hi", MODEL, 5)
    assert content.startswith("The answer is forty-two")
    assert len(_chat_posts()) == 1
    assert "CHZ-FO-LOCAL-CTX-OVERFLOW" not in _codes()


def test_a_full_window_prompt_eval_count_marks_the_draft_incomplete(fake):
    fake.prompt_eval_count = WINDOW  # Ollama evaluated exactly the window: it trimmed
    content, _usage = direct_executor.call_ollama("hi", MODEL, 5)
    assert "overflowed the model's context window" in content
    assert _codes().get("CHZ-FO-LOCAL-CTX-TRUNCATED") == 1


def test_a_low_prompt_eval_count_is_recorded_but_does_not_discard_the_answer(fake):
    """A warm KV cache reports a low prompt_eval_count for an intact prompt
    (tests/test_warm_plan_3_7.py::test_resident_model_is_served_with_keep_alive
    is the regression this guards). Pad the prompt so 5 is far below the
    estimate, yet still fits the window."""
    content, _usage = direct_executor.call_ollama("y" * 3000, MODEL, 5)
    assert content.startswith("The answer is forty-two")
    assert "overflowed" not in content
    assert _codes().get("CHZ-FO-LOCAL-CTX-TRUNCATED") == 1


# ── hooks/agent_loop.run_agent_loop ──────────────────────────────────────────


def test_agent_loop_refuses_an_oversized_prompt_and_never_calls_ollama(fake, monkeypatch, tmp_path):
    calls = []
    # Repo-knowledge retrieval is not under test and costs seconds of real I/O.
    monkeypatch.setattr("llm_router.context_injection.inject_system_prompt",
                        lambda system_prompt, *a, **k: system_prompt)
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("sent")))
    out = agent_loop.run_agent_loop("z" * 60_000, MODEL, tmp_path)
    assert out is None  # no tools used yet -> caller falls back to Claude
    assert calls == []
    assert _codes().get("CHZ-FO-LOCAL-CTX-OVERFLOW", 0) >= 1


# ── providers.call_llm / call_llm_stream_events ──────────────────────────────


def _fake_litellm(captured: list):
    async def _acompletion(**kwargs):
        captured.append(kwargs)
        usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0)
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    return _acompletion


@pytest.mark.asyncio
async def test_call_llm_refuses_an_oversized_ollama_prompt_before_litellm(fake, monkeypatch):
    import litellm

    from llm_router.providers import call_llm
    captured: list = []
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", str(WINDOW))
    with pytest.raises(lcg.ContextOverflow):
        await call_llm(f"ollama/{MODEL}", [{"role": "user", "content": "x" * 60_000}])
    assert captured == []
    assert _codes().get("CHZ-FO-LOCAL-CTX-OVERFLOW") == 1


@pytest.mark.asyncio
async def test_call_llm_does_not_guard_a_cloud_model(fake, monkeypatch):
    import litellm

    from llm_router.providers import call_llm
    captured: list = []
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    await call_llm("openai/gpt-4o-mini", [{"role": "user", "content": "x" * 60_000}])
    assert len(captured) == 1
    assert "CHZ-FO-LOCAL-CTX-OVERFLOW" not in _codes()


@pytest.mark.asyncio
async def test_call_llm_passes_a_fitting_ollama_prompt(fake, monkeypatch):
    import litellm

    from llm_router.providers import call_llm
    captured: list = []
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(captured))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", str(WINDOW))
    await call_llm(f"ollama/{MODEL}", [{"role": "user", "content": "hi"}])
    assert len(captured) == 1
