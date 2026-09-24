"""I2: every local-model path requests the same context window.

The server default is 8192; with session context a draft overflowed it and
llama.cpp silently dropped the oldest tokens (the system prompt). And a num_ctx
that differs between calls makes Ollama reload the model — measured 3-6s on
qwen3.8 at 18 GB resident. So the draft path, the agent loop and the MCP
provider path all resolve to one value.
"""
from __future__ import annotations

import json

import pytest

from llm_router.hooks import direct_executor as de


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_OLLAMA_NUM_CTX", "LLM_ROUTER_LOCAL_NUM_CTX", "LLM_ROUTER_AGENT_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)


def test_the_draft_call_sends_the_shared_window(monkeypatch):
    sent = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"message": {"content": "ok"}, "done": True}).encode()
        def __iter__(self): return iter([json.dumps({"message": {"content": "ok"}, "done": True}).encode()])

    def fake_urlopen(req, timeout=None):
        sent.update(json.loads(req.data))
        return _R()

    monkeypatch.setattr(de.urllib.request, "urlopen", fake_urlopen)
    try:
        de.call_ollama("hi", "qwen3.8:latest", 5)
    except Exception:
        pass
    assert sent, "premise: a request was built"
    assert sent["options"].get("num_ctx") == 131072, sent["options"]


def test_all_three_paths_agree(monkeypatch):
    from llm_router.hooks.agent_loop import _num_ctx
    from llm_router.providers import _ollama_num_ctx
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "16384")
    assert _num_ctx() == _ollama_num_ctx() == de._local_num_ctx() == 16384
