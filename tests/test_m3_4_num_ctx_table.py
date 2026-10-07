"""M3.4: one num_ctx table; every local caller reads it through num_ctx(model)."""
import pytest

from llm_router import local_models

TABLE = {
    "qwen3-coder:30b": 32768,
    "qwen3.6:35b-a3b-coding": 32768,
    "qwen3.5:latest": 131072,
    "qwen3.8:latest": 131072,
    "llmr-classifier": 4096,
    "llmr-edit": 16384,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LLM_ROUTER_OLLAMA_NUM_CTX", "LLM_ROUTER_LOCAL_NUM_CTX",
              "LLM_ROUTER_AGENT_NUM_CTX", "LLM_ROUTER_PROXY_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)


def test_table_is_the_planned_one():
    assert local_models.NUM_CTX == TABLE
    assert local_models.DEFAULT_NUM_CTX == 32768


@pytest.mark.parametrize("model,want", list(TABLE.items()) + [
    ("ollama/qwen3-coder:30b", 32768),     # provider prefix is ignored
    ("QWEN3.5:latest", 131072),
    ("qwen3.5:9b", 131072),                # another tag of a listed :latest family
    ("qwen3.8:7b", 131072),
    ("some-new-model:7b", 32768),
    ("qwen3-coder:480b", 32768),
    ("", 32768),
    (None, 32768),
])
def test_num_ctx(model, want):
    assert local_models.num_ctx(model) == want


@pytest.mark.parametrize("model,want", list(TABLE.items()))
def test_every_caller_returns_the_table_value(model, want):
    from llm_router.hooks import agent_loop, direct_executor
    from llm_router.providers import _ollama_num_ctx
    from llm_router.warm import warmup_payload

    assert agent_loop._default_num_ctx(model) == want
    assert agent_loop._num_ctx(model) == want
    assert direct_executor._local_num_ctx(model) == want
    assert _ollama_num_ctx(f"ollama/{model}") == want
    assert warmup_payload(model)["options"]["num_ctx"] == want


@pytest.mark.parametrize("model,want", list(TABLE.items()))
def test_proxy_default_is_the_table_value(model, want):
    from llm_router.proxy import server
    from llm_router.proxy.backends import OllamaBackend

    assert server.ProxyConfig(model=f"ollama/{model}").num_ctx == want
    assert server.ProxyConfig.from_env().num_ctx == 32768      # no model
    assert OllamaBackend(f"ollama/{model}", None, base_url="http://x").num_ctx == want


def test_proxy_explicit_window_still_wins(monkeypatch):
    from llm_router.proxy import server
    from llm_router.proxy.backends import OllamaBackend

    assert server.ProxyConfig(model="ollama/qwen3.5:latest", num_ctx=65536).num_ctx == 65536
    assert OllamaBackend("ollama/x", None, base_url="http://x", num_ctx=1024).num_ctx == 1024
    monkeypatch.setenv("LLM_ROUTER_PROXY_NUM_CTX", "49152")
    assert server.ProxyConfig.from_env().num_ctx == 49152


def test_operator_override_still_beats_the_table(monkeypatch):
    from llm_router.hooks import direct_executor
    from llm_router.warm import warmup_payload

    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", "65536")
    assert direct_executor._local_num_ctx("llmr-edit") == 65536
    assert warmup_payload("llmr-edit")["options"]["num_ctx"] == 65536


def test_old_family_values_are_unchanged():
    """Zero-Claude edits must not move: the pre-M3.4 family rule gave these."""
    for m in ("qwen3.5:latest", "qwen3.8:latest", "qwen3-coder:30b", "x:1b"):
        old = 131072 if any(f in m for f in ("qwen3.5", "qwen3.8")) else 32768
        assert local_models.num_ctx(m) == old
