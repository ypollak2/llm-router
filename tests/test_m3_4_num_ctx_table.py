"""M3.4: one num_ctx table; every local caller reads it through num_ctx(model)."""
import pytest

from llm_router import local_models

TABLE = {
    "qwen3-coder:30b": 32768,
    "qwen3.6:35b-a3b-coding": 32768,
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
    ("QWEN3.5:latest", 32768),
    ("qwen3.5:9b", 32768),                # another tag of a listed :latest family
    ("llmr-classifier:latest", 4096),      # Ollama reports aliases with their tag
    ("ollama/llmr-classifier:latest", 4096),
    ("llmr-edit:latest", 16384),
    ("ollama/llmr-edit:latest", 16384),
    ("LLMR-Edit:Latest", 16384),
    ("qwen3.8:7b", 32768),
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
    assert server.ProxyConfig.from_env().num_ctx is None       # no pin: resolved per model
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
        old = 32768  # N20: server OLLAMA_CONTEXT_LENGTH
        assert local_models.num_ctx(m) == old


# --- proxy without --model (docs/proxy.md's default way to run it) -----------

@pytest.mark.parametrize("model,want", [("qwen3.5:latest", 32768), ("qwen3.8:latest", 32768),
                                        ("qwen3-coder:30b", 32768), ("llmr-edit", 16384)])
def test_make_backend_without_a_pin_uses_each_models_table_value(monkeypatch, tmp_path, model, want):
    """Probe 2026-10-07: cfg.model None gave cfg.num_ctx 32768, make_backend sent
    it to every policy-chosen model, and qwen3.5 (131072 from the hooks) got a
    second Ollama runner. make_backend must hand the model its own table value."""
    import asyncio

    import httpx

    from llm_router.proxy import server
    from llm_router.proxy.backends import OllamaBackend

    built = []

    class Spy(OllamaBackend):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            built.append(self)

        async def warm_up(self):
            return 0.0

    async def chain(_prompt):
        return "code", "moderate", [f"ollama/{model}"]

    monkeypatch.setattr(server, "BACKENDS", {"ollama/": Spy})
    monkeypatch.setattr(server, "policy_chain", chain)
    monkeypatch.setattr(server, "tool_capable", lambda _m: True)
    cfg = server.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "l.jsonl",
                             warm_up=False, hedge_s=None)
    assert cfg.model is None and cfg.num_ctx is None
    app = server.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={}))))
    result = asyncio.run(app.state.warm_up())
    assert result["warm_up"] == "ok" and result["model"] == f"ollama/{model}"
    assert [b.num_ctx for b in built] == [want]


def test_make_backend_without_a_pin_keeps_an_explicit_window(monkeypatch, tmp_path):
    import asyncio

    import httpx

    from llm_router.proxy import server
    from llm_router.proxy.backends import OllamaBackend

    built = []

    class Spy(OllamaBackend):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            built.append(self)

        async def warm_up(self):
            return 0.0

    async def chain(_prompt):
        return "code", "moderate", ["ollama/qwen3.5:latest"]

    monkeypatch.setattr(server, "BACKENDS", {"ollama/": Spy})
    monkeypatch.setattr(server, "policy_chain", chain)
    monkeypatch.setattr(server, "tool_capable", lambda _m: True)
    cfg = server.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "l.jsonl",
                             warm_up=False, hedge_s=None, num_ctx=49152)
    app = server.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={}))))
    asyncio.run(app.state.warm_up())
    assert [b.num_ctx for b in built] == [49152]


def test_direct_executor_falls_back_to_the_table_when_agent_loop_will_not_import(monkeypatch):
    """direct_executor._local_num_ctx: the import of agent_loop fails -> read the
    table directly, same value, operator override not applied (documented)."""
    import builtins

    from llm_router.hooks import direct_executor

    real_import = builtins.__import__

    def refuse(name, *a, **k):
        if name == "llm_router.hooks.agent_loop":
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", refuse)
    for model, want in TABLE.items():
        assert direct_executor._local_num_ctx(model) == want
    assert direct_executor._local_num_ctx(None) == 32768
