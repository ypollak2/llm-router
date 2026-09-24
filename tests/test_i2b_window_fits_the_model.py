"""I2b: the local context window is sized per model, not one number for all.

I2 chose 131072 from a measurement on qwen3.8 alone. On qwen3-coder:30b (the
agent loop's first choice) the same window loaded at 70 GB, 45% on CPU, on this
52 GB Mac, and every read-only draft then timed out at the 45s per-call limit.
Measured 2026-09-24, resident size by num_ctx (32K / 64K / 128K):

    qwen3-coder:30b   31 GB GPU / 49 GB 11% CPU / 70 GB 45% CPU
    qwen3.5           6.6 / 8.1 / 10 GB, all GPU
    qwen3.8           18 / 18 / 17 GB, all GPU

So 131072 only for families measured to fit; 32768 for everything else.
"""
from __future__ import annotations

import pytest

from llm_router.hooks.agent_loop import _num_ctx


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_OLLAMA_NUM_CTX", "LLM_ROUTER_LOCAL_NUM_CTX", "LLM_ROUTER_AGENT_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("model,window", [
    ("qwen3.8:latest", 131072),
    ("qwen3.5:latest", 131072),
    ("qwen3-coder:30b", 32768),     # 131072 spilled 45% to CPU
    ("some-new-model:7b", 32768),   # unmeasured: the window known to be safe
    (None, 32768),
])
def test_the_default_window_is_the_one_measured_to_fit(model, window):
    assert _num_ctx(model) == window


def test_an_explicit_window_wins_for_every_model(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "65536")
    assert _num_ctx("qwen3-coder:30b") == _num_ctx("qwen3.8:latest") == 65536


def test_every_local_path_sizes_by_model():
    from llm_router.hooks import direct_executor as de
    from llm_router.providers import _ollama_num_ctx
    assert de._local_num_ctx("qwen3-coder:30b") == 32768
    assert _ollama_num_ctx("ollama/qwen3-coder:30b") == 32768
    assert _ollama_num_ctx("ollama/qwen3.8:latest") == 131072


def test_the_draft_request_carries_the_models_window(monkeypatch):
    import json
    from llm_router.hooks import direct_executor as de
    sent = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([json.dumps({"message": {"content": "ok"}, "done": True}).encode()])
        def read(self): return b"{}"

    monkeypatch.setattr(de.urllib.request, "urlopen",
                        lambda req, timeout=None: (sent.update(json.loads(req.data)), _R())[1])
    try:
        de.call_ollama("hi", "qwen3-coder:30b", 5)
    except Exception:
        pass
    assert sent["options"]["num_ctx"] == 32768
