"""Backend-health circuit breaker for the proxy's local serving path.

Evidence (``~/.rsi/research/llm-router-cursor-parity/p3-compaction-ab.md``,
2026-09-30): after a Metal fault (``command buffer 0 failed with status 5``,
``kIOGPUCommandBufferCallbackErrorOutOfMemory``) every later step got an empty
reply in ~0.1 s until the server was restarted: 29/29 empties came from those
windows, 0/24 while the backend was healthy. The breaker stops sending steps to
a backend in that state, and probes it cheaply after a cooldown.

The fake backend below returns empty replies the way the crashed server did.
No test makes a network call.
"""

from __future__ import annotations

import json

import httpx
import pytest

from llm_router.proxy import backend_health as bh
from llm_router.proxy import backends as pb
from llm_router.proxy.translate import from_ollama
from tests.test_proxy import TOKEN, Upstream, _call, _ollama, _post, _req, _rows  # noqa: F401
from llm_router.proxy import server as ps

EMPTY = {"message": {"role": "assistant", "content": "", "tool_calls": []}, "done": True,
         "done_reason": "stop"}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class CrashedBackend:
    """Behaves like the crashed Ollama server: every step is an empty reply in
    ~0 s; ``probe`` reports whatever ``healthy`` says."""

    def __init__(self, *, healthy: bool = False, reply=None) -> None:
        self.calls: list[dict] = []
        self.probes = 0
        self.healthy = healthy
        self.reply = reply

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        m, err = from_ollama(self.reply if self.healthy and self.reply else EMPTY, body)
        return m, err, {"first_token_s": 0.1}

    async def probe(self, timeout_s):
        self.probes += 1
        return (True, "ok") if self.healthy else (False, "empty reply")


@pytest.fixture
def policy(monkeypatch):
    async def _choose(text, pinned):
        return {"task_type": "code", "complexity": "moderate",
                "chain_head": ["ollama/fake:1"], "model": "ollama/fake:1"}

    monkeypatch.setattr(ps, "choose_model", _choose)


def _app(tmp_path, backend, clock=None, **cfg):
    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "proxy_calls.jsonl", **cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(Upstream()))
    return ps.build_app(config, client=client, backend_factory=lambda model: backend,
                        health_clock=clock)


# ── the classifier: which outcomes count against the backend ────────────────


@pytest.mark.parametrize("err,reason,elapsed,expected", [
    ("empty response", "validation", 0.1, bh.OUTCOME_FAIL),
    ("empty response", "validation", 5.0, bh.OUTCOME_FAIL),       # empty is empty, however slow
    ("unknown tool 'llm'", "validation", 0.3, bh.OUTCOME_FAIL),   # sub-second invalid
    ("unknown tool 'llm'", "validation", 17.2, bh.OUTCOME_OK),    # the model worked; the answer was wrong
    ("ConnectError: refused", "backend_error", 0.01, bh.OUTCOME_FAIL),
    ("exceeded step budget 30.0s", "budget_exceeded", 30.0, bh.OUTCOME_NEUTRAL),
    ("no first token within 8.0s", "hedge_timeout", 8.0, bh.OUTCOME_NEUTRAL),
    ("ollama error: decode() failed: Compute error.", "validation", 0.1, bh.OUTCOME_CRASH),
    ("RuntimeError: HTTP 500: ggml_metal_synchronize: command buffer 0 failed with status 5",
     "backend_error", 0.2, bh.OUTCOME_CRASH),
    ("HTTP 500: llama runner process has terminated: signal: abort trap", "backend_error", 0.2,
     bh.OUTCOME_CRASH),
    (None, None, 3.0, bh.OUTCOME_OK),
])
def test_classify(err, reason, elapsed, expected):
    assert bh.classify(err, reason, elapsed) == expected


# ── the state machine ────────────────────────────────────────────────────────


async def test_opens_after_n_consecutive_failures_and_a_success_resets_the_streak():
    clock = Clock()
    h = bh.BackendHealth(fail_n=3, cooldown_s=60, clock=clock, warn=lambda s: None)
    h.record("m", "empty response", "validation", 0.1)
    h.record("m", "empty response", "validation", 0.1)
    h.record("m", None, None, 2.0)  # a good reply in between: streak broken
    h.record("m", "empty response", "validation", 0.1)
    h.record("m", "empty response", "validation", 0.1)
    assert (await h.admit("m", CrashedBackend()))[0] is True
    tripped = h.record("m", "empty response", "validation", 0.1)
    assert tripped and tripped["state"] == "tripped" and tripped["trigger"] == "consecutive_invalid"
    ok, info = await h.admit("m", CrashedBackend())
    assert ok is False and info["state"] == "open"


