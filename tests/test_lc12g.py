"""LC-12g: local-only text jobs bypass HTTP proxies and never switch models on overflow; live-check success path;
#714 review nits.

* the real ``_live_tags`` success path (loopback ``/api/tags`` server, real ``http.client``) serves the passed model;
* LiteLLM sends a loopback Ollama request through ``HTTP_PROXY`` when ``NO_PROXY`` is unset (the finding);
  on the pass-only path ``providers.call_llm`` gives that call a direct client, so the text skips the proxy;
* with ``LLM_ROUTER_PACK_OVERFLOW`` the pass-only path refuses the switch to an unpassed model;
* a non-loopback ``OLLAMA_API_BASE`` has its own reason; an unreadable fail-closed store alarms; a tags row
  without a digest clears the cached one.

Every server is on 127.0.0.1 in this process; ``providers.call_llm`` or ``litellm.acompletion`` is mocked
wherever a test does not need the real one. No real Ollama, no non-loopback host, no model.
"""
# ruff: noqa: F811  (the `world` / `e2e` fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import builtins
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import litellm
import pytest

from llm_router import counter_registry, model_discovery, paths, providers
from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.local_check import display
from llm_router.types import LLMResponse
from tests.test_lc12 import FLAG, call_job, passed, world  # noqa: F401
from tests.test_lc12c import GOOD, LOCAL, e2e  # noqa: F401
from tests.test_lc8_routing import BARE, DIGEST, JOB

REAL_LIVE_TAGS = er._live_tags
REAL_CALL_TEXT = router._call_text
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
SECRET = "user-text-must-stay-local-7f3a"


def _drain():
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))


