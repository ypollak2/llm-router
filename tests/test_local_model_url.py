"""LLM_ROUTER_LOCAL_MODEL_URL: the ONE big local model is served by its own Ollama.

Owner decision 2026-11-09: qwen3.6 (26 GB) lives on a separate server (max 1 loaded);
the default server keeps the SMALL models (classifier, decision model, embeddings).
Sending a small model to the big server would evict the big one, so only the MAIN
model's requests move. Unset = today's behaviour exactly.
"""
from __future__ import annotations

import json
import types

import pytest

pytestmark = pytest.mark.usefixtures("open_url_via_urlopen")

MAIN = "http://127.0.0.1:11435"
SMALL = "http://127.0.0.1:11434"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", SMALL)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_MODEL", "llmr-classifier")
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "nimble:9b")


class _R:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._p).encode()
    def __iter__(self):
        yield (json.dumps({"message": {"content": "hello"}, "done": True}) + "\n").encode()


def _recorder(monkeypatch, urls):
    import urllib.request

    def _urlopen(req, *a, **k):
        url = getattr(req, "full_url", req)
        urls.append(url)
        if url.endswith("/api/tags"):
            return _R({"models": [{"name": "qwen3.6:35b-a3b-coding"}]})
        if url.endswith("/api/chat"):
            return _R({"message": {"content": json.dumps(
                {"tool": "finish", "arguments": {"answer": "done"}}), "tool_calls": []}})
        return _R({})
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


# ── resolution ────────────────────────────────────────────────────────────────

def test_unset_is_todays_behaviour(monkeypatch):
    from llm_router.toolkit.adapters.ollama import get_local_model_url, url_for_model
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL", raising=False)
    assert get_local_model_url() is None
    assert url_for_model("qwen3.6:35b-a3b-coding") == SMALL
    assert url_for_model("nimble:9b") == SMALL


def test_set_moves_main_model_only(monkeypatch):
    from llm_router.toolkit.adapters.ollama import url_for_model
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    assert url_for_model("qwen3.6:35b-a3b-coding") == MAIN
    assert url_for_model("ollama/qwen3.6:35b-a3b-coding") == MAIN
    # small models must not move: that would evict the big model
    assert url_for_model("llmr-classifier") == SMALL
    assert url_for_model("nimble:9b") == SMALL


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "http://169.254.169.254", "nonsense"])
def test_invalid_value_is_ignored(monkeypatch, bad):
    from llm_router.toolkit.adapters.ollama import get_local_model_url, url_for_model
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", bad)
    assert get_local_model_url() is None
    assert url_for_model("qwen3.6:35b-a3b-coding") == SMALL


# ── agent loop ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("local_url,expected", [(MAIN, MAIN), ("", SMALL)])
def test_agent_loop_hits_the_right_server(monkeypatch, tmp_path, local_url, expected):
    from llm_router.hooks import agent_loop
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", local_url)
    urls: list[str] = []
    _recorder(monkeypatch, urls)
    out = agent_loop.run_agent_loop(prompt="hi", model="qwen3.6:35b-a3b-coding", project_root=tmp_path)
    assert urls, "no request was made"
    chats = [u for u in urls if u.endswith("/api/chat")]
    assert chats and all(u.startswith(expected) for u in chats), urls
    other = SMALL if expected == MAIN else MAIN
    assert not any(u.startswith(other) for u in urls), urls


# ── direct executor (hook draft chain / zero-claude edit) ─────────────────────

def test_call_ollama_and_listing_follow_the_main_server(monkeypatch):
    from llm_router.hooks import direct_executor as de
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    urls: list[str] = []
    _recorder(monkeypatch, urls)
    text, _ = de.call_ollama("hi", "qwen3.6:35b-a3b-coding", timeout=5)
    assert text == "hello"
    assert de.available_ollama_models() == {"qwen3.6:35b-a3b-coding"}
    assert de.ollama_is_alive()
    assert urls and all(u.startswith(MAIN) for u in urls), urls


def test_call_ollama_small_model_stays_on_default(monkeypatch):
    from llm_router.hooks import direct_executor as de
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    urls: list[str] = []
    _recorder(monkeypatch, urls)
    de.call_ollama("hi", "llmr-classifier", timeout=5)
    assert urls and all(u.startswith(SMALL) for u in urls), urls


def test_listing_unset_uses_default_server(monkeypatch):
    from llm_router.hooks import direct_executor as de
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL", raising=False)
    urls: list[str] = []
    _recorder(monkeypatch, urls)
    de.available_ollama_models()
    assert urls and all(u.startswith(SMALL) for u in urls)


