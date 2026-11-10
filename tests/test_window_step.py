"""LLM_ROUTER_WINDOW_STEP: one flag raises the shared local model's window and every cap
derived from it; unset changes nothing; every site that sends num_ctx for that model agrees."""
from __future__ import annotations

import pytest

MODEL = "qwen3.6:35b-a3b-coding"
STEP = 131072


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", MODEL)
    for k in ("LLM_ROUTER_WINDOW_STEP", "LLM_ROUTER_LOCAL_NUM_CTX", "LLM_ROUTER_AGENT_NUM_CTX",
              "LLM_ROUTER_OLLAMA_NUM_CTX", "LLM_ROUTER_PROXY_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def stepped(unset, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", str(STEP))


def _show(window):
    from llm_router.local_check.runtimes import OllamaAdapter
    a = OllamaAdapter("http://127.0.0.1:11435")
    a._show_info = lambda model: {"model_info": {"qwen35moe.context_length": 262144}}
    return a.context_length("m")


def test_unset_values_unchanged(unset):
    from llm_router import local_models
    from llm_router.proxy import local_mode
    assert local_models.raised_window() is None
    assert local_models.num_ctx(MODEL) == 32768
    assert local_models.num_ctx("ollama/" + MODEL) == 32768
    assert local_models.num_ctx("llmr-classifier") == 4096
    assert local_mode.prompt_cap_tokens() == local_mode.PROMPT_CAP_TOKENS == 25_000
    assert _show(None) == 32768


@pytest.mark.parametrize("bad", ["", "abc", "16384", "0", "-5", "32767"])
def test_invalid_or_below_floor_is_ignored(unset, monkeypatch, bad):
    from llm_router import local_models
    monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", bad)
    assert local_models.raised_window() is None
    assert local_models.num_ctx(MODEL) == 32768


def test_floor_itself_is_accepted(unset, monkeypatch):
    from llm_router import local_models
    monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", "32768")
    assert local_models.num_ctx(MODEL) == 32768


def test_step_needs_a_shared_model(stepped, monkeypatch):
    from llm_router import local_models
    from llm_router.proxy import local_mode
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL")
    assert local_models.raised_window() is None
    assert local_models.num_ctx(MODEL) == 32768
    assert local_mode.prompt_cap_tokens() == 25_000


def test_step_values(stepped):
    from llm_router import local_models
    from llm_router.proxy import local_mode
    from llm_router.proxy.okf_context import MAX_BLOCK_TOKENS
    assert local_models.num_ctx(MODEL) == STEP
    assert local_models.num_ctx("ollama/" + MODEL) == STEP
    assert local_models.num_ctx(None) == STEP          # a caller naming no model gets the shared one
    assert local_models.num_ctx("llmr-classifier") == 4096
    assert local_models.num_ctx("qwen3-coder:30b") == 32768
    assert local_mode.prompt_cap_tokens() == STEP - local_mode.LOCAL_NUM_PREDICT - 256 - MAX_BLOCK_TOKENS == 127268
    assert local_mode.MIN_NUM_CTX == 32768
    assert _show(None) == STEP


def test_cap_follows_the_decision(stepped):
    from llm_router.proxy import local_mode
    big = {"messages": [{"role": "user", "content": "x" * int(local_mode.OTHER_CHARS_PER_TOKEN * 60_000)}]}
    assert local_mode.over_prompt_cap(big) is None                 # 60k est: over 25k, under 127k
    huge = {"messages": [{"role": "user", "content": "x" * int(local_mode.OTHER_CHARS_PER_TOKEN * 140_000)}]}
    assert local_mode.over_prompt_cap(huge) is not None


def test_cap_unset_refuses_the_same_60k(unset):
    from llm_router.proxy import local_mode
    big = {"messages": [{"role": "user", "content": "x" * int(local_mode.OTHER_CHARS_PER_TOKEN * 60_000)}]}
    assert local_mode.over_prompt_cap(big) is not None


def test_default_window_of_the_gate_follows(stepped):
    from llm_router.proxy.local_gate import _default_window
    assert _default_window(MODEL) == STEP


def _sites():
    """Every in-process site that picks the num_ctx it sends for a local model."""
    from llm_router import local_classifier, local_models, providers
    from llm_router.hooks import agent_loop, direct_executor
    from llm_router.proxy.backends import OllamaBackend
    from llm_router.proxy.server import ProxyConfig
    from llm_router.toolkit.adapters import ollama
    return {
        "local_models.num_ctx": local_models.num_ctx(MODEL),
        "ollama.num_ctx": ollama.num_ctx(MODEL),
        "ollama.default_num_ctx": ollama.default_num_ctx(MODEL),
        "ollama.shared_runner_opts": ollama.shared_runner_opts(MODEL)[0],
        "providers._ollama_num_ctx": providers._ollama_num_ctx("ollama/" + MODEL),
        "agent_loop._num_ctx": agent_loop._num_ctx(MODEL),
        "direct_executor._local_num_ctx": direct_executor._local_num_ctx(MODEL),
        "local_classifier._num_ctx_for": local_classifier._num_ctx_for(MODEL),
        "ProxyConfig.num_ctx": ProxyConfig(model=MODEL).num_ctx,
        "OllamaBackend.num_ctx": OllamaBackend(MODEL, None, base_url="http://127.0.0.1:11435").num_ctx,
    }


@pytest.mark.parametrize("flag,expected", [(None, 32768), (str(STEP), STEP)])
def test_every_num_ctx_site_agrees(unset, monkeypatch, flag, expected):
    if flag:
        monkeypatch.setenv("LLM_ROUTER_WINDOW_STEP", flag)
    # operator overrides that must lose to the one-model policy
    monkeypatch.setenv("LLM_ROUTER_AGENT_NUM_CTX", "8192")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    sites = _sites()
    assert set(sites.values()) == {expected}, sites


def test_window_probe_sends_the_shared_window(stepped):
    from llm_router.resolver.calibrate import Reply, probe_long_context
    seen = []

    class C:
        supports_tools = supports_images = False

        def chat(self, model, messages, *, tools=None, num_ctx=None, timeout=1.0):
            seen.append(num_ctx)
            return Reply(text="")

    probe_long_context(C(), MODEL, 1.0, tokens=8000, window=STEP)
    assert seen == [STEP]
    probe_long_context(C(), "other:7b", 1.0, tokens=8000, window=STEP)
    assert seen[-1] == 9024


def test_env_var_registered():
    from llm_router.env_registry import registered_names
    assert "LLM_ROUTER_WINDOW_STEP" in registered_names()
