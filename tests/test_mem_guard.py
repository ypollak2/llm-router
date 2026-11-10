"""Memory guard (ensure_fits): no local model load past what this machine's memory allows.

Incident 2026-11-09 (SYNTH-200): qwen3.6 (29.2 GB) + qwen3.8-uncensored (24.8 GB) + nimble:9b (10.7 GB) resident on a
48 GB Mac, 5,670 Metal OOM lines, every local reply empty. Every test fakes the Ollama servers and the host memory.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

MAIN = "http://127.0.0.1:11435"
SMALL = "http://127.0.0.1:11434"
GB = 1_000_000_000
GIB = 1024 ** 3
TARGET = "target:30b"


class _R:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._p).encode()


class Fake:
    """Fake Ollama pair. ps: {port: [rows]}; tags: {name: bytes}; a missing port = unreachable."""

    def __init__(self, monkeypatch, ps, tags=None, show=None):
        self.ps, self.tags, self.show, self.calls, self.unloaded = ps, tags or {}, show or {}, [], []
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, req, *a, **k):
        url = getattr(req, "full_url", req)
        self.calls.append(url)
        port = url.split(":")[2].split("/")[0]
        if url.endswith("/api/ps"):
            if port not in self.ps:
                raise OSError("connection refused")
            return _R({"models": self.ps[port]})
        if url.endswith("/api/tags"):
            return _R({"models": [{"name": n, "size": s} for n, s in self.tags.items()]})
        if url.endswith("/api/show"):
            name = json.loads(req.data)["model"]
            if name not in self.show:
                raise OSError("no such model")
            val = self.show[name]
            return _R(val if isinstance(val, dict) else {"size": val})
        if url.endswith("/api/generate"):
            body = json.loads(req.data)
            assert body["keep_alive"] == 0, body
            self.unloaded.append((port, body["model"]))
            self.ps[port] = [r for r in self.ps[port] if r["name"] != body["model"]]
            return _R({"done": True})
        raise AssertionError("unexpected request " + url)


def row(name, gb):
    return {"name": name, "size": int(gb * GB), "size_vram": int(gb * GB)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("LLM_ROUTER_LOCAL_MODEL", "LLM_ROUTER_LOCAL_MODEL_URL", "LLM_ROUTER_MEM_GUARD",
                "LLM_ROUTER_MEM_ON_PRESSURE", "LLM_ROUTER_MEM_RESERVE_GB", "LLM_ROUTER_MEM_HEADROOM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", SMALL)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_ROUTER_MEM_HEADROOM", "1.0")
    from llm_router.toolkit.adapters import ollama as oa
    oa._PS_CACHE.clear()
    oa._RESERVED.clear()
    oa._SHOW_CACHE.clear()
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)
    monkeypatch.setattr(oa, "_metal_cap_gib", lambda total: None)
    host(monkeypatch, 48.0, 40.0)
    yield
    oa._PS_CACHE.clear()
    oa._RESERVED.clear()


def host(monkeypatch, total_gib, free_gib):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(oa, "_machine_memory", lambda: (total_gib, free_gib))


# -- the rule ---------------------------------------------------------------------------------------------------

def test_refuses_25gb_target_with_40gb_resident_on_48gb(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:30b", 25), row("b:30b", 15)]}, {TARGET: 25 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded) as ei:
        oa.ensure_fits(TARGET, SMALL, "t")
    assert isinstance(ei.value, oa.LocalModelConflict) and ei.value.rule == "sum"
    assert "a:30b" in str(ei.value) and TARGET in str(ei.value)


def test_allows_when_it_fits(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:7b", 5)]}, {TARGET: 20 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")


def test_allows_when_target_already_resident_even_over_budget(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [row(TARGET, 44)]}, {TARGET: 44 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")
    assert not any(u.endswith("/api/tags") for u in fake.calls)      # no size lookup for a model already loaded


def test_target_is_not_counted_against_itself(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row(TARGET, 30)], "11435": [row("small:1b", 2)]}, {TARGET: 30 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")
    oa.ensure_fits("ollama/" + TARGET, SMALL, "t")


def test_free_rule_b_catches_other_apps_eating_ram(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": []}, {TARGET: 10 * GB})
    host(monkeypatch, 48.0, 6.0)         # sum rule passes (10 << 48-9.6) but only 6 GiB is free
    with pytest.raises(oa.MemoryBudgetExceeded) as ei:
        oa.ensure_fits(TARGET, SMALL, "t")
    assert ei.value.rule == "free"


def test_headroom_scales_the_target(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:30b", 20)]}, {TARGET: 15 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")                                # 20 + 14 GiB-ish <= 48 - 9.6
    oa._PS_CACHE.clear()
    monkeypatch.setenv("LLM_ROUTER_MEM_HEADROOM", "2.0")
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t")


def test_sums_both_servers(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    Fake(monkeypatch, {"11434": [row("nimble:9b", 10)], "11435": [row("big:30b", 29)]}, {TARGET: 15 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):                      # 39 + 14 > 38.4
        oa.ensure_fits(TARGET, SMALL, "t")


def test_target_size_falls_back_to_api_show(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, tags={}, show={TARGET: 25 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t")


# -- fail open --------------------------------------------------------------------------------------------------

def _failopen_rows():
    from llm_router import failopen
    path = failopen.store_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_ps_down_allows_and_records_failopen(monkeypatch):
    from llm_router import failopen
    from llm_router.toolkit.adapters import ollama as oa
    failopen.clear()
    Fake(monkeypatch, {}, {TARGET: 45 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")
    assert any(r["c"] == "CHZ-FO-MEM-GUARD-PS" for r in _failopen_rows())


def test_unknown_target_size_allows_and_records_failopen(monkeypatch):
    from llm_router import failopen
    from llm_router.toolkit.adapters import ollama as oa
    failopen.clear()
    Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {})
    oa.ensure_fits(TARGET, SMALL, "t")
    assert any(r["c"] == "CHZ-FO-MEM-GUARD-SIZE" for r in _failopen_rows())


def test_vm_stat_unreadable_allows_when_the_sum_fits(monkeypatch):
    from llm_router import failopen
    from llm_router.toolkit.adapters import ollama as oa
    failopen.clear()
    Fake(monkeypatch, {"11434": []}, {TARGET: 10 * GB})
    host(monkeypatch, 48.0, None)
    oa.ensure_fits(TARGET, SMALL, "t")
    assert any(r["c"] == "CHZ-FO-MEM-GUARD-HOST" for r in _failopen_rows())


def test_host_probe_raising_allows(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {TARGET: 30 * GB})

    def boom():
        raise RuntimeError("sysctl missing")
    monkeypatch.setattr(oa, "_machine_memory", boom)
    oa.ensure_fits(TARGET, SMALL, "t")


# -- never skipped, cached per (model, url) ---------------------------------------------------------------------

def test_short_deadline_still_checks(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {TARGET: 25 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t", deadline_s=0.2)
    oa._PS_CACHE.clear()
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.check_resident(TARGET, deadline_s=0.2)


def test_cache_is_keyed_on_model_and_url(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [row("a:30b", 30)], "11435": []},
                {TARGET: 25 * GB, "tiny:1b": 1 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t")
    n = len(fake.calls)
    with pytest.raises(oa.MemoryBudgetExceeded):                      # same key: served from the cache, no I/O
        oa.ensure_fits(TARGET, SMALL, "t")
    assert len(fake.calls) == n
    oa.ensure_fits("tiny:1b", SMALL, "t")                             # other model: its own decision
    with pytest.raises(oa.MemoryBudgetExceeded):                      # same model on another server: re-evaluated
        oa.ensure_fits(TARGET, MAIN, "t")
    assert len(fake.calls) > n


def test_kill_switch(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {TARGET: 25 * GB})
    monkeypatch.setenv("LLM_ROUTER_MEM_GUARD", "off")
    oa.ensure_fits(TARGET, SMALL, "t")
    assert fake.calls == []


def test_one_model_rule_survives_the_kill_switch(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", TARGET)
    monkeypatch.setenv("LLM_ROUTER_MEM_ON_PRESSURE", "refuse")
    monkeypatch.setenv("LLM_ROUTER_MEM_GUARD", "off")
    Fake(monkeypatch, {"11434": [row("other:30b", 20)]}, {TARGET: 5 * GB})
    with pytest.raises(oa.LocalModelConflict):
        oa.ensure_fits(TARGET, SMALL, "t")


# -- evict ------------------------------------------------------------------------------------------------------

def test_evict_unloads_the_largest_idle_model_then_allows(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_MEM_ON_PRESSURE", "evict")
    fake = Fake(monkeypatch, {"11434": [row("mid:5b", 5), row("big:30b", 22)]}, {TARGET: 25 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")
    assert fake.unloaded == [("11434", "big:30b")]


def test_evict_never_unloads_the_allowed_model(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", "allowed:30b")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MAX_OTHER_VRAM_GB", "100")      # isolate the budget rule from the one-model rule
    fake = Fake(monkeypatch, {"11434": [row("allowed:30b", 28), row("small:7b", 5)]}, {TARGET: 25 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t")                               # evicting small:7b is not enough
    assert ("11434", "allowed:30b") not in fake.unloaded


def test_default_is_refuse_without_a_local_model(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {TARGET: 25 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t")
    assert fake.unloaded == []


def test_default_is_evict_when_the_local_model_is_set(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", TARGET)
    fake = Fake(monkeypatch, {"11434": [row("other:30b", 20)]}, {TARGET: 25 * GB})
    oa.ensure_fits(TARGET, SMALL, "t")                                    # the stray model is dropped, then it fits
    assert fake.unloaded == [("11434", "other:30b")]


# -- every load path calls ensure_fits --------------------------------------------------------------------------

@pytest.fixture
def spy(monkeypatch):
    """Replace ensure_fits with a spy that refuses every load, so a path that skips it sends a request."""
    from llm_router.toolkit.adapters import ollama as oa
    seen: list[tuple] = []

    def _refuse(model, base_url=None, purpose="", *, deadline_s=None):
        seen.append((model, base_url, purpose))
        raise oa.MemoryBudgetExceeded("sum", str(model), str(base_url), 1.0, 1.0, 1.0)
    monkeypatch.setattr(oa, "ensure_fits", _refuse)

    def _no_network(req, *a, **k):
        raise AssertionError("request sent past a refused memory guard: " + str(getattr(req, "full_url", req)))
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    from llm_router import local_privacy
    monkeypatch.setattr(local_privacy, "open_url", lambda req, *, base_url, timeout: _no_network(req))
    return seen


class _Client:
    def __init__(self): self.posts = []
    async def post(self, *a, **k):
        self.posts.append(a)
        raise AssertionError("httpx request sent past a refused memory guard")
    def stream(self, *a, **k):
        self.posts.append(a)
        raise AssertionError("httpx request sent past a refused memory guard")


def test_proxy_backend_complete_warm_up_probe_are_guarded(spy):
    from llm_router.proxy.backends import OllamaBackend
    from llm_router.toolkit.adapters.ollama import LocalModelConflict
    client = _Client()
    be = OllamaBackend("ollama/m:7b", client, base_url=MAIN)
    for call in (lambda: be.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]}, 5.0),
                 lambda: be.warm_up(5.0), lambda: be.probe(5.0)):
        with pytest.raises(LocalModelConflict):
            asyncio.run(call())
    assert client.posts == [] and [s[2] for s in spy] == [
        "proxy.OllamaBackend.complete", "proxy.OllamaBackend.warm_up", "proxy.OllamaBackend.probe"]
    assert all(s[0] == "m:7b" and s[1] == MAIN for s in spy)


def test_proxy_step_generator_is_guarded(spy):
    from llm_router.local_agent.proxy_step import generator_for
    from llm_router.proxy.backends import OllamaBackend
    client = _Client()
    be = OllamaBackend("ollama/m:7b", client, base_url=MAIN)
    out = asyncio.run(generator_for(be)("sys", "prompt", 50, 5.0))
    assert out[0] is None and client.posts == []
    assert spy and spy[0][2] == "local_agent.proxy_step.generate"


def test_compact_embedder_is_guarded(spy):
    from llm_router.local_agent.compact import ollama_embedder
    from llm_router.toolkit.adapters.ollama import LocalModelConflict
    client = _Client()
    with pytest.raises(LocalModelConflict):
        asyncio.run(ollama_embedder(client, MAIN, "nomic-embed-text")(["x"]))
    assert client.posts == [] and spy[0][2] == "local_agent.compact.embed"


def test_semantic_cache_embedding_is_guarded(spy):
    from llm_router import semantic_cache
    assert semantic_cache._get_embedding("hello", SMALL) is None
    assert spy and spy[0][:3] == ("nomic-embed-text", SMALL, "semantic_cache.embedding")


def test_semantic_classify_embedding_is_guarded(spy):
    from llm_router import semantic_classify
    assert semantic_classify._embed_ollama("hello", SMALL) is None
    assert spy and spy[0][2] == "semantic_classify.embed"


def test_sealer_librarian_is_guarded(spy):
    from llm_router.library import sealer
    assert sealer._librarian("p", timeout=1) is None
    assert spy and spy[0][2] == "library.sealer.librarian"


def test_react_client_is_guarded(spy):
    from llm_router.agentic import react
    turn = react._default_ollama_client("m:7b", SMALL)([{"role": "user", "content": "x"}], [])
    assert turn.error and "memory_budget_exceeded" in turn.error
    assert spy and spy[0][2] == "agentic.react.client"


def test_vision_probe_is_guarded(spy, monkeypatch):
    from llm_router import vision_registry
    monkeypatch.setattr(vision_registry, "_probe_image", lambda code: b"png")
    assert vision_registry.probe_model("v:7b", timeout=1) is False
    assert spy and spy[0][2] == "vision_registry.probe"


def test_edit_model_warmup_is_guarded(spy, monkeypatch):
    from llm_router import warm
    popen = []
    monkeypatch.setattr(warm.subprocess, "Popen", lambda cmd, **k: popen.append(cmd))
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE_SCOPE", "edit")
    monkeypatch.setenv("LLM_ROUTER_ZCE_WARMUP", "on")
    assert warm.warm_edit_model_bg("m:7b") is None
    assert popen == [] and spy[0][2] == "warm.edit_model"       # guarded even with LLM_ROUTER_LOCAL_MODEL unset


def _load_hook(name):
    import importlib.util
    from pathlib import Path

    import llm_router
    path = Path(llm_router.__file__).parent / "hooks" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_start_warmup_is_guarded_without_a_local_model(spy, monkeypatch):
    ss = _load_hook("session-start.py")
    popen = []
    monkeypatch.setattr(ss.subprocess, "Popen", lambda cmd, **k: popen.append(cmd))
    monkeypatch.setattr(ss, "_detached_spawn_disabled", lambda: False)
    monkeypatch.setattr(ss, "_EDIT_WARMED_MODEL", None)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP", "on")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP_MODEL", "mine:7b")
    monkeypatch.setenv("OLLAMA_BASE_URL", SMALL)
    ss._warm_ollama_bg()
    assert popen == [] and spy[0][0] == "mine:7b" and spy[0][2] == "session-start.warmup"


def test_session_start_warmup_still_runs_when_the_guard_allows(monkeypatch):
    ss = _load_hook("session-start.py")
    popen = []
    monkeypatch.setattr(ss.subprocess, "Popen", lambda cmd, **k: popen.append(cmd))
    monkeypatch.setattr(ss, "_detached_spawn_disabled", lambda: False)
    monkeypatch.setattr(ss, "_EDIT_WARMED_MODEL", None)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP", "on")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_WARMUP_MODEL", "mine:7b")
    monkeypatch.setenv("OLLAMA_BASE_URL", SMALL)
    Fake(monkeypatch, {"11434": []}, {"mine:7b": 4 * GB})
    ss._warm_ollama_bg()
    assert len(popen) == 1 and f"{SMALL}/api/generate" in popen[0]


# -- the agent_loop overflow swap -------------------------------------------------------------------------------

def _loop_with_pack_spy(monkeypatch, tmp_path, *, swap_to):
    from llm_router import pack_overflow
    from llm_router.hooks import agent_loop
    kwargs: dict = {}
    real = pack_overflow.PayloadPack

    class _Pack(real):
        def __init__(self, payload, **kw):
            kwargs.update(kw)
            super().__init__(payload, **kw)

        def trim_to(self, window=None):
            if self.allow_model_swap and swap_to:
                self.model = swap_to
            return self.payload
    monkeypatch.setattr(pack_overflow, "PayloadPack", _Pack)
    return agent_loop, kwargs


def test_agent_loop_overflow_swap_is_disabled_when_the_local_model_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", TARGET)
    monkeypatch.setenv("LLM_ROUTER_MEM_GUARD", "off")
    agent_loop, kwargs = _loop_with_pack_spy(monkeypatch, tmp_path, swap_to="bigger:70b")
    fake = Fake(monkeypatch, {"11434": []}, {TARGET: 5 * GB})
    seen_models = []
    inner = fake._urlopen

    def _urlopen(req, *a, **k):
        url = getattr(req, "full_url", req)
        if url.endswith("/api/chat"):
            seen_models.append(json.loads(req.data)["model"])
            return _R({"message": {"content": json.dumps({"tool": "finish", "arguments": {"answer": "x"}}),
                                   "tool_calls": []}})
        return inner(req, *a, **k)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    from llm_router import local_privacy
    monkeypatch.setattr(local_privacy, "open_url", lambda req, *, base_url, timeout: _urlopen(req))
    agent_loop.run_agent_loop(prompt="hi", model=TARGET, project_root=tmp_path)
    assert kwargs["allow_model_swap"] is False
    assert seen_models and set(seen_models) == {TARGET}


def test_agent_loop_overflow_swap_to_a_model_that_does_not_fit_is_refused(monkeypatch, tmp_path):
    from llm_router.toolkit.adapters.ollama import last_refusal
    agent_loop, kwargs = _loop_with_pack_spy(monkeypatch, tmp_path, swap_to="bigger:70b")
    fake = Fake(monkeypatch, {"11434": [row(TARGET, 10)]}, {TARGET: 10 * GB, "bigger:70b": 44 * GB})
    inner = fake._urlopen
    chats = []

    def _urlopen(req, *a, **k):
        url = getattr(req, "full_url", req)
        if url.endswith("/api/chat"):
            chats.append(url)
            return _R({"message": {"content": json.dumps({"tool": "finish", "arguments": {"answer": "x"}}),
                                   "tool_calls": []}})
        return inner(req, *a, **k)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    from llm_router import local_privacy
    monkeypatch.setattr(local_privacy, "open_url", lambda req, *, base_url, timeout: _urlopen(req))
    assert agent_loop.run_agent_loop(prompt="hi", model=TARGET, project_root=tmp_path) is None
    assert kwargs["allow_model_swap"] is True              # LLM_ROUTER_LOCAL_MODEL unset: a swap is still allowed...
    assert chats == []                                     # ...but it is a load, so it went through the guard
    assert "bigger:70b" in str(last_refusal())


# -- review items -----------------------------------------------------------------------------------------------

def test_two_models_checked_back_to_back_are_summed(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:7b", 10)]}, {"one:20b": 20 * GB, "two:20b": 20 * GB})
    host(monkeypatch, 48.0, 40.0)
    oa.ensure_fits("one:20b", SMALL, "t")                 # 10 + ~18.6 <= 38.4
    with pytest.raises(oa.MemoryBudgetExceeded):          # same /api/ps snapshot, but one:20b is loading: 10 + 18.6 + 18.6
        oa.ensure_fits("two:20b", SMALL, "t")


def test_reservation_ends_when_the_model_is_resident_or_expires(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": []}, {"one:20b": 21 * GB, "two:20b": 21 * GB})
    oa.ensure_fits("one:20b", SMALL, "t")
    assert ("one:20b", SMALL) in oa._RESERVED
    fake.ps["11434"] = [row("one:20b", 21)]               # now real: counted once, via /api/ps
    oa._PS_CACHE.clear()
    with pytest.raises(oa.MemoryBudgetExceeded):          # 19.6 + 19.6 = 39.1 > 38.4: not double counted (would be 57)
        oa.ensure_fits("two:20b", SMALL, "t")
    assert ("one:20b", SMALL) not in oa._RESERVED
    oa._RESERVED[("ghost:9b", SMALL)] = (0.0, 40 * GB)    # expired entry is pruned and ignored
    oa._PS_CACHE.clear()
    oa._prune(oa.time.monotonic())
    assert ("ghost:9b", SMALL) not in oa._RESERVED


def test_evict_polls_until_the_unload_lands_and_never_caches_a_refusal_after_it(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_MEM_ON_PRESSURE", "evict")
    fake = Fake(monkeypatch, {"11434": [row("big:30b", 30)]}, {TARGET: 25 * GB})
    state = {"n": 0}
    real = fake._urlopen

    def lagging(req, *a, **k):
        url = getattr(req, "full_url", req)
        if url.endswith("/api/generate"):                 # accepted, but the model leaves /api/ps only 3 polls later
            fake.unloaded.append(("11434", "big:30b"))
            return _R({"done": True})
        if url.endswith("/api/ps"):
            state["n"] += 1
            if state["n"] > 5:
                fake.ps["11434"] = []
        return real(req, *a, **k)
    monkeypatch.setattr(urllib.request, "urlopen", lagging)
    oa.ensure_fits(TARGET, SMALL, "t")                    # allowed after polling
    assert fake.unloaded == [("11434", "big:30b")]


def test_refusal_after_a_successful_unload_is_not_cached(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setenv("LLM_ROUTER_MEM_ON_PRESSURE", "evict")
    fake = Fake(monkeypatch, {"11434": [row("big:30b", 30), row("big2:30b", 5)]}, {TARGET: 40 * GB})
    with pytest.raises(oa.MemoryBudgetExceeded):          # 40 GB cannot fit even after the unload
        oa.ensure_fits(TARGET, SMALL, "t")
    assert fake.unloaded and not any(v[1] is not None for v in oa._PS_CACHE.values())


def test_cached_refusal_raises_a_fresh_exception(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {TARGET: 25 * GB})
    errs = []
    for _ in range(2):
        with pytest.raises(oa.MemoryBudgetExceeded) as ei:
            oa.ensure_fits(TARGET, SMALL, "t")
        errs.append(ei.value)
    assert errs[0] is not errs[1] and str(errs[0]) == str(errs[1])


def test_non_loopback_server_skips_the_budget_without_a_failopen_row(monkeypatch):
    from llm_router import failopen
    from llm_router.toolkit.adapters import ollama as oa
    failopen.clear()
    remote = "http://192.168.1.50:11434"
    fake = Fake(monkeypatch, {"11434": [row("a:30b", 40)]}, {TARGET: 45 * GB})
    oa.ensure_fits(TARGET, remote, "t")
    assert not any(r["c"].startswith("CHZ-FO-MEM-GUARD") and "SIZE" in r["c"] for r in _failopen_rows())
    assert not any(u.endswith("/api/tags") for u in fake.calls)


def test_calibrate_completer_is_guarded(spy):
    from llm_router.resolver.calibrate import OllamaCompleter
    posted = []
    reply = OllamaCompleter(SMALL, lambda *a: posted.append(a)).chat("ollama/m:7b", [{"role": "user", "content": "x"}])
    assert reply.error and posted == [] and spy[0][2] == "resolver.calibrate.chat"
