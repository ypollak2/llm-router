"""LC-12h: every loopback Ollama call bypasses HTTP proxies; the secret door keeps a prompt with a secret local.

* Socket-level trace, HTTP_PROXY / ALL_PROXY set and NO_PROXY unset, aiohttp and httpx transports: the text of
  (a) an ordinary local route, (b) a secret-detected local-only route and (c) the stream door reaches the loopback
  Ollama only; the fake proxy sees no request at all (no /api/generate, no /api/show, no /api/tags, no embeddings).
* (b): the semantic cache (check and store) and the judge queue are skipped, there is no emergency fallback, a
  non-loopback Ollama base or a chain with no local model is refused, and the PACK_OVERFLOW switch may not leave
  the local set.
* A non-loopback Ollama base on the ordinary path is unchanged (LiteLLM's shared client, the user's proxy env); an
  explicit NO_PROXY is respected there.
* The overflow / endpoint refusal adds no chain error and is not a fallback (#715 review nit).

Every server is a loopback fake in this process (tests.test_lc12g.FakeServer). The only model is the fake; a
"non-loopback" base is either refused before any request or simulated by patching ``local_privacy.is_loopback_base``
so the request still lands on a loopback fake. No real Ollama, no real model, no non-loopback host.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.request

import litellm
import pytest

import llm_router.config as config_module
from llm_router import classifier, local_privacy, providers, router
from llm_router import eligibility_routing as er
from llm_router.types import LLMResponse, TaskType
from tests.test_lc12 import passed, world  # noqa: F401
from tests.test_lc12c import GOOD, LOCAL, e2e  # noqa: F401
from tests.test_lc12g import PROXY_VARS, REAL_CALL_TEXT, SECRET, FakeServer, _SwitchingPack
from tests.test_lc8_routing import BARE

#: A GitHub-token shape (engine.secret_names: "github_token"); synthetic.
TOKEN = "ghp_" + "Ab3" * 12
CLOUD = "openai/gpt-4o"
CTX_LEN = 32768
TRANSPORTS = pytest.mark.parametrize("aiohttp", [True, False], ids=["aiohttp-transport", "httpx-transport"])


class _Ndjson(list):
    """An answer body sent as newline-delimited JSON (Ollama's streaming format)."""


class _Server(FakeServer):
    """FakeServer whose answer may be an :class:`_Ndjson` body (a streamed /api/generate)."""

    def __init__(self, answer):
        super().__init__(answer)
        handler = self.srv.RequestHandlerClass

        def _any(h):
            n = int(h.headers.get("Content-Length") or 0)
            body = h.rfile.read(n) if n else b""
            self.seen.append((h.command, h.path, body))
            status, payload = answer(h.command, h.path, body)
            data = (b"".join(json.dumps(x).encode() + b"\n" for x in payload) if isinstance(payload, _Ndjson)
                    else json.dumps(payload).encode())
            h.send_response(status)
            h.send_header("Content-Type", "application/x-ndjson" if isinstance(payload, _Ndjson)
                          else "application/json")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
        handler.do_GET = handler.do_POST = _any


def _ollama_answer(who):
    def answer(method, path, body):
        if path.endswith("/api/generate") and json.loads(body or b"{}").get("stream"):
            return 200, _Ndjson([
                {"model": "m", "created_at": "2026-10-25T00:00:00Z", "response": f"{GOOD} ({who})", "done": False},
                {"model": "m", "created_at": "2026-10-25T00:00:01Z", "response": "", "done": True,
                 "prompt_eval_count": 3, "eval_count": 2}])
        if path.endswith("/api/generate"):
            return 200, {"model": "m", "created_at": "2026-10-25T00:00:00Z", "response": f"{GOOD} ({who})",
                         "done": True, "prompt_eval_count": 3, "eval_count": 2}
        if path.endswith("/api/chat"):
            return 200, {"model": "m", "created_at": "2026-10-25T00:00:00Z", "done": True,
                         "message": {"role": "assistant", "content": f"{GOOD} ({who})"},
                         "prompt_eval_count": 3, "eval_count": 2}
        if path.endswith("/api/show"):
            return 200, {"model_info": {"llama.context_length": CTX_LEN}, "template": "{{ .Prompt }}"}
        if path.endswith("/api/tags"):
            return 200, {"models": []}
        if path.endswith("/api/embeddings"):
            return 200, {"embedding": [0.1, 0.2, 0.3]}
        return 404, {"error": "not here"}
    return answer


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No proxy env from the host; LiteLLM's model map, our model-info memo, urllib's global opener, os.environ
    and the config singleton restored after each test."""
    for k in list(os.environ):
        if k.upper() in PROXY_VARS:
            monkeypatch.delenv(k)
    env, cfg, cost_keys = dict(os.environ), config_module._config, set(litellm.model_cost)
    local_privacy._info_attempted.clear()
    urllib.request._opener = None
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", None)
    yield
    for k in set(litellm.model_cost) - cost_keys:
        del litellm.model_cost[k]
    local_privacy._info_attempted.clear()
    os.environ.clear()
    os.environ.update(env)
    config_module._config = cfg
    urllib.request._opener = None


@pytest.fixture
def net(monkeypatch):
    """A fake proxy, a fake Ollama and a fake embedding host on 127.0.0.1; HTTP_PROXY and ALL_PROXY point at the
    proxy, NO_PROXY unset. Every socket send carrying SECRET is recorded with its peer port."""
    proxy, ollama, embed = (_Server(_ollama_answer(w)) for w in ("proxy", "ollama", "embed"))
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("ALL_PROXY", proxy.url)
    monkeypatch.setenv("OLLAMA_API_BASE", ollama.url)
    monkeypatch.setenv("OLLAMA_BASE_URL", embed.url)          # the semantic cache's embedding endpoint
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    config_module._config = None
    peers: list[int] = []
    real_send, real_sendall = socket.socket.send, socket.socket.sendall

    def _watch(sock, data):
        if SECRET.encode() in bytes(data):
            peers.append(sock.getpeername()[1])

    monkeypatch.setattr(socket.socket, "send", lambda self, d, *a: (_watch(self, d), real_send(self, d, *a))[1])
    monkeypatch.setattr(socket.socket, "sendall", lambda self, d, *a: (_watch(self, d), real_sendall(self, d, *a))[1])
    n = type("Net", (), {})()
    n.proxy, n.ollama, n.embed, n.peers = proxy, ollama, embed, peers
    n.port = lambda server: int(server.url.rsplit(":", 1)[1])
    yield n
    for s in (proxy, ollama, embed):
        s.close()


def _carried(server):
    return [p for _m, p, b in server.seen if SECRET.encode() in b]


def _drain():
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))


def _judge_queue() -> str:
    from llm_router import paths
    q = paths.state_path("judge_queue.jsonl")
    return q.read_text() if q.exists() else ""


@pytest.fixture
def routed(e2e, net, monkeypatch):
    """The real route_and_call -> _call_text -> providers.call_llm -> LiteLLM for a local model. A cloud model is
    recorded and refused (no network): it would be a door failure, and the tests assert it is never tried."""
    tried: list[str] = []

    async def call_text(model, *a, **k):
        tried.append(model)
        if not model.startswith("ollama/"):
            raise RuntimeError(f"cloud model {model} must not be called in this test")
        return await REAL_CALL_TEXT(model, *a, **k)
    monkeypatch.setattr(router, "_call_text", call_text)
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")     # conftest sets 0; every call is sampled
    e2e.state["tried"] = tried
    e2e.net = net
    return e2e


def _route(prompt, **k):
    return asyncio.run(router.route_and_call(TaskType.GENERATE, prompt, **k))


def _no_proxy_traffic(n):
    assert n.proxy.seen == []                                   # nothing at all went to the proxy
    assert set(n.peers) <= {n.port(n.ollama), n.port(n.embed)}  # socket level: the text only to loopback fakes


# ----------------------------------------------------------------------------- (a) ordinary local route
@TRANSPORTS
def test_a_ordinary_local_route_sends_the_text_only_to_the_loopback_daemon(routed, monkeypatch, aiohttp):
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    n = routed.net
    routed.state["chain"] = [LOCAL]
    resp = _route(f"write a line about {SECRET}")
    _drain()
    assert routed.state["tried"] == [LOCAL] and "(ollama)" in resp.content
    _no_proxy_traffic(n)
    assert _carried(n.ollama) == ["/api/generate"]
    assert n.port(n.ollama) in n.peers
    # LiteLLM's /api/show lookup did not run: our direct one (model name only, loopback) filled its model map
    assert [(m, p) for m, p, _b in n.ollama.seen if p == "/api/show"] == [("POST", "/api/show")]
    assert litellm.model_cost[f"ollama/{BARE}"]["max_input_tokens"] == CTX_LEN
    # control: off the private paths the semantic cache embeds (direct, to the loopback embed host) and the judge
    # queue gets the entry
    assert any("embeddings" in p for _m, p, _b in n.embed.seen)
    assert SECRET in _judge_queue()


# ----------------------------------------------------------------------------- (b) the secret door
@TRANSPORTS
def test_b_secret_route_is_local_only_without_cache_or_judge(routed, monkeypatch, aiohttp):
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    n = routed.net
    routed.state["chain"] = [CLOUD, LOCAL]
    resp = _route(f"debug this, my token is {TOKEN} ({SECRET})")
    _drain()
    assert routed.state["tried"] == [LOCAL] and "(ollama)" in resp.content   # the cloud entry was cut, not tried
    _no_proxy_traffic(n)
    assert _carried(n.ollama) == ["/api/generate"]
    assert [p for _m, p, _b in n.embed.seen if "embeddings" in p] == []   # semantic cache: no check, no store
    assert _carried(n.embed) == []
    assert SECRET not in _judge_queue() and TOKEN not in _judge_queue()


def test_b_secret_in_a_message_or_system_prompt_also_routes_local(routed):
    routed.state["chain"] = [CLOUD, LOCAL]
    _route(f"hello {SECRET}", system_prompt=f"use {TOKEN}")
    _drain()
    assert routed.state["tried"] == [LOCAL]
    routed.state["tried"].clear()
    _route(f"hello {SECRET}", messages=[{"role": "user", "content": [{"type": "text", "text": TOKEN}]}])
    _drain()
    assert routed.state["tried"] == [LOCAL]


def test_b_non_loopback_ollama_base_is_refused_before_any_request(routed, monkeypatch):
    n = routed.net
    monkeypatch.setenv("OLLAMA_API_BASE", "http://gpu-box.invalid:11434")
    routed.state["chain"] = [CLOUD, LOCAL]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused, match="not loopback"):
        _route(f"my token is {TOKEN} ({SECRET})")
    assert routed.state["tried"] == []
    assert n.peers == [] and n.proxy.seen == [] and n.ollama.seen == []


def test_b_no_local_model_in_the_chain_is_refused(routed):
    routed.state["chain"] = [CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused, match="no local model"):
        _route(f"my token is {TOKEN}")
    assert routed.state["tried"] == []


def test_b_local_failure_does_not_fall_back_to_the_budget_chain(routed, monkeypatch):
    routed.state["chain"] = [CLOUD, LOCAL]
    built: list = []
    real_build = router._build_and_filter_chain

    async def build(*a, **k):
        built.append(a)
        return await real_build(*a, **k)
    monkeypatch.setattr(router, "_build_and_filter_chain", build)
    tried = routed.state["tried"]

    async def call_text(model, *a, **k):
        tried.append(model)
        raise RuntimeError("local model down")
    monkeypatch.setattr(router, "_call_text", call_text)
    with pytest.raises(RuntimeError, match="All models failed"):
        _route(f"my token is {TOKEN}")
    _drain()
    assert tried == [LOCAL]
    assert len(built) == 1                                      # the primary build only; no emergency BUDGET build


def test_b_ordinary_prompt_keeps_the_full_chain(routed):
    """The door is scoped: without a secret the cloud entry stays (and is tried first here)."""
    routed.state["chain"] = [CLOUD, LOCAL]
    _route(f"write a line about {SECRET}")
    _drain()
    assert routed.state["tried"][0] == CLOUD


class _CloudSwitchPack(_SwitchingPack):
    def trim_to(self, window=None):
        out = super().trim_to(window)
        self.model = "ollama/bigger:cloud"
        return out


@pytest.mark.parametrize("pack, refused", [(_SwitchingPack, False), (_CloudSwitchPack, True)],
                         ids=["switch-to-local-allowed", "switch-to-cloud-refused"])
def test_b_overflow_switch_may_not_leave_the_local_set(monkeypatch, pack, refused):
    import llm_router.pack_overflow as po
    monkeypatch.setattr(po, "PayloadPack", pack)
    monkeypatch.setattr("llm_router.local_context_guard.check_overflow", lambda *a, **k: None)
    monkeypatch.setattr(local_privacy, "ensure_ollama_model_info", lambda *a, **k: None)
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    called: list[str] = []

    async def acompletion(**kw):
        called.append(kw["model"])
        raise RuntimeError("stop here")
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    token = local_privacy.set_secret_dispatch(True)
    try:
        with pytest.raises(er.OverflowSwitchRefused if refused else RuntimeError):
            asyncio.run(providers.call_llm("ollama/small:1b", [{"role": "user", "content": SECRET}]))
    finally:
        local_privacy.reset_secret_dispatch(token)
    assert called == ([] if refused else ["ollama/bigger:70b"])
    assert er.fail_closed_counts() in ({}, None)               # the text-job store is not the secret door's


def test_b_private_call_to_a_non_local_model_is_refused_in_providers(monkeypatch):
    """Defense in depth below the router: under the private marker a cloud model or a cloud-tagged Ollama model
    is refused before LiteLLM is called."""
    called: list[str] = []

    async def acompletion(**kw):
        called.append(kw["model"])
        raise RuntimeError("stop here")
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:9")
    token = local_privacy.set_secret_dispatch(True)
    try:
        for model in (CLOUD, "ollama/gpt-oss:120b-cloud"):
            with pytest.raises(local_privacy.PrivateEndpointRefused):
                asyncio.run(providers.call_llm(model, [{"role": "user", "content": SECRET}]))
        monkeypatch.setenv("OLLAMA_API_BASE", "http://gpu-box.invalid:11434")
        with pytest.raises(local_privacy.PrivateEndpointRefused):
            asyncio.run(providers.call_llm("ollama/small:1b", [{"role": "user", "content": SECRET}]))
    finally:
        local_privacy.reset_secret_dispatch(token)
    assert called == []


def test_policy_refusal_is_not_a_chain_error_or_a_fallback(routed, monkeypatch):
    """#715 review nit: the refusal stops the walk without a chain error, so fallback_occurred stays false."""
    routed.state["chain"] = [LOCAL]
    terminals: list[dict] = []
    monkeypatch.setattr(router, "_emit_quality_terminal", lambda **k: terminals.append(k))

    async def call_text(model, *a, **k):
        raise local_privacy.PrivateEndpointRefused("refused in test")
    monkeypatch.setattr(router, "_call_text", call_text)
    with pytest.raises(RuntimeError) as info:
        _route(f"my token is {TOKEN}")
    _drain()
    assert isinstance(info.value.__cause__, local_privacy.PrivateEndpointRefused)
    assert terminals and terminals[-1]["chain_errors"] == []


def test_classifier_does_not_send_a_secret_to_a_classifier_model(monkeypatch):
    calls: list[str] = []

    async def call_llm(model, *a, **k):
        calls.append(model)
        raise AssertionError("must not be called")
    monkeypatch.setattr(providers, "call_llm", call_llm)
    result = asyncio.run(classifier.classify_complexity(f"rotate {TOKEN} please"))
    assert calls == [] and "secret" in result.reasoning


def test_secret_scan_fails_closed(monkeypatch):
    from llm_router import engine

    def boom(text):
        raise ValueError("scan broke")
    monkeypatch.setattr(engine, "secret_names", boom)
    assert local_privacy.contains_secret("plain text") is True
    monkeypatch.undo()
    assert local_privacy.contains_secret("plain text", None, [{"role": "user", "content": "hi"}]) is False


# ----------------------------------------------------------------------------- (c) the stream door
def _stream(model, private=False):
    async def go():
        out = []
        async for ev in providers.call_llm_stream_events(model, [{"role": "user", "content": SECRET}],
                                                          private=private):
            out.append(ev)
        return out
    return asyncio.run(go())


@TRANSPORTS
@pytest.mark.parametrize("private", [False, True], ids=["ordinary", "private"])
def test_c_stream_door_sends_the_text_only_to_the_loopback_daemon(net, monkeypatch, aiohttp, private):
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    events = _stream("ollama/m", private=private)
    text = "".join(e["delta"]["text"] for e in events if e["type"] == "delta")
    assert "(ollama)" in text and events[-1]["type"] == "usage"
    _no_proxy_traffic(net)
    assert _carried(net.ollama) == ["/api/generate"]


def test_c_route_and_stream_secret_door_streams_from_the_local_model_only(net, monkeypatch):
    monkeypatch.setattr(router, "get_model_chain", lambda *a, **k: [CLOUD, "ollama/m"])
    monkeypatch.setattr(router, "_drop_failed_eligibility", lambda models, *a, **k: models)
    seen: list[tuple[str, bool]] = []
    real = providers.call_llm_stream_events

    def spy(model, messages, **k):
        seen.append((model, k.get("private")))
        if not model.startswith("ollama/"):
            raise AssertionError("cloud model must not be called")
        return real(model, messages, **k)
    monkeypatch.setattr(providers, "call_llm_stream_events", spy)

    async def go():
        return [ev async for ev in router.route_and_stream(TaskType.GENERATE, f"{SECRET} {TOKEN}")]
    events = asyncio.run(go())
    assert seen == [("ollama/m", True)]
    assert any(e["type"] == "output.delta" for e in events)
    _no_proxy_traffic(net)


# ----------------------------------------------------------------------------- metadata requests
def test_probe_ollama_goes_direct_for_a_loopback_daemon(net):
    assert config_module.probe_ollama(net.ollama.url) is True
    assert net.proxy.seen == [] and ("GET", "/api/tags", b"") in net.ollama.seen


def test_embedding_backends_go_direct_for_a_loopback_base(net):
    from llm_router import semantic_cache, semantic_classify
    assert semantic_cache._get_embedding(SECRET, net.embed.url) == [0.1, 0.2, 0.3]
    assert semantic_classify._embed_ollama(SECRET, net.embed.url) == [0.1, 0.2, 0.3]
    assert net.proxy.seen == [] and len(_carried(net.embed)) == 2


def test_model_info_entry_matches_litellms_own_and_is_fetched_once(net):
    local_privacy.ensure_ollama_model_info("ollama_chat/m")
    local_privacy.ensure_ollama_model_info("ollama/m")
    shows = [b for _m, p, b in net.ollama.seen if p == "/api/show"]
    assert [json.loads(b) for b in shows] == [{"name": "m"}]
    entry = litellm.model_cost["ollama/m"]
    assert entry["max_tokens"] == CTX_LEN and entry["supports_function_calling"] is False
    from litellm.llms.ollama.common_utils import OllamaModelInfo
    assert OllamaModelInfo().get_model_info("ollama/m") is None   # LiteLLM now skips its own /api/show
    assert net.proxy.seen == []


# ----------------------------------------------------------------------------- non-loopback base: unchanged
def _pretend_remote(monkeypatch):
    """A base the user configured on purpose (a LAN GPU box). Simulated: the predicate says "not loopback" while
    the request still lands on a loopback fake, so no non-loopback host is contacted."""
    monkeypatch.setattr(local_privacy, "is_loopback_base", lambda url: False)


@TRANSPORTS
def test_non_loopback_base_on_the_ordinary_path_keeps_the_proxy(net, monkeypatch, aiohttp):
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    _pretend_remote(monkeypatch)
    resp = asyncio.run(providers.call_llm("ollama/m", [{"role": "user", "content": SECRET}]))
    assert "(proxy)" in resp.content                            # as before LC-12h: the user's proxy env applies
    assert _carried(net.proxy) == [f"{net.ollama.url}/api/generate"] and _carried(net.ollama) == []
    assert "ollama/m" not in litellm.model_cost                 # no direct /api/show for it either


@TRANSPORTS
def test_explicit_no_proxy_is_respected_for_a_non_loopback_base(net, monkeypatch, aiohttp):
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    _pretend_remote(monkeypatch)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    resp = asyncio.run(providers.call_llm("ollama/m", [{"role": "user", "content": SECRET}]))
    assert "(ollama)" in resp.content
    assert _carried(net.proxy) == [] and _carried(net.ollama) == ["/api/generate"]


def test_urllib_side_non_loopback_keeps_urlopen_and_no_proxy(net, monkeypatch):
    from llm_router import semantic_cache
    _pretend_remote(monkeypatch)
    assert semantic_cache._get_embedding(SECRET, net.embed.url) == [0.1, 0.2, 0.3]
    assert _carried(net.proxy) == [f"{net.embed.url}/api/embeddings"]   # unchanged: urlopen honours the proxy
    net.proxy.seen.clear()
    urllib.request._opener = None
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    assert semantic_cache._get_embedding(SECRET, net.embed.url) == [0.1, 0.2, 0.3]
    assert net.proxy.seen == []                                 # and honours an explicit NO_PROXY


@TRANSPORTS
def test_ollama_chat_prefix_goes_direct_too(net, monkeypatch, aiohttp):
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    resp = asyncio.run(providers.call_llm("ollama_chat/m", [{"role": "user", "content": SECRET}]))
    assert "(ollama)" in resp.content
    assert net.proxy.seen == [] and _carried(net.ollama) == ["/api/chat"]


def test_loopback_with_explicit_no_proxy_still_direct(net, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    resp = asyncio.run(providers.call_llm("ollama/m", [{"role": "user", "content": SECRET}]))
    assert "(ollama)" in resp.content and net.proxy.seen == []


def test_ordinary_route_response_shape(monkeypatch):
    """The direct client changes the transport only: the LLMResponse of a loopback call is LiteLLM's as before."""
    async def acompletion(**kw):
        assert kw["client"].client._trust_env is False
        return litellm.ModelResponse(model="ollama/m", choices=[{"message": {"role": "assistant", "content": GOOD}}],
                                     usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(local_privacy, "ensure_ollama_model_info", lambda *a, **k: None)
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    resp = asyncio.run(providers.call_llm("ollama/m", [{"role": "user", "content": "hi"}]))
    assert isinstance(resp, LLMResponse) and resp.content == GOOD and resp.provider == "ollama"