class FakeServer:
    """A loopback HTTP server that records every request and answers with ``answer(method, path, body)``."""

    def __init__(self, answer):
        self.seen: list[tuple[str, str, bytes]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.seen.append((self.command, self.path, body))
                status, payload = answer(self.command, self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _any

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _generate_answer(who):
    def answer(method, path, body):
        if path.endswith("/api/generate"):
            return 200, {"model": "m", "created_at": "2026-10-25T00:00:00Z", "response": f"{GOOD} ({who})",
                         "done": True, "prompt_eval_count": 3, "eval_count": 2}
        return 404, {"error": "not here"}
    return answer


@pytest.fixture
def servers(monkeypatch):
    """A fake proxy and a fake Ollama, both loopback; proxy env points at the proxy, NO_PROXY unset."""
    for k in list(os.environ):
        if k.upper() in PROXY_VARS:
            monkeypatch.delenv(k)
    proxy, ollama = FakeServer(_generate_answer("proxy")), FakeServer(_generate_answer("ollama"))
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("ALL_PROXY", proxy.url)
    monkeypatch.setenv("OLLAMA_API_BASE", ollama.url)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", None)  # no client from an earlier test
    yield proxy, ollama
    proxy.close()
    ollama.close()


def _carried_text(server):
    return [p for _m, p, body in server.seen if SECRET.encode() in body]


# ----------------------------------------------------------------------------- 1. the live-check success path
def test_real_live_tags_success_path_serves_the_passed_model(e2e, monkeypatch):
    """Real ``_live_tags`` + ``rt._fetch`` against a loopback ``/api/tags``; the provider layer is the only fake."""
    passed(e2e)
    rows = {"models": [{"name": "other:1b", "model": "other:1b", "digest": "sha256:" + "c" * 16},
                       {"name": BARE, "model": BARE, "digest": DIGEST}]}
    tags = FakeServer(lambda m, p, b: (200, rows) if (m, p) == ("GET", "/api/tags") else (404, {}))
    monkeypatch.setenv("OLLAMA_API_BASE", tags.url)
    monkeypatch.setattr(er, "_live_tags", REAL_LIVE_TAGS)
    monkeypatch.setattr(router, "_call_text", REAL_CALL_TEXT)
    served: list[str] = []

    async def call_llm(model, messages, **k):
        served.append(model)
        return LLMResponse(content=GOOD, model=model, input_tokens=5, output_tokens=5, cost_usd=0.0,
                           latency_ms=1.0, provider="ollama")
    monkeypatch.setattr(providers, "call_llm", call_llm)
    try:
        out = call_job()
        _drain()
    finally:
        tags.close()
    assert tags.seen == [("GET", "/api/tags", b"")]          # one live GET, to the loopback daemon
    assert served == [LOCAL] and "status=accepted" in out and GOOD in out
    assert er.fail_closed_counts() == {}


# ----------------------------------------------------------------------------- 2. HTTP proxy env
def test_finding_proxy_env_has_no_loopback_bypass(servers):
    """The finding, from the libraries' own proxy selection: with NO_PROXY unset nothing exempts localhost."""
    proxy, _ = servers
    assert httpx._utils.get_environment_proxies() == {"http://": proxy.url, "all://": proxy.url}
    assert urllib.request.proxy_bypass("localhost") in (False, 0)  # what LiteLLM's aiohttp transport consults


@pytest.mark.parametrize("aiohttp", [True, False], ids=["aiohttp-transport", "httpx-transport"])
def test_finding_litellm_sends_loopback_ollama_text_through_the_proxy(servers, monkeypatch, aiohttp):
    """Without LC-12g's client, LiteLLM's /api/generate (with the prompt) goes to the proxy, on both transports."""
    proxy, ollama = servers
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", not aiohttp)
    r = asyncio.run(litellm.acompletion(model="ollama/m", messages=[{"role": "user", "content": SECRET}],
                                        api_base=ollama.url))
    assert "(proxy)" in r.choices[0].message.content
    assert _carried_text(proxy) == [f"{ollama.url}/api/generate"]
    assert _carried_text(ollama) == []


class _pass_only:
    """The state the router sets for a pass-only dispatch: llm_text_job's ContextVar and the router's own marker
    (providers read the marker, so the two modules apply the router's one predicate)."""

    def __enter__(self):
        self.t1, self.t2 = er.set_require_passed_first(), er.set_pass_only_dispatch(True)

    def __exit__(self, *a):
        er.reset_pass_only_dispatch(self.t2)
        er.reset_require_passed_first(self.t1)


def _call(model="ollama/m"):
    return asyncio.run(providers.call_llm(model, [{"role": "user", "content": SECRET}]))


def test_pass_only_call_goes_direct_to_ollama_not_the_proxy(servers):
    proxy, ollama = servers
    with _pass_only():
        resp = _call()
    assert "(ollama)" in resp.content
    assert _carried_text(ollama) == ["/api/generate"]
    assert _carried_text(proxy) == []                          # the text never reached the proxy
    assert not any(p.endswith("/api/generate") for _m, p, _b in proxy.seen)


def test_direct_client_is_per_call_and_closed(monkeypatch):
    """A fresh handler with trust_env=False and no redirects per loopback Ollama call, closed after it. LC-12h: on
    every path, not only the pass-only one; a non-Ollama model gets none, and is refused on the pass-only path."""
    from llm_router import local_privacy
    seen: list[dict] = []

    async def acompletion(**kw):
        seen.append(kw)
        raise RuntimeError("stop here")
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(local_privacy, "ensure_ollama_model_info", lambda *a, **k: None)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    with pytest.raises(RuntimeError):
        _call()                                                # ordinary path: direct too (LC-12h)
    with _pass_only():
        with pytest.raises(RuntimeError):
            _call()
        with pytest.raises(local_privacy.PrivateEndpointRefused):
            _call("anthropic/claude-sonnet-5")                 # not Ollama: refused before any call
    with pytest.raises(RuntimeError):
        _call("anthropic/claude-sonnet-5")                     # not Ollama, ordinary path: no client
    assert len(seen) == 3
    for kw in seen[:2]:
        direct = kw["client"].client
        assert direct._trust_env is False and direct.follow_redirects is False and direct.is_closed
    assert seen[0]["client"] is not seen[1]["client"]
    assert "client" not in seen[2]


# ----------------------------------------------------------------------------- 3. PACK_OVERFLOW model switch
class _SwitchingPack:
    """Stands in for PayloadPack: the payload does not fit and a bigger validated model exists."""

    def __init__(self, payload, **k):
        self.payload, self.model, self.num_ctx, self.action = payload, k.get("model"), k.get("num_ctx"), "off"

    def trim_to(self, window=None):
        self.model, self.num_ctx, self.action = "ollama/bigger:70b", 131072, "bigger_window"
        return self.payload


@pytest.fixture
def switching(monkeypatch, world):
    import llm_router.pack_overflow as po
    monkeypatch.setattr(po, "PayloadPack", _SwitchingPack)
    monkeypatch.setattr("llm_router.local_context_guard.check_overflow", lambda *a, **k: None)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    called: list[str] = []

    async def acompletion(**kw):
        called.append(kw["model"])
        raise RuntimeError("stop after dispatch")
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return called


def test_pass_only_overflow_switch_is_refused_before_any_model_call(switching):
    with _pass_only():
        with pytest.raises(er.OverflowSwitchRefused, match="bigger:70b"):
            _call(LOCAL)
    assert switching == []
    assert er.fail_closed_counts() == {er.FC_OVERFLOW_SWITCH: 1}


def test_overflow_switch_outside_the_pass_only_path_is_unchanged(switching):
    with pytest.raises(RuntimeError, match="stop after dispatch"):
        _call(LOCAL)
    assert switching == ["ollama/bigger:70b"]
    assert er.fail_closed_counts() == {}


def test_text_job_hands_back_when_overflow_would_switch(e2e, switching, monkeypatch):
    """Through llm_text_job -> route_and_call -> the real _call_text / call_llm: a hand-back, no model call."""
    passed(e2e)
    monkeypatch.setattr(router, "_call_text", REAL_CALL_TEXT)
    out = call_job()
    _drain()
    assert switching == [] and "no cloud model was called" in out and "status=fallback, attempts=0" in out
    assert f"the input does not fit the passed local model for {JOB}" in out
    assert er.fail_closed_counts() == {er.FC_OVERFLOW_SWITCH: 1}
    from llm_router import text_job_ledger
    assert not paths.state_path(text_job_ledger.LOCAL_ATTEMPTS_FILENAME).exists()  # a refusal is not a local-model error row


# ----------------------------------------------------------------------------- 4. nits
def test_non_loopback_endpoint_has_its_own_reason_and_makes_no_request(world, monkeypatch):
    monkeypatch.setattr(er, "_passed_lane", lambda *a, **k: ({"grade": "passed"}, "prereg", None))
    monkeypatch.setenv("OLLAMA_API_BASE", "http://ollama.example.com:11434")

    def no_request():
        raise AssertionError("a non-loopback endpoint must not be asked")
    monkeypatch.setattr(er, "_live_tags", no_request)
    assert er.live_check_refusal(LOCAL, JOB, time.time()) == er.NOT_LOOPBACK
    assert er.NOT_LOOPBACK == "endpoint is not loopback (text would leave the machine)"


def test_unreadable_fail_closed_store_alarms(world, monkeypatch):
    store = paths.state_path(er.FC_STORE_NAME)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"c": er.FC_CUT_CHECK, "ts": time.time()}) + "\n")
    real_open = builtins.open

    def guarded(file, *a, **k):
        if str(file).endswith(er.FC_STORE_NAME):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", guarded)
    r = counter_registry.read_one("text_job_fail_closed")
    assert r.value is None and r.alarming is True and "unreadable (PermissionError)" in r.unknown_reason


def test_tags_row_without_a_digest_clears_the_cached_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    model_discovery._last_probe_meta.clear()
    model_discovery.remember_tags_rows([{"name": BARE, "digest": "abc"}])
    model_discovery._write_cache([BARE])
    assert json.loads(model_discovery._cache_path().read_text())["models"][f"ollama/{BARE}"]["digest"] == "abc"
    model_discovery.remember_tags_rows([{"name": BARE}])       # the daemon answered without a digest
    model_discovery._write_cache([BARE])
    entry = json.loads(model_discovery._cache_path().read_text())["models"][f"ollama/{BARE}"]
    assert entry["digest"] is None and entry["remote"] is False
    names, digests = display.inventory()                       # the reader treats None as no digest
    assert BARE in names and BARE not in digests
    model_discovery._last_probe_meta.clear()