async def test_crash_signature_opens_at_once():
    h = bh.BackendHealth(fail_n=3, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    t = h.record("m", "ollama error: Compute error.", "validation", 0.1)
    assert t["trigger"] == "crash_signature"
    assert (await h.admit("m", CrashedBackend()))[0] is False


async def test_timeouts_neither_count_nor_reset():
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    h.record("m", "empty response", "validation", 0.1)
    h.record("m", "exceeded step budget 30s", "budget_exceeded", 30.0)
    assert h.record("m", "empty response", "validation", 0.1)["state"] == "tripped"


async def test_probe_after_cooldown_closes_on_success_and_reopens_on_failure():
    clock = Clock()
    h = bh.BackendHealth(fail_n=1, cooldown_s=60, clock=clock, warn=lambda s: None)
    backend = CrashedBackend(healthy=False)
    h.record("m", "empty response", "validation", 0.1)
    clock.t += 59
    assert (await h.admit("m", backend))[0] is False and backend.probes == 0  # still cooling down
    clock.t += 2
    ok, info = await h.admit("m", backend)
    assert ok is False and backend.probes == 1 and info["probe_ok"] is False
    clock.t += 30
    assert (await h.admit("m", backend))[0] is False and backend.probes == 1  # new cooldown from the probe
    clock.t += 31
    backend.healthy = True
    ok, info = await h.admit("m", backend)
    assert ok is True and backend.probes == 2 and info["state"] == "recovered"
    assert (await h.admit("m", backend)) == (True, None)  # closed again: no probe per step


async def test_backend_without_probe_gets_one_trial_step_after_cooldown():
    class NoProbe:
        pass

    clock = Clock()
    h = bh.BackendHealth(fail_n=1, cooldown_s=10, clock=clock, warn=lambda s: None)
    h.record("m", "empty response", "validation", 0.1)
    clock.t += 11
    ok, info = await h.admit("m", NoProbe())
    assert ok is True and info["state"] == "trial"


async def test_fail_n_zero_disables_the_breaker():
    h = bh.BackendHealth(fail_n=0, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    for _ in range(10):
        assert h.record("m", "ollama error: Compute error.", "validation", 0.1) is None
    assert (await h.admit("m", CrashedBackend())) == (True, None)


async def test_trip_warns_once():
    said: list[str] = []
    h = bh.BackendHealth(fail_n=1, cooldown_s=60, clock=Clock(), warn=said.append)
    h.record("ollama/q:1", "empty response", "validation", 0.1)
    h.record("ollama/q:1", "empty response", "validation", 0.1)
    assert len(said) == 1 and "ollama/q:1" in said[0] and "60" in said[0]


# ── the proxy: red before the breaker, green after ──────────────────────────


async def test_proxy_stops_sending_steps_to_a_crashed_backend(tmp_path, policy):
    """Before the breaker, all 6 steps went to the crashed backend and each fell
    back. After it: 3 attempts, then ``backend_unhealthy`` without an attempt."""
    clock = Clock()
    backend = CrashedBackend()
    app = _app(tmp_path, backend, clock, backend_fail_n=3, backend_cooldown_s=60)
    for _ in range(6):
        r = await _post(app, _req())
        assert r.status_code == 200  # every step still answered (by Claude)
    assert len(backend.calls) == 3
    rows = _rows(tmp_path)
    assert [r["reason"] for r in rows] == ["validation"] * 3 + ["backend_unhealthy"] * 3
    assert rows[2]["backend_health"]["state"] == "tripped"
    skipped = rows[3]
    assert skipped["decision"] == "forwarded" and skipped["backend_health"]["state"] == "open"
    assert skipped["model"] == "ollama/fake:1" and skipped["upstream_status"] == 200


async def test_proxy_probes_after_cooldown_and_serves_again(tmp_path, policy):
    clock = Clock()
    backend = CrashedBackend(reply=_ollama("Running.", [_call("Bash", {"command": "ls"})]))
    app = _app(tmp_path, backend, clock, backend_fail_n=2, backend_cooldown_s=60)
    for _ in range(3):
        await _post(app, _req())
    assert _rows(tmp_path)[-1]["reason"] == "backend_unhealthy"
    clock.t += 61
    await _post(app, _req())  # probe fails: still unhealthy, no step sent
    assert backend.probes == 1 and len(backend.calls) == 2
    assert _rows(tmp_path)[-1]["reason"] == "backend_unhealthy"
    clock.t += 61
    backend.healthy = True
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert backend.probes == 2 and row["decision"] == "served"
    assert row["backend_health"]["state"] == "recovered"


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_BACKEND_FAIL_N", "5")
    monkeypatch.setenv("LLM_ROUTER_PROXY_BACKEND_COOLDOWN_S", "120")
    cfg = ps.ProxyConfig.from_env()
    assert cfg.backend_fail_n == 5 and cfg.backend_cooldown_s == 120.0
    monkeypatch.delenv("LLM_ROUTER_PROXY_BACKEND_FAIL_N")
    monkeypatch.delenv("LLM_ROUTER_PROXY_BACKEND_COOLDOWN_S")
    cfg = ps.ProxyConfig.from_env()
    assert cfg.backend_fail_n == bh.DEFAULT_FAIL_N and cfg.backend_cooldown_s == bh.DEFAULT_COOLDOWN_S


# ── the Ollama backend: crash signatures reach the breaker ──────────────────


def _ollama_client(status: int, lines: list[dict] | None = None, text: str | None = None):
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        if text is not None:
            return httpx.Response(status, text=text)
        body = "".join(json.dumps(x) + "\n" for x in lines or [])
        return httpx.Response(status, text=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_ollama_stream_error_line_is_reported_not_read_as_empty():
    client, _ = _ollama_client(200, [{"error": "decode() failed: Compute error."}])
    b = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=1024)
    m, err, _usage = await b.complete(_req(), 5.0)
    assert m is None and "Compute error" in err
    assert bh.classify(err, "validation", 0.1) == bh.OUTCOME_CRASH


async def test_ollama_http_500_body_is_in_the_error():
    client, _ = _ollama_client(500, text='{"error":"llama runner process has terminated: exit status 2"}')
    b = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=1024)
    with pytest.raises(Exception) as ei:
        await b.complete(_req(), 5.0)
    assert "runner process has terminated" in str(ei.value)


async def test_ollama_probe_is_one_token_with_the_serving_num_ctx():
    client, seen = _ollama_client(200, [{"message": {"content": "o"}, "done": False},
                                        {"message": {"content": ""}, "done": True, "eval_count": 1}])
    b = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=4096)
    assert (await b.probe(5.0))[0] is True
    p = seen[0]
    # same num_ctx as real steps: a different one would make Ollama reload the model
    assert p["options"] == {"num_ctx": 4096, "num_predict": 1} and p["model"] == "x"