def test_agentic_registry_lists_the_server_it_will_call(monkeypatch):
    from llm_router import agentic_registry
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    urls: list[str] = []
    _recorder(monkeypatch, urls)
    assert agentic_registry.list_installed_models() == ["qwen3.6:35b-a3b-coding"]
    assert urls == [f"{MAIN}/api/tags"]


def test_model_discovery_probes_the_main_server(monkeypatch):
    from llm_router import model_discovery
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    urls: list[str] = []
    _recorder(monkeypatch, urls)
    assert model_discovery.probe_ollama() == ["qwen3.6:35b-a3b-coding"]
    assert urls == [f"{MAIN}/api/tags"]


# ── MCP llm() / LiteLLM path ──────────────────────────────────────────────────

def _fake_litellm(captured):
    async def _acompletion(**kwargs):
        captured.clear()
        captured.update(kwargs)
        usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0)
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    return _acompletion


@pytest.mark.asyncio
async def test_llm_ollama_answer_uses_main_server(monkeypatch):
    import litellm

    from llm_router.providers import call_llm
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    cap: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(cap))
    await call_llm("ollama/qwen3.6:35b-a3b-coding", [{"role": "user", "content": "hi"}])
    assert cap.get("api_base") == MAIN


@pytest.mark.asyncio
async def test_llm_small_model_and_unset_do_not_get_api_base(monkeypatch):
    import litellm

    from llm_router.providers import call_llm
    cap: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(cap))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    await call_llm("ollama/nimble:9b", [{"role": "user", "content": "hi"}])
    assert cap.get("api_base") in (None, SMALL)
    assert cap.get("api_base") != MAIN
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL")
    await call_llm("ollama/qwen3.6:35b-a3b-coding", [{"role": "user", "content": "hi"}])
    assert "api_base" not in cap


# ── single-model limit: LLM_ROUTER_LOCAL_MODEL + resident guard ───────────────

ALLOWED = "qwen3.6:35b-a3b-coding"


def _ps_server(monkeypatch, urls, ps_by_host, chat_ok=True):
    """ps_by_host: {"11435": [rows], "11434": [rows]}; a missing key = server unreachable."""
    import urllib.request

    def _urlopen(req, *a, **k):
        url = getattr(req, "full_url", req)
        urls.append(url)
        port = url.split(":")[2].split("/")[0]
        if url.endswith("/api/ps"):
            if port not in ps_by_host:
                raise OSError("connection refused")
            return _R({"models": ps_by_host[port]})
        if url.endswith("/api/tags"):
            return _R({"models": [{"name": ALLOWED}]})
        return _R({"message": {"content": json.dumps({"tool": "finish", "arguments": {"answer": "done"}}),
                               "tool_calls": []}})
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def test_model_override_applied(monkeypatch):
    from llm_router.toolkit.adapters.ollama import resolve_main_model
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    assert resolve_main_model("qwen3.5:latest") == ALLOWED
    assert resolve_main_model("ollama/qwen3.5:latest") == f"ollama/{ALLOWED}"
    assert resolve_main_model(None) == ALLOWED
    assert resolve_main_model("nimble:9b") == ALLOWED               # strictly one model: nothing is exempt
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL")
    assert resolve_main_model("qwen3.5:latest") == "qwen3.5:latest"


def test_agent_loop_uses_only_the_allowed_model(monkeypatch, tmp_path):
    from llm_router.hooks import agent_loop
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    seen = []
    import urllib.request

    def _urlopen(req, *a, **k):
        if req.full_url.endswith("/api/chat"):
            seen.append(json.loads(req.data)["model"])
        return _R({"message": {"content": json.dumps({"tool": "finish", "arguments": {"answer": "x"}}),
                               "tool_calls": []}}) if req.full_url.endswith("/api/chat") else _R({"models": []})
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    agent_loop.run_agent_loop(prompt="hi", model="some-other:7b", project_root=tmp_path)
    assert seen and set(seen) == {ALLOWED}


def test_other_big_model_resident_refuses_without_chat(monkeypatch, tmp_path):
    from llm_router.hooks import agent_loop
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    urls: list[str] = []
    _ps_server(monkeypatch, urls, {"11435": [{"name": ALLOWED, "size_vram": 26_100_000_000}],
                                   "11434": [{"name": "qwen3.5:27b", "size_vram": 20_000_000_000}]})
    assert agent_loop.run_agent_loop(prompt="hi", model=ALLOWED, project_root=tmp_path) is None
    assert not any(u.endswith("/api/chat") for u in urls), urls
    from llm_router.toolkit.adapters.ollama import last_refusal
    msg = str(last_refusal())
    assert "qwen3.5:27b" in msg and "20.0 GB" in msg


