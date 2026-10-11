"""P1.3 provider path: trim, then validated larger local window."""
from __future__ import annotations

import copy
import types

import pytest

from llm_router import context_tags, failopen, local_context_guard as lcg, local_models, model_discovery
from llm_router.paths import is_isolated

SMALL = "ollama/small:latest"
BIG = "ollama/big:latest"


class Sent(Exception):
    def __init__(self, kwargs: dict):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    assert is_isolated()
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "512")
    monkeypatch.delenv("LLM_ROUTER_PACK_OVERFLOW", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROMOTE_GATE", raising=False)
    failopen.reset_cache(); failopen.clear(); lcg.reset_cache()  # noqa: E702
    yield
    failopen.reset_cache(); lcg.reset_cache()


def _messages(big: bool = True) -> list[dict[str, str]]:
    project = "knowledge " * 4000 if big else ""
    system = "base\n" + context_tags.wrap("project", project)
    return [{"role": "system", "content": system}, {"role": "user", "content": "request " * 500}]


def _install(monkeypatch, *, installed=("big:latest",), valid=(BIG,), big_window=8192,
             serves=True, gate_off=False):
    table = {"small:latest": 512}
    if big_window is not None:
        table["big:latest"] = big_window
    monkeypatch.setattr(local_models, "NUM_CTX", table)
    monkeypatch.setattr(model_discovery, "available_ollama_models", lambda: list(installed))
    monkeypatch.setattr(model_discovery, "ensure_grandfather", lambda *a, **k: None)
    monkeypatch.setattr(model_discovery, "grandfathered_ids", lambda: frozenset(valid))
    monkeypatch.setattr(model_discovery, "_env_configured_ids", lambda: set())
    monkeypatch.setattr(model_discovery, "_routing_yaml_pin_ids", lambda: set())
    monkeypatch.setattr(local_models, "serves_generate", lambda m: serves)
    if gate_off:
        monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")


def _patch_provider(monkeypatch):
    import litellm
    from llm_router import providers

    cfg = types.SimpleNamespace(default_temperature=0.2, default_max_tokens=256, request_timeout=5)
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    monkeypatch.setattr(providers, "inject_cache_control", lambda messages, model: messages)
    async def boom(**kwargs):
        raise Sent(kwargs)
    monkeypatch.setattr(litellm, "acompletion", boom)
    return providers


async def _drive(site: str, monkeypatch, messages: list[dict[str, str]]):
    providers = _patch_provider(monkeypatch)
    if site == "call_llm":
        await providers.call_llm(SMALL, messages)
    else:
        async for _ in providers.call_llm_stream_events(SMALL, messages):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["call_llm", "stream"])
@pytest.mark.parametrize("case", [
    "off", "unvalidated", "unvalidated_gate_off", "unknown_window",
    "no_fit", "absent_installed", "non_generator",
])
async def test_window_failover_refuses_invalid_candidates(site, case, monkeypatch):
    if case != "off":
        monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    opts = {}
    if case == "unvalidated":
        opts["valid"] = ()
    elif case == "unvalidated_gate_off":
        opts.update(valid=(), gate_off=True)
    elif case == "unknown_window":
        opts["big_window"] = None
    elif case == "no_fit":
        opts["big_window"] = 1024
    elif case == "absent_installed":
        opts["installed"] = ()
    elif case == "non_generator":
        opts["serves"] = False
    _install(monkeypatch, **opts)
    with pytest.raises(lcg.ContextOverflow):
        await _drive(site, monkeypatch, _messages())


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["call_llm", "stream"])
async def test_window_failover_uses_validated_bigger_window(site, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    _install(monkeypatch)
    original = _messages()
    before = copy.deepcopy(original)
    with pytest.raises(Sent) as sent:
        await _drive(site, monkeypatch, original)
    kwargs = sent.value.kwargs
    assert kwargs["model"] == BIG
    assert kwargs["num_ctx"] == 8192
    assert "knowledge" not in kwargs["messages"][0]["content"]
    assert "request " * 100 in kwargs["messages"][1]["content"]
    assert original == before
    assert failopen.snapshot().by_code.get("CHZ-FO-PACK-OVERFLOW-BIGGER-WINDOW") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["call_llm", "stream"])
async def test_pack_overflow_off_tiny_request_never_discovers(site, monkeypatch):
    providers = _patch_provider(monkeypatch)
    monkeypatch.setattr(model_discovery, "available_ollama_models", lambda: (_ for _ in ()).throw(AssertionError("discovered")))
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(Sent) as sent:
        await _drive(site, monkeypatch, messages)
    assert sent.value.kwargs["messages"] is messages
    assert sent.value.kwargs["model"] == SMALL