@pytest.mark.parametrize("status,lines,text", [
    (200, [], None),                                   # the post-crash reply: no lines at all
    (200, [{"error": "Compute error."}], None),
    (500, None, '{"error":"runner stopped"}'),
    (200, [{"message": {"content": ""}, "done": True, "eval_count": 0}], None),
])
async def test_ollama_probe_fails_on_crash_shapes(status, lines, text):
    client, _ = _ollama_client(status, lines, text)
    b = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=1024)
    ok, detail = await b.probe(5.0)
    assert ok is False and detail


# ── the local_agent path (PR #216) goes through the same breaker ────────────


async def test_local_agent_path_skips_a_crashed_backend_before_compaction(tmp_path, policy, monkeypatch):
    from llm_router.local_agent import LocalAgentConfig
    from tests import test_local_agent as tla

    backend = CrashedBackend()
    app, up = tla._app(tmp_path, backend, LocalAgentConfig(), monkeypatch)
    for _ in range(5):
        assert (await tla._post(app, tla._body())).status_code == 200
    rows = ledger_rows(tmp_path / "calls.jsonl")
    assert len(backend.calls) == 3 and len(up) == 5
    assert [r["reason"] for r in rows] == ["validation"] * 3 + ["backend_unhealthy"] * 2
    assert all("compaction" in r for r in rows[:3])
    assert not any("compaction" in r for r in rows[3:])  # no embedding call to a broken server


async def test_a_fast_valid_edit_sent_to_claude_is_not_a_backend_failure(tmp_path, policy, monkeypatch):
    """The capability check turns a valid Edit reply into ``err``; that is a
    policy decision about a working backend, so it must not trip the breaker."""
    from tests import test_local_agent as tla

    backend = tla.FakeBackend(_ollama("", [tla._edit_call("/work/repo/src/mod.py")]))
    app, up = tla._app(tmp_path, backend, None, monkeypatch)
    for _ in range(5):
        await tla._post(app, tla._body())
    rows = ledger_rows(tmp_path / "calls.jsonl")
    assert len(backend.calls) == 5
    assert [r["reason"] for r in rows] == ["edit_to_claude"] * 5
    assert not any("backend_health" in r for r in rows)


def ledger_rows(path):
    from llm_router.proxy import ledger

    return ledger.read_rows(path)