def test_call_ollama_refuses_and_sends_no_chat(monkeypatch):
    from llm_router.hooks import direct_executor as de
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    urls: list[str] = []
    _ps_server(monkeypatch, urls, {"11434": [{"name": "gemma:27b", "size_vram": 18_000_000_000}]})
    assert de.call_ollama("hi", ALLOWED, timeout=5) == (None, {})
    assert not any(u.endswith("/api/chat") for u in urls)


@pytest.mark.asyncio
async def test_llm_path_raises_conflict_and_never_calls_litellm(monkeypatch):
    import litellm

    from llm_router.providers import call_llm
    from llm_router.toolkit.adapters.ollama import LocalModelConflict
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    urls: list[str] = []
    _ps_server(monkeypatch, urls, {"11434": [{"name": "gemma:27b", "size_vram": 18_000_000_000}]})
    cap: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _fake_litellm(cap))
    with pytest.raises(LocalModelConflict, match="gemma:27b"):
        await call_llm("ollama/whatever:7b", [{"role": "user", "content": "hi"}])
    assert cap == {}


def test_small_resident_model_is_allowed(monkeypatch, tmp_path):
    from llm_router.toolkit.adapters.ollama import check_resident
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    urls: list[str] = []
    _ps_server(monkeypatch, urls, {"11434": [{"name": "nomic-embed-text:latest", "size_vram": 600_000_000},
                                             {"name": ALLOWED, "size_vram": 26_000_000_000}]})
    check_resident(ALLOWED)   # embedding model is under the 2 GB default: does not raise


def test_ps_unreachable_does_not_block(monkeypatch):
    from llm_router.toolkit.adapters.ollama import check_resident
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    _ps_server(monkeypatch, [], {})
    check_resident(ALLOWED)


def test_threshold_is_configurable(monkeypatch):
    from llm_router.toolkit.adapters.ollama import LocalModelConflict, check_resident
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    _ps_server(monkeypatch, [], {"11434": [{"name": "nomic-embed-text", "size_vram": 1_500_000_000}]})
    check_resident(ALLOWED)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MAX_OTHER_VRAM_GB", "1")
    with pytest.raises(LocalModelConflict):
        check_resident(ALLOWED)


def test_unset_means_no_ps_read_and_no_override(monkeypatch):
    from llm_router.toolkit.adapters.ollama import check_resident
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL", raising=False)
    urls: list[str] = []
    _ps_server(monkeypatch, urls, {"11434": [{"name": "gemma:27b", "size_vram": 18_000_000_000}]})
    check_resident("anything")
    assert urls == []


def test_discovery_returns_only_the_allowed_model(monkeypatch):
    from llm_router import model_discovery
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "other:7b")
    assert model_discovery.available_ollama_models() == [ALLOWED]
    assert model_discovery.available_ollama_models_nowait(refresh=False) == [ALLOWED]
    assert model_discovery.user_local_model_setting() == ALLOWED


def test_nimble_resident_is_refused_at_default_threshold(monkeypatch):
    from llm_router.toolkit.adapters.ollama import LocalModelConflict, check_resident
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    _ps_server(monkeypatch, [], {"11434": [{"name": "nimble:9b", "size_vram": 8_700_000_000}]})
    with pytest.raises(LocalModelConflict, match="nimble:9b"):
        check_resident(ALLOWED)


def test_classifier_paths_use_the_one_model(monkeypatch):
    from llm_router import door_verdict, local_classifier as lc
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "systemone")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_CLASSIFIER", raising=False)
    # unset: today's behaviour
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL", raising=False)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL_URL", MAIN)
    assert lc.backend() == "systemone"
    assert lc._model() == "nimble:9b"
    assert lc._base_url() == SMALL
    assert door_verdict._decision_model_configured() is True
    # set: the same model on the same server, chat backend, decision model off
    monkeypatch.setenv("LLM_ROUTER_LOCAL_MODEL", ALLOWED)
    assert lc.backend() == "chat"
    assert lc._model() == ALLOWED
    assert lc._base_url() == MAIN
    assert door_verdict._decision_model_configured() is False
    assert door_verdict.mode() == "off"          # no explicit opt-in: fails open to the non-model path
