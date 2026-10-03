"""local: escalate instead of letting Ollama truncate an oversized prompt.

Ollama (llama.cpp's ``--context-shift --keep 4``) does not reject a prompt
longer than the loaded model's context window — it silently drops the OLDEST
tokens (system prompt, start of task) and answers about whatever survived.
`local_context_guard` is the shared preflight that refuses to send a payload
that would not fit, and the shared postflight that flags it when Ollama's own
numbers say it truncated anyway. See the module docstring for the five call
paths this backstops.

HERMETICITY: `failopen` writes under `LLM_ROUTER_HOME`; every test here points
that at `tmp_path` and proves isolation before writing (same pattern as
tests/test_gf_c1_failopen_codes.py). `reset_cache()` is required for both
`local_context_guard` (the `/api/ps` cache) and `failopen` (`snapshot()`
memoises).
"""
from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from llm_router import failopen, local_context_guard as lcg
from llm_router.paths import is_isolated


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolated fail-open store + a clean `/api/ps` cache for every test."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
    failopen.reset_cache()
    failopen.clear()
    lcg.reset_cache()
    yield failopen
    lcg.reset_cache()
    failopen.reset_cache()


def codes(store) -> dict[str, int]:
    store.reset_cache()
    return dict(store.snapshot().by_code)


def _payload(n_chars: int, model: str = "qwen3-coder:30b") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "x" * n_chars}],
        "stream": False,
    }


# ── required test 1: an oversized prompt escalates and never reaches Ollama ──


def test_oversized_prompt_raises_and_never_calls_ollama(store, monkeypatch):
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("Ollama must not be called for an oversized prompt")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    # 4096-token default window, 256 reserved for the reply -> budget 3840
    # tokens -> ~3840*3.5/1.15 ≈ 11,683 chars is the largest payload that
    # still fits. Comfortably over that.
    payload = _payload(200_000)
    with pytest.raises(lcg.ContextOverflow):
        lcg.check_overflow(payload, site="test")

    assert not called
    assert codes(store) == {"CHZ-FO-LOCAL-CTX-OVERFLOW": 1}


# ── required test 2: a fitting prompt passes through normally ────────────────


def test_fitting_prompt_passes(store):
    payload = _payload(100, model="qwen3-coder:30b")
    lcg.check_overflow(payload, num_ctx=8192, site="test")  # must not raise
    assert codes(store) == {}


# ── required test 3: window source priority — num_ctx > /api/ps > env > default ──


def test_window_prefers_explicit_num_ctx_over_everything(store, monkeypatch):
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "2048")
    window, source = lcg.effective_window(num_ctx=16384, base_url="http://127.0.0.1:11434")
    assert (window, source) == (16384, "num_ctx")


def test_window_prefers_api_ps_over_env_and_default(store, monkeypatch):
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "2048")

    def _fake_urlopen(req, timeout=None):
        body = json.dumps({"models": [{"name": "qwen3-coder:30b", "context_length": 65536}]}).encode()
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    window, source = lcg.effective_window(base_url="http://127.0.0.1:11434", model="qwen3-coder:30b")
    assert (window, source) == (65536, "api_ps")


def test_window_falls_back_to_env_when_no_num_ctx_or_ps(store, monkeypatch):
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "2048")
    # No base_url at all -> /api/ps is never tried.
    window, source = lcg.effective_window()
    assert (window, source) == (2048, "env")


def test_window_falls_back_to_default_when_nothing_else_says(store, monkeypatch):
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    window, source = lcg.effective_window()
    assert (window, source) == (lcg.DEFAULT_CONTEXT_WINDOW, "default")


# ── required test 4: prompt_eval_count lower than the estimate is flagged ────


def test_low_prompt_eval_count_is_flagged_truncated(store):
    # ~1000-token estimate, server says it only evaluated 200 -> well under
    # the 0.6 ratio -> truncated.
    estimated = 1000
    flagged = lcg.check_truncated(200, estimated, window=4096, site="test", model="m")
    assert flagged is True
    assert codes(store) == {"CHZ-FO-LOCAL-CTX-TRUNCATED": 1}


def test_prompt_eval_count_at_the_window_is_flagged_truncated(store):
    flagged = lcg.check_truncated(4096, 4096, window=4096, site="test", model="m")
    assert flagged is True
    assert codes(store) == {"CHZ-FO-LOCAL-CTX-TRUNCATED": 1}


def test_prompt_eval_count_matching_the_estimate_is_not_flagged(store):
    flagged = lcg.check_truncated(950, 1000, window=4096, site="test", model="m")
    assert flagged is False
    assert codes(store) == {}


def test_missing_prompt_eval_count_is_not_flagged(store):
    assert lcg.check_truncated(None, 1000, window=4096, site="test", model="m") is False
    assert codes(store) == {}


# ── required test 5: fail-open when /api/ps errors ────────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fails_open_when_api_ps_errors(store, monkeypatch):
    def _raise(req, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "2048")

    window, source = lcg.effective_window(base_url="http://127.0.0.1:11434", model="qwen3-coder:30b")

    # Unknown, not zero and not a crash: falls through to the next source (env).
    assert (window, source) == (2048, "env")
    assert codes(store) == {"CHZ-FO-LOCAL-CTX-PS-UNREACHABLE": 1}


def test_api_ps_cache_avoids_a_second_round_trip(store, monkeypatch):
    calls = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        calls["n"] += 1
        body = json.dumps({"models": [{"name": "m", "context_length": 32768}]}).encode()
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    lcg.effective_window(base_url="http://127.0.0.1:11434", model="m")
    lcg.effective_window(base_url="http://127.0.0.1:11434", model="m")
    assert calls["n"] == 1  # second call served from the 5s cache


# ── estimate_tokens / estimate_payload_tokens ────────────────────────────────


def test_estimate_tokens_applies_the_safety_margin():
    # 350 chars / 3.5 chars-per-token * 1.15 margin = 115
    assert lcg.estimate_tokens(350) == 115


def test_estimate_payload_tokens_counts_tools_not_just_messages():
    """The exact gap hooks/context_budget.py's own estimate leaves open — see
    local_context_guard's module docstring."""
    small = {"messages": [{"role": "user", "content": "hi"}]}
    with_tools = dict(small, tools=[{"name": "t", "description": "x" * 5000}])
    assert lcg.estimate_payload_tokens(with_tools) > lcg.estimate_payload_tokens(small)


def test_estimate_payload_tokens_never_raises_on_odd_values():
    class Weird:
        def __str__(self):
            return "weird"

    payload = {"messages": "ok", "thing": Weird()}
    # json.dumps(..., default=str) covers this; the sum-of-str() fallback
    # exists for whatever that still can't handle. Either way: no raise.
    assert lcg.estimate_payload_tokens(payload) > 0
