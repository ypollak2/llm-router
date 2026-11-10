"""P22 cold-start rule (LLM_ROUTER_COLD_GUARD): behaviour through the real proxy app."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from llm_router.proxy import backends as pb
from llm_router.proxy import cold_guard
from llm_router.proxy import server as ps
from tests.test_proxy_local_mode import (  # noqa: F401
    Backend, _first_call, _ollama, _overrides_readable, _post, env,
)
from llm_router.proxy.translate import from_ollama


class WarmBackend(Backend):
    """Records num_predict and queue priority of every call; a warm-up is a call with num_predict 1."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.meta = []
        self.num_predict = None
        self.model = "fake:1"
        self.num_ctx = 32768
        self.gate = None  # an asyncio.Event a test can hold a warm-up behind

    async def complete(self, body, timeout_s, priority="interactive"):
        self.meta.append({"priority": priority, "num_predict": self.num_predict,
                          "model": self.model, "num_ctx": self.num_ctx})
        if priority == "background" and self.gate is not None:
            await self.gate.wait()
        self.calls.append(body)
        m, err = from_ollama(self.replies[0], body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


@pytest.fixture(autouse=True)
def _fresh_guard(monkeypatch):
    monkeypatch.setattr(cold_guard, "GUARD", cold_guard.ColdGuard())
    monkeypatch.delenv("LLM_ROUTER_COLD_GUARD", raising=False)
    monkeypatch.delenv("LLM_ROUTER_DEADLINE_CAP_S", raising=False)


def _long(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DEADLINE_CAP_S", "0.05")  # any prompt now predicts a longer prefill


async def _settle(backend, n_calls):
    for _ in range(200):
        if len(backend.calls) >= n_calls:
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("warm-up never completed")


async def test_cold_and_long_goes_to_claude_without_pin_and_queues_one_warm_up(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_COLD_GUARD", "1")
    _long(monkeypatch)
    backend = WarmBackend(_ollama("x"))
    app = env.app(backend)
    await _post(app, _first_call())
    await _settle(backend, 1)
    assert len(env.up.requests) == 1
    (row,) = env.rows()
    assert row["reason"] == "cold_over_deadline" and row["egress"] is True
    cg = row["local_mode"]["cold_guard"]
    assert cg["warm_up_queued"] is True and cg["predicted_prefill_s"] > 0
    assert "pin_change" not in row["local_mode"]
    assert backend.meta == [{"priority": "background", "num_predict": 1, "model": "fake:1", "num_ctx": 32768}]


async def test_the_next_turn_after_the_warm_up_completes_goes_local(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_COLD_GUARD", "1")
    _long(monkeypatch)
    backend = WarmBackend(_ollama("x"))
    app = env.app(backend)
    first = _first_call()
    await _post(app, first)
    await _settle(backend, 1)
    nxt = copy.deepcopy(first)  # append-only: the same conversation plus one more exchange
    nxt["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                        {"role": "user", "content": [{"type": "text", "text": "continue"}]}]
    await _post(app, nxt)
    rows = env.rows()
    assert rows[1]["decision"] == "served" and len(env.up.requests) == 1  # only the cold turn went to Claude
    assert rows[1]["local_mode"]["pin"] == "local"
    assert len(backend.calls) == 2 and "cold_guard" not in rows[1]["local_mode"]


async def test_warm_and_long_goes_local(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_COLD_GUARD", "1")
    _long(monkeypatch)
    backend = WarmBackend(_ollama("x"))
    app = env.app(backend)
    body = _first_call()
    cold_guard.GUARD.note_resident(cold_guard.GUARD.observe(_ollama_payload(body)))
    await _post(app, body)
    (row,) = env.rows()
    assert row["decision"] == "served" and env.up.requests == []


def _ollama_payload(body):
    from llm_router.proxy.translate import to_ollama
    return to_ollama(body, "fake:1", num_ctx=32768, max_predict=1)


async def test_cold_and_short_goes_local(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_COLD_GUARD", "1")
    backend = WarmBackend(_ollama("x"))
    await _post(env.app(backend), _first_call())
    (row,) = env.rows()
    assert row["decision"] == "served" and env.up.requests == []
    assert backend.meta[0]["priority"] == "interactive"


async def test_flag_off_is_unchanged(env, monkeypatch):  # noqa: F811
    _long(monkeypatch)
    backend = WarmBackend(_ollama("x"))
    await _post(env.app(backend), _first_call())
    (row,) = env.rows()
    assert row["decision"] == "served" and "cold_guard" not in row["local_mode"]
    assert len(backend.calls) == 1 and env.up.requests == []


async def test_duplicate_cold_turns_queue_one_warm_up(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_COLD_GUARD", "1")
    _long(monkeypatch)
    backend = WarmBackend(_ollama("x"))
    backend.gate = asyncio.Event()  # hold the warm-up so both cold turns arrive while it runs
    app = env.app(backend)
    await _post(app, _first_call())
    await _post(app, _first_call())
    backend.gate.set()
    await _settle(backend, 1)
    queued = [r["local_mode"]["cold_guard"]["warm_up_queued"] for r in env.rows()]
    assert queued == [True, False]
    assert len(backend.meta) == 1


# ── warm-up deadline, real OllamaBackend, simulated time ────────────────────────────────────────────────────

SIM = {"first_line_s": 0.0}  # simulated wait for the first streamed line


class _Resp:
    status_code = 200

    async def aiter_lines(self):
        yield json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True, "eval_count": 1,
                          "prompt_eval_count": 50})

    async def aread(self):
        return b""

    def raise_for_status(self):
        pass


class _Stream:
    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self):
        self.timeouts, self.payloads = [], []

    def stream(self, method, url, json=None, timeout=None):  # noqa: A002
        self.timeouts.append(timeout)
        self.payloads.append(json)
        return _Stream()


@pytest.fixture
def sim_time(monkeypatch):
    """Time for the first line is simulated: a wait_for on the stream's first line times out when the simulated
    delay exceeds its timeout; nothing sleeps."""
    real = asyncio.wait_for

    async def fake(aw, timeout=None):
        if type(aw).__name__ == "async_generator_asend" and timeout is not None and SIM["first_line_s"] > timeout:
            raise asyncio.TimeoutError
        return await real(aw, timeout)

    monkeypatch.setattr(pb.asyncio, "wait_for", fake)
    monkeypatch.delenv("LLM_ROUTER_LOCAL_QUEUE", raising=False)
    SIM["first_line_s"] = 0.0
    yield
    SIM["first_line_s"] = 0.0


async def test_the_warm_up_outlives_the_default_hedge_and_the_prefix_becomes_warm(env, monkeypatch, sim_time):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_COLD_GUARD", "1")
    _long(monkeypatch)
    client = _Client()
    made = []

    def factory(model):
        b = pb.OllamaBackend("ollama/fake:1", client, base_url="http://localhost:59998", num_ctx=32768,
                             hedge_s=pb.DEFAULT_HEDGE_S)
        made.append(b)
        return b

    SIM["first_line_s"] = 30.0  # prefill: far past the 8 s default hedge, inside the warm-up deadline
    old_path = pb.OllamaBackend("ollama/fake:1", client, base_url="http://localhost:59998", num_ctx=32768,
                                hedge_s=pb.DEFAULT_HEDGE_S)
    with pytest.raises(pb.HedgeTimeout):  # what the old code path (default hedge) did to the warm-up
        await old_path.complete(_first_call(), 60.0, priority="background")

    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=env.ledger, kill_switch=env.kill,
                            model="ollama/fake:1", trim="none", serve="local-agent", hedge_s=pb.DEFAULT_HEDGE_S,
                            warm_up=False)
    app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(env.up)),
                       backend_factory=factory, breaker_fn=lambda lever, task_type: type(
                           "D", (), {"allowed": True, "reason": "t"})())
    first = _first_call()
    await _post(app, first)
    for _ in range(300):
        if cold_guard.GUARD.is_warm(cold_guard.GUARD.observe(_ollama_payload(first))):
            break
        await asyncio.sleep(0.01)
    assert cold_guard.GUARD.is_warm(cold_guard.GUARD.observe(_ollama_payload(first)))
    warm = [p for p in client.payloads if p["options"].get("num_predict") == 1]
    assert warm and warm[-1]["options"]["num_ctx"] == 32768  # the resident model's num_ctx
    SIM["first_line_s"] = 0.0
    await _post(app, first)  # warm now: served locally
    assert env.rows()[-1]["decision"] == "served"


# ── resident prefix lifetime, failure memory, generations ──────────────────────────────────────────────────

def _obs(*msgs):
    return {"sys": "s", "msgs": list(msgs), "keep_alive": None}


def test_the_resident_prefix_expires_with_keep_alive():
    g = cold_guard.ColdGuard()
    g.note_resident(_obs("a"), now=100.0)
    g.ttl_s = 600.0
    g.note_resident(_obs("a"), now=100.0)
    assert g.is_warm(_obs("a", "b"), now=699.0) and not g.is_warm(_obs("a", "b"), now=701.0)


def test_ttl_from_keep_alive():
    f = cold_guard.ttl_from_keep_alive
    assert f(600) == 600.0 and f("10m") == 600.0 and f("90s") == 90.0
    assert f(-1) == f("-1") == f(None) == f("forever") == cold_guard.DEFAULT_TTL_S


def test_a_local_failure_clears_the_resident_prefix():
    g = cold_guard.ColdGuard()
    g.note_resident(_obs("a"))
    g.clear()
    assert not g.is_warm(_obs("a", "b"))


def test_a_failed_warm_up_is_not_retried_for_that_prefix_for_ten_minutes():
    g = cold_guard.ColdGuard()
    key, gen = g.begin_warm_up(_obs("a"), now=0.0)
    assert g.end_warm_up(key, _obs("a"), False, gen, now=1.0) is False
    assert g.begin_warm_up(_obs("a"), now=500.0) is None
    assert g.begin_warm_up(_obs("other"), now=500.0) is not None  # another prefix is unaffected
    g.end_warm_up(g._inflight, None, False, 0, now=500.0)
    assert g.begin_warm_up(_obs("a"), now=602.0) is not None


def test_a_warm_up_does_not_overwrite_a_prefix_a_live_turn_recorded_meanwhile():
    g = cold_guard.ColdGuard()
    key, gen = g.begin_warm_up(_obs("a"))
    g.note_resident(_obs("live"))  # a live local turn, a different prefix, served while the warm-up ran
    assert g.end_warm_up(key, _obs("a"), True, gen) is False
    assert g.is_warm(_obs("live", "x")) and not g.is_warm(_obs("a", "x"))
