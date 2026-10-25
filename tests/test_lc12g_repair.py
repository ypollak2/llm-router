"""LC-12g, review of #715: nothing on the pass-only text-job path sends the text over HTTP except the generate call;
the overflow refusal is a policy refusal, not an Ollama health failure; one pass-only predicate.

* ``semantic_cache`` POSTs the full prompt to ``{ollama_base_url}/api/embeddings`` with ``urllib`` (which honours
  ``HTTP_PROXY`` and has no loopback check) at check time and at store time. On the pass-only path neither runs;
* the judge queue (graded later by another model) gets no entry on the pass-only path;
* the refusal of the PACK_OVERFLOW model switch records no health failure, no ``attempt_failed`` row, and stops the walk.

Loopback fakes only (127.0.0.1 in this process); ``urlopen`` is stubbed for the non-loopback case. No model.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import socket
import urllib.request
from types import SimpleNamespace

import pytest

import llm_router.config as config_module
from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.health import get_tracker
from llm_router.tools import text as T
from tests.test_lc12 import DIFF, call_job, passed, world  # noqa: F401
from tests.test_lc12c import GOOD, LOCAL, e2e  # noqa: F401
from tests.test_lc12g import (  # noqa: F401
    REAL_CALL_TEXT, SECRET, FakeServer, _drain, _SwitchingPack, servers, switching,
)
from tests.test_lc8_routing import JOB

TEXT = DIFF + f"# {SECRET}\n"


def _job() -> str:
    return asyncio.run(T.llm_text_job(JOB, TEXT, SimpleNamespace()))


@pytest.fixture(autouse=True)
def _no_leak():
    """Loading a config with ``OLLAMA_BASE_URL`` set also writes ``OLLAMA_*`` into ``os.environ``; undo it. And
    drop ``urllib``'s cached global opener, which would keep this test's fake proxy for every later test."""
    import os
    env, cfg = dict(os.environ), config_module._config
    urllib.request._opener = None      # urlopen builds its global opener once, from the proxy env of its first use
    yield
    os.environ.clear()
    os.environ.update(env)
    config_module._config = cfg
    urllib.request._opener = None


def _ollama_base(monkeypatch, url: str):
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    config_module._config = None


def _embeds(server):
    return [p for _m, p, _b in server.seen if "embeddings" in p]


# ----------------------------------------------------------------------------- 1. the semantic cache
def test_pass_only_text_job_sends_no_embedding_request_and_no_text_to_the_proxy(e2e, servers, monkeypatch):
    """Real llm_text_job -> route_and_call -> _call_text -> call_llm, HTTP_PROXY set, the cache on (default), the
    ensemble on (production default), every routed call sampled for the judge. Every socket send in the process
    is watched: the text goes out once, to the fake Ollama."""
    proxy, ollama = servers
    _ollama_base(monkeypatch, ollama.url)
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    monkeypatch.setattr(router, "_call_text", REAL_CALL_TEXT)
    peers: list = []
    real_send, real_sendall = socket.socket.send, socket.socket.sendall

    def _watch(sock, data):
        if SECRET.encode() in bytes(data):
            peers.append(sock.getpeername()[1])

    monkeypatch.setattr(socket.socket, "send", lambda self, d, *a: (_watch(self, d), real_send(self, d, *a))[1])
    monkeypatch.setattr(socket.socket, "sendall", lambda self, d, *a: (_watch(self, d), real_sendall(self, d, *a))[1])
    passed(e2e)
    out = _job()
    _drain()
    assert "status=accepted" in out and "(ollama)" in out        # the job ran on the local model, for real
    assert peers and set(peers) == {int(ollama.url.rsplit(":", 1)[1])}   # every text-carrying send: the daemon
    assert _embeds(ollama) == [] and _embeds(proxy) == []        # check AND store: no /api/embeddings anywhere
    assert [p for _m, p, b in proxy.seen if SECRET.encode() in b] == []   # no text through the proxy
    assert [p for _m, p, b in ollama.seen if SECRET.encode() in b] == ["/api/generate"]


async def _cache_rows() -> int:
    from llm_router.cost import _get_db
    db = await _get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) FROM semantic_cache")
        return (await cur.fetchone())[0]
    finally:
        await db.close()


def _judge_queue() -> str:
    from llm_router import paths
    q = paths.state_path("judge_queue.jsonl")
    return q.read_text() if q.exists() else ""


@pytest.mark.parametrize("flag", [None, "1"])
def test_cache_and_judge_queue_are_skipped_only_on_the_pass_only_path(e2e, servers, monkeypatch, flag):
    """Flag unset (pass-only): no embedding request, no cache row, no judge-queue entry. The flag-on run is the
    control: the same job, same servers, reaches the embedding endpoint and writes both."""
    proxy, ollama = servers
    _ollama_base(monkeypatch, ollama.url)
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")   # conftest sets 0; production samples 10%
    if flag:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", flag)
    passed(e2e)
    out = _job()
    _drain()
    assert "status=accepted" in out
    embedded = bool(_embeds(proxy) or _embeds(ollama))
    rows, queued = asyncio.run(_cache_rows()), SECRET in _judge_queue()
    if flag is None:
        assert (embedded, rows, queued) == (False, 0, False)
    else:
        assert embedded and rows >= 1 and queued


def test_non_loopback_ollama_base_url_gets_no_urlopen_with_the_text(e2e, monkeypatch):
    # The generation daemon (OLLAMA_API_BASE) is loopback; the cache's embedding endpoint (OLLAMA_BASE_URL) is not.
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:9")
    _ollama_base(monkeypatch, "http://ollama.remote.invalid:11434")
    sent: list[tuple[str, bytes]] = []

    def urlopen(req, *a, **k):
        url = req if isinstance(req, str) else req.full_url
        sent.append((url, b"" if isinstance(req, str) else (req.data or b"")))
        raise OSError("blocked in test")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    passed(e2e)
    out = _job()
    _drain()
    assert "status=accepted" in out
    assert [u for u, d in sent if SECRET.encode() in d or "embeddings" in u] == []


class _RecordingOpener:
    """Stands in for ``local_privacy.direct_opener()`` (LC-12h: a loopback embedding request goes through it)."""

    def __init__(self, got):
        self.got = got

    def open(self, req, *a, **k):
        self.got.append(req if isinstance(req, str) else req.full_url)
        raise OSError("blocked in test")


def _record_direct(monkeypatch, got):
    from llm_router import local_privacy
    monkeypatch.setattr(local_privacy, "direct_opener", lambda: _RecordingOpener(got))


def test_the_cache_still_works_off_the_pass_only_path(e2e, monkeypatch):
    """The skip is scoped: outside a pass-only call the same cache reaches the embedding endpoint."""
    from llm_router import semantic_cache
    got: list[str] = []
    _record_direct(monkeypatch, got)
    assert semantic_cache._get_embedding(SECRET, "http://127.0.0.1:9") is None
    assert got == ["http://127.0.0.1:9/api/embeddings"]
    token = er.set_pass_only_dispatch(True)
    try:
        assert semantic_cache._cache_disabled() is True
        assert semantic_cache._get_embedding(SECRET, "http://127.0.0.1:9") is None
    finally:
        er.reset_pass_only_dispatch(token)
    assert got == ["http://127.0.0.1:9/api/embeddings"]          # the second call sent nothing


def test_semantic_classify_embedding_is_skipped_on_the_pass_only_path(monkeypatch):
    from llm_router import semantic_classify
    got: list[str] = []
    _record_direct(monkeypatch, got)
    token = er.set_pass_only_dispatch(True)
    try:
        assert semantic_classify._embed_ollama(SECRET, "http://127.0.0.1:9") is None
    finally:
        er.reset_pass_only_dispatch(token)
    assert got == []
    assert semantic_classify._embed_ollama(SECRET, "http://127.0.0.1:9") is None
    assert got == ["http://127.0.0.1:9/api/embeddings"]


# ----------------------------------------------------------------------------- 2. the overflow refusal
def test_two_overflow_refusals_leave_ollama_healthy_and_write_no_failed_row(e2e, switching, monkeypatch):
    monkeypatch.setattr(router, "_call_text", REAL_CALL_TEXT)
    failures: list[str] = []
    rows: list[dict] = []
    notes: list[str] = []
    tracker = get_tracker()
    real_fail = tracker.record_failure
    monkeypatch.setattr(tracker, "record_failure", lambda p: (failures.append(p), real_fail(p))[1])
    monkeypatch.setattr(router, "_emit_ledger_attempt", lambda *a, **k: rows.append(k))

    async def notify(ctx, level, msg):
        notes.append(msg)
    monkeypatch.setattr(router, "_notify", notify)
    passed(e2e)
    for _ in range(2):
        out = _job()
        _drain()
        assert "status=fallback" in out and "switching to an unpassed model is refused" in out
    assert switching == []                                         # no model call at all
    assert failures == [] and tracker.is_healthy("ollama")
    assert [r for r in rows if r.get("event_type") == "attempt_failed"] == []
    assert notes and all("trying next" not in n and "failed" not in n for n in notes)
    assert any("refused" in n for n in notes)


def test_a_real_provider_error_still_counts_as_a_failure(e2e, monkeypatch):
    """The refusal branch is narrow: an ordinary local error still records the failure."""
    e2e.state["script"][LOCAL] = RuntimeError("boom")
    failures: list[str] = []
    tracker = get_tracker()
    monkeypatch.setattr(tracker, "record_failure", lambda p: failures.append(p))
    passed(e2e)
    _job()
    _drain()
    assert failures == ["ollama"]


# ----------------------------------------------------------------------------- 3. one predicate
def test_pass_only_predicate_needs_both_the_reason_and_the_context_var():
    assert er.pass_only_call(er.TEXT_JOB_REASON) is False
    token = er.set_require_passed_first()
    try:
        assert er.pass_only_call(er.TEXT_JOB_REASON) is True
        assert er.pass_only_call("something_else") is False
        assert er.pass_only_call(None) is False
    finally:
        er.reset_require_passed_first(token)
    assert er.pass_only_dispatch_active() is False


def test_the_marker_does_not_leak_out_of_a_routed_call(e2e):
    passed(e2e)
    _job()
    _drain()
    assert er.pass_only_dispatch_active() is False
