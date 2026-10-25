"""LC-12h repair 2 (D-298): the model-info race is closed, deterministically.

The review's blocker: a concurrent caller for the same (daemon, model) returned from ``ensure_ollama_model_info``
while the first caller's ``/api/show`` was still in flight, so ``litellm.model_cost`` had no entry and LiteLLM ran
its own ``/api/show`` through its proxy-honouring client (model name, daemon URL, OLLAMA_API_KEY header).

No sleeps decide these tests: the fake Ollama's ``/api/show`` blocks on an Event the test releases once every
caller has entered ``ensure_ollama_model_info``. HTTP_PROXY points at a recording server, NO_PROXY is unset.
Loopback fakes only; no real Ollama, no model, no network.
"""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import os
import threading
import time
import urllib.request

import litellm
import pytest

import llm_router.config as config_module
from llm_router import local_privacy, providers
from tests.test_lc12g import PROXY_VARS
from tests.test_lc12h import _isolated, _ollama_answer, _Server, CTX_LEN  # noqa: F401  (autouse fixture)

MSG = [{"role": "user", "content": "hello"}]


@pytest.fixture
def slow(monkeypatch):
    """Fake proxy (records everything) + fake Ollama whose /api/show waits for ``gate``."""
    gate, started = threading.Event(), threading.Event()
    base = _ollama_answer("ollama")

    def ollama_answer(method, path, body):
        if path.endswith("/api/show"):
            started.set()
            gate.wait(10)
        return base(method, path, body)

    proxy, ollama = _Server(_ollama_answer("proxy")), _Server(ollama_answer)
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("ALL_PROXY", proxy.url)
    monkeypatch.setenv("OLLAMA_API_BASE", ollama.url)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", "8192")
    config_module._config = None
    n = type("Net", (), {})()
    n.proxy, n.ollama, n.gate, n.started = proxy, ollama, gate, started
    yield n
    gate.set()
    proxy.close()
    ollama.close()


def _shows(server):
    return [p for _m, p, _b in server.seen if p.endswith("/api/show")]


def _concurrent_calls(slow, n, model="ollama/m"):
    """``n`` real call_llm calls at once; the gate opens when all have entered ensure_ollama_model_info (+ 0.2 s
    for the late ones to pass the lock). Returns (responses, per-call 'entry present when ensure returned')."""
    entered, returned_with_entry = [], []
    lock = threading.Lock()
    real = local_privacy.ensure_ollama_model_info

    def watched(m, api_base=None):
        with lock:
            entered.append(m)
        real(m, api_base)
        with lock:
            returned_with_entry.append("ollama/m" in litellm.model_cost)

    def release():
        slow.started.wait(10)
        deadline = time.monotonic() + 10
        while len(entered) < n and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.2)
        slow.gate.set()

    async def go():
        threading.Thread(target=release, daemon=True).start()
        return await asyncio.gather(*(providers.call_llm(model, MSG) for _ in range(n)))

    mp = pytest.MonkeyPatch()
    mp.setattr(local_privacy, "ensure_ollama_model_info", watched)
    try:
        out = asyncio.run(go())
    finally:
        mp.undo()
    return out, returned_with_entry


@pytest.mark.parametrize("n", [2, 6])
def test_concurrent_calls_for_one_model_send_nothing_to_the_proxy(slow, n):
    responses, with_entry = _concurrent_calls(slow, n)
    assert len(responses) == n and all("(ollama)" in r.content for r in responses)
    assert slow.proxy.seen == []                      # the proxy saw nothing: no /api/show, no /api/generate
    assert len(_shows(slow.ollama)) == 1              # one direct fetch, shared by every caller
    assert with_entry == [True] * n                   # every caller returned with the entry in place
    assert litellm.model_cost["ollama/m"]["max_tokens"] == CTX_LEN   # the real entry won over the safe one


def test_the_model_map_has_an_entry_while_the_first_fetch_is_in_flight(slow):
    t = threading.Thread(target=local_privacy.ensure_ollama_model_info, args=("ollama/m",), daemon=True)
    t.start()
    assert slow.started.wait(10)
    entry = litellm.model_cost["ollama/m"]            # written BEFORE the fetch: the failure-shaped one
    assert entry["max_tokens"] is None and entry["litellm_provider"] == "ollama"
    from litellm.llms.ollama.common_utils import OllamaModelInfo
    assert OllamaModelInfo().get_model_info("ollama/m") is None   # LiteLLM would not look it up now
    slow.gate.set()
    t.join(10)
    assert litellm.model_cost["ollama/m"]["max_tokens"] == CTX_LEN


def test_a_removed_model_map_entry_is_reinserted_before_the_next_call(slow):
    slow.gate.set()
    local_privacy.ensure_ollama_model_info("ollama/m")
    assert "ollama/m" in litellm.model_cost and len(_shows(slow.ollama)) == 1
    del litellm.model_cost["ollama/m"]                 # a cost-map reload inside the freshness window
    local_privacy.ensure_ollama_model_info("ollama/m")
    assert litellm.model_cost["ollama/m"]["max_tokens"] == CTX_LEN     # the real entry, not a re-fetch
    assert len(_shows(slow.ollama)) == 1
    del litellm.model_cost["ollama/m"]
    resp = asyncio.run(providers.call_llm("ollama/m", MSG))
    assert "(ollama)" in resp.content and slow.proxy.seen == []


def test_the_failopen_path_leaves_an_entry_in_place(slow, monkeypatch):
    from llm_router.local_check import runtimes

    def boom(*a, **k):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(runtimes, "_fetch", boom)
    local_privacy.ensure_ollama_model_info("ollama/m")     # never raises
    assert litellm.model_cost["ollama/m"]["max_tokens"] is None
    assert not local_privacy._info_inflight                 # nothing left in flight
    local_privacy.ensure_ollama_model_info("ollama/m")
    assert "ollama/m" in litellm.model_cost


def test_three_callers_leave_nothing_in_flight(slow):
    out, with_entry = _concurrent_calls(slow, 3)
    assert with_entry == [True] * 3 and slow.proxy.seen == []
    assert not local_privacy._info_inflight


@pytest.mark.parametrize("model", ["ollama/m", "ollama_chat/m"])
def test_litellm_finds_the_entry_for_either_prefix_and_never_looks_the_model_up(slow, model, monkeypatch):
    from litellm.llms.ollama.common_utils import OllamaModelInfo
    slow.gate.set()
    local_privacy.ensure_ollama_model_info(model)
    looked_up = []
    monkeypatch.setattr(OllamaModelInfo, "get_runtime_model_info",
                        lambda self, m, *a, **k: looked_up.append(m))
    assert litellm.get_model_info(model)["max_tokens"] == CTX_LEN   # the post-call logging / cost path
    assert looked_up == [] and slow.proxy.seen == []
