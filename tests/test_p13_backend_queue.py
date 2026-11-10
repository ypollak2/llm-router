"""P13: the proxy Ollama backend takes a slot in the local queue (LLM_ROUTER_LOCAL_QUEUE), nothing else does."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from llm_router.proxy.backends import OllamaBackend
from llm_router.toolkit import local_gate as g

URL = "http://localhost:59998"  # never contacted: fake clients only
BODY = {"model": "m", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}


class _Resp:
    status_code = 200

    def __init__(self, lines, hold=None):
        self._lines, self._hold = lines, hold

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln
        if self._hold is not None:
            await self._hold.wait()

    async def aread(self):
        return b""

    def raise_for_status(self):
        pass


class _Stream:
    def __init__(self, client, resp):
        self.c, self.resp = client, resp

    async def __aenter__(self):
        self.c.in_stream = True
        return self.resp

    async def __aexit__(self, *exc):
        self.c.in_stream = False


class FakeClient:
    def __init__(self, lines=None, hold=None, fail=None):
        self.timeouts, self.in_stream, self.fail, self.hold = [], False, fail, hold
        self.lines = lines if lines is not None else [
            json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True, "eval_count": 1})]
        self.started = asyncio.Event()

    def stream(self, method, url, json=None, timeout=None):  # noqa: A002
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        self.started.set()
        return _Stream(self, _Resp(self.lines, self.hold))

    async def post(self, url, json=None, timeout=None):  # noqa: A002
        self.timeouts.append(timeout)
        return _Resp([])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home" / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "1")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_QUEUE", raising=False)
    g._FS_CACHE.clear()
    monkeypatch.setattr(g, "_fstype_for", lambda p: "apfs")
    return tmp_path


def _backend(client, **kw):
    b = OllamaBackend("ollama/qwen3-coder:30b", client, base_url=URL, hedge_s=5.0, **kw)

    async def _noguard(purpose):
        return None
    b._guard = _noguard
    return b


def _spy(monkeypatch):
    calls = []
    real = g.aadmit

    async def spy(url, deadline, *, caller="", priority=None):
        calls.append((url, caller, priority))
        return await real(url, deadline, caller=caller, priority=priority)
    monkeypatch.setattr(g, "aadmit", spy)
    return calls


def _held() -> bool:
    return any(a._valid for a in list(g._LIVE.values()))


def test_flag_off_no_admission_no_gate_dir(env, monkeypatch):
    calls = _spy(monkeypatch)
    c = FakeClient()
    msg, err, _u = asyncio.run(_backend(c).complete(BODY, 30.0))
    assert err is None and msg is not None
    assert calls == []
    assert c.timeouts == [30.0]  # untouched budget
    assert not (env / "gate").exists()
    asyncio.run(_backend(c).warm_up(12.0))
    assert calls == [] and c.timeouts[-1] == 12.0 and not (env / "gate").exists()


def test_flag_on_holds_interactive_admission_for_the_stream(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    calls = _spy(monkeypatch)
    seen = []

    class C(FakeClient):
        def stream(self, *a, **k):
            seen.append(_held())
            return super().stream(*a, **k)
    c = C()
    asyncio.run(_backend(c).complete(BODY, 30.0))
    assert calls == [(URL, "proxy.complete", "interactive")]
    assert seen == [True]
    assert not _held()


def test_released_after_exception(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    c = FakeClient(fail=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        asyncio.run(_backend(c).complete(BODY, 30.0))
    assert not _held()
    # the slot is really free: a fresh admission with a tiny deadline succeeds
    g.admit(URL, time.monotonic() + 0.5).release()


def test_released_after_cancellation(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")

    async def run():
        c = FakeClient(hold=asyncio.Event())  # stream never ends
        t = asyncio.create_task(_backend(c).complete(BODY, 30.0))
        await asyncio.wait_for(c.started.wait(), 5)
        await asyncio.sleep(0.1)
        assert _held() and c.in_stream
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(run())
    assert not _held()
    g.admit(URL, time.monotonic() + 0.5).release()


def test_released_after_hedge_timeout(env, monkeypatch):
    from llm_router.proxy.backends import HedgeTimeout
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")

    async def run():
        c = FakeClient(lines=[], hold=asyncio.Event())
        b = _backend(c)
        b.hedge_s = 0.05
        with pytest.raises(HedgeTimeout):
            await b.complete(BODY, 30.0)
    asyncio.run(run())
    assert not _held()


def test_deadline_is_shared_between_queue_wait_and_http(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    pre = g.admit(URL, time.monotonic() + 2)
    c = FakeClient()

    async def run():
        t = asyncio.create_task(_backend(c).complete(BODY, 5.0))
        await asyncio.sleep(0.6)
        assert not c.started.is_set()  # still queued behind the held slot
        pre.release()
        return await t
    msg, err, _u = asyncio.run(run())
    assert err is None
    (http_timeout,) = c.timeouts
    assert 3.5 < http_timeout < 4.6, http_timeout  # 5.0 minus >= 0.6 s of waiting


def test_queue_expiry_raises_localqueuetimeout(env, monkeypatch):
    from llm_router.toolkit.adapters.ollama import LocalModelConflict
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    pre = g.admit(URL, time.monotonic() + 30)
    c = FakeClient()
    try:
        with pytest.raises(g.LocalQueueTimeout) as ei:
            asyncio.run(_backend(c).complete(BODY, 0.3))
    finally:
        pre.release()
    assert isinstance(ei.value, LocalModelConflict)
    assert c.timeouts == []  # no HTTP call was made


def test_nothing_remaining_after_wait_is_a_timeout(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    real = g.aadmit

    async def slow(url, deadline, **k):
        adm = await real(url, deadline, **k)
        await asyncio.sleep(0.2)  # the wait ate the whole budget after the slot was granted
        return adm
    monkeypatch.setattr(g, "aadmit", slow)
    c = FakeClient()
    with pytest.raises(g.LocalQueueTimeout):
        asyncio.run(_backend(c).complete(BODY, 0.1))
    assert c.timeouts == [] and not _held()


def test_warm_up_is_background(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    calls = _spy(monkeypatch)
    c = FakeClient()
    asyncio.run(_backend(c).warm_up(20.0))
    assert calls == [(URL, "proxy.warm_up", "background")]
    assert c.timeouts[0] <= 20.0 and not _held()


def test_probe_is_not_admitted(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    calls = _spy(monkeypatch)
    ok, _d = asyncio.run(_backend(FakeClient()).probe(5.0))
    assert ok and calls == []


def test_cancel_during_queue_wait_leaves_no_ticket_and_no_slot(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    pre = g.admit(URL, time.monotonic() + 30)
    c = FakeClient()

    async def run():
        t = asyncio.create_task(_backend(c).complete(BODY, 30.0))
        await asyncio.sleep(0.4)
        assert not c.started.is_set()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    try:
        asyncio.run(run())
        wait = g._wait_dir(g.gate_dir(URL))
        assert not wait.exists() or not [p for p in wait.iterdir() if g._ticket_live(p)]
    finally:
        pre.release()
    assert c.timeouts == []
    g.admit(URL, time.monotonic() + 0.5).release()


def test_warm_up_releases_on_exception(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")

    class Bad(FakeClient):
        async def post(self, *a, **k):
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        asyncio.run(_backend(Bad()).warm_up(10.0))
    assert not _held()
    g.admit(URL, time.monotonic() + 0.5).release()


def test_priority_parameter_defaults_interactive_and_can_be_background(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    calls = _spy(monkeypatch)
    asyncio.run(_backend(FakeClient()).complete(BODY, 30.0, priority="background"))
    assert calls == [(URL, "proxy.complete", "background")]


def test_queue_deadline_sits_inside_the_callers_budget(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    seen = []
    real = g.aadmit

    async def spy(url, deadline, **k):
        seen.append(deadline)
        return await real(url, deadline, **k)
    monkeypatch.setattr(g, "aadmit", spy)
    t = time.monotonic()
    asyncio.run(_backend(FakeClient()).complete(BODY, 10.0))
    assert seen[0] < t + 10.0   # strictly inside, so the queue expires before the server's wait_for


def test_first_token_excludes_queue_wait_and_wait_is_reported_when_flag_on(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    pre = g.admit(URL, time.monotonic() + 2)
    c = FakeClient()

    async def run():
        t = asyncio.create_task(_backend(c).complete(BODY, 10.0))
        await asyncio.sleep(0.6)
        pre.release()
        return await t
    _m, _e, usage = asyncio.run(run())
    assert usage["queue_wait_ms"] >= 500
    assert usage["first_token_s"] < 0.4   # model latency only
    monkeypatch.delenv("LLM_ROUTER_LOCAL_QUEUE")
    _m, _e, usage = asyncio.run(_backend(FakeClient()).complete(BODY, 10.0))
    assert "queue_wait_ms" not in usage


def test_hedge_is_clamped_to_the_time_left_after_the_queue_wait(env, monkeypatch):
    from llm_router.proxy.backends import HedgeTimeout
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")

    async def run():
        c = FakeClient(lines=[], hold=asyncio.Event())
        b = _backend(c)
        b.hedge_s = 60.0
        t0 = time.monotonic()
        with pytest.raises(HedgeTimeout):
            await b.complete(BODY, 1.0)
        return time.monotonic() - t0
    assert asyncio.run(run()) < 3.0
