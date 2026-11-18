"""D-536: LOCAL_QUEUE + PINFIX at gate level (real local_gate file lock, real OllamaBackend, fake Ollama stream).

Pins the PINFIX accounting of server.py:1323 (``failed = attempted and reason not in {local_queue_timeout,
memory_budget}``): a queue wait that uses the whole step budget is neutral (no pinfix_outcome call, so the streak is
neither advanced nor reset), while a late admit that then runs out of budget is ``budget_exceeded`` and counts.
"""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy.backends import OllamaBackend
from llm_router.toolkit import local_gate as g
from tests.test_p13_backend_queue import FakeClient
from tests.test_proxy_cls_decide import turn_body
from tests.test_proxy_local_mode import MODEL, Breaker, Upstream, _overrides_readable, _post  # noqa: F401

URL = "http://localhost:59995"


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    for name in ('LLM_ROUTER_PINFIX_K', 'LLM_ROUTER_PINFIX_MAX_FALLBACKS', 'LLM_ROUTER_PIN_SCOPE',
                 'LLM_ROUTER_LOCAL_CLASSIFIER', 'LLM_ROUTER_CLAUDE_MARKER', 'LLM_ROUTER_CLASSIFIER_AUTHORITY',
                 'LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN', 'LLM_ROUTER_LOCAL_QUEUE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LLM_ROUTER_LOCAL_EXEMPT', 'none')
    monkeypatch.setenv('LLM_ROUTER_PINFIX', 'on')
    monkeypatch.setenv('LLM_ROUTER_LOCAL_GATE_DIR', str(tmp_path / 'gate'))
    monkeypatch.setenv('LLM_ROUTER_LOCAL_SLOTS', '1')
    g._FS_CACHE.clear()
    monkeypatch.setattr(g, '_fstype_for', lambda p: 'apfs')


def queue_on(monkeypatch):
    monkeypatch.setenv('LLM_ROUTER_LOCAL_QUEUE', 'on')


class Switch:
    """The backend's HTTP client; each step swaps in a fresh FakeClient (an asyncio.Event per asyncio.run)."""

    def __init__(self):
        self.cur, self.http_calls = FakeClient(), 0

    def stream(self, *a, **k):
        self.http_calls += 1
        return self.cur.stream(*a, **k)

    async def post(self, *a, **k):
        return await self.cur.post(*a, **k)


class Rig:
    def __init__(self, tmp_path, *, budget, hedge_s=None):
        self.client = Switch()
        self.be = OllamaBackend(MODEL, self.client, base_url=URL, num_ctx=8192, hedge_s=hedge_s, num_predict=50)

        async def _noguard(purpose):
            return None
        self.be._guard = _noguard
        self.up, self.ledger = Upstream(), tmp_path / 'proxy_calls.jsonl'
        config = ps.ProxyConfig(upstream='http://127.0.0.1:9', ledger_path=self.ledger, kill_switch=tmp_path / 'kill',
                                model=MODEL, trim='none', serve='local-agent', hedge_s=None, warm_up=False,
                                step_budget_s=budget)
        self.app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(self.up)),
                                backend_factory=lambda m: self.be, breaker_fn=Breaker())
        self.step = 0

    def _post(self):
        r = asyncio.run(_post(self.app, turn_body(['repair'], loops=self.step)))
        self.step += 1
        assert r.status_code == 200
        return ledger.read_rows(self.ledger)[-1]

    def ok(self):
        """Free slot, the model answers."""
        self.client.cur = FakeClient()
        return self._post()

    def queue_timeout(self):
        """Another holder keeps the only slot for longer than the whole step budget."""
        self.client.cur = FakeClient()
        before = self.client.http_calls
        held = g.admit(URL, time.monotonic() + 30)
        try:
            row = self._post()
        finally:
            held.release()
        assert self.client.http_calls == before   # never reached HTTP
        return row

    def late_admit(self):
        """The slot frees after 0.3 s, then the model sends nothing before the budget ends."""
        self.client.cur = FakeClient(lines=[], hold=asyncio.Event())
        held = g.admit(URL, time.monotonic() + 30)
        threading.Timer(0.3, held.release).start()
        return self._post()

    def stalled_in_ollama(self):
        """Queue off: the request reaches Ollama and waits there (Ollama's own queue); no first line in budget."""
        self.client.cur = FakeClient(lines=[], hold=asyncio.Event())
        return self._post()


def pinned(row):
    return row['local_mode'].get('pin_change') == 'local->claude'


def test_held_slot_is_neutral_local_queue_timeout(tmp_path, monkeypatch):
    """Case 1: no HTTP call, no pin, breaker not fed; the streak is untouched (next counted failure is still #1)."""
    queue_on(monkeypatch)
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '2')
    rig = Rig(tmp_path, budget=1.0)
    row = rig.queue_timeout()
    assert row['decision'] == 'fallback' and row['reason'] == 'local_queue_timeout', row
    assert not pinned(row) and 'backend_health' not in row
    assert not pinned(rig.stalled_in_ollama())        # counted failure 1 of K=2: the queue step did not count
    assert len(rig.up.requests) == 2


def test_k_queue_timeouts_never_pin_and_next_free_step_is_local(tmp_path, monkeypatch):
    """Case 2: K consecutive queue timeouts in one conversation; no pin, the next free-slot step is served locally."""
    queue_on(monkeypatch)
    rig = Rig(tmp_path, budget=1.0)
    rows = [rig.queue_timeout() for _ in range(3)]    # K default 3
    assert [r['reason'] for r in rows] == ['local_queue_timeout'] * 3
    assert not any(pinned(r) for r in rows)
    row = rig.ok()
    assert row['decision'] == 'served' and row['served_by'] == 'local', row


def test_late_admit_then_budget_runs_out_counts(tmp_path, monkeypatch):
    """Case 3 (the residual, documented, not fixed): a late admit that runs out of budget is budget_exceeded and counts."""
    queue_on(monkeypatch)
    monkeypatch.setenv('LLM_ROUTER_PINFIX_K', '2')
    rig = Rig(tmp_path, budget=1.5, hedge_s=60.0)
    first = rig.late_admit()
    assert first['reason'] == 'budget_exceeded' and not pinned(first), first
    second = rig.late_admit()
    assert second['reason'] == 'budget_exceeded' and pinned(second), second   # streak reached K=2
    assert second['pin_cause'] == 'budget_exceeded'


def test_mixed_sequence_pins_when_counted_streak_reaches_k(tmp_path, monkeypatch):
    """Case 4: qt, be, qt, be, be with K=3. Queue timeouts neither reset nor advance the streak (server.py:1323 skips
    pinfix_outcome for them), so the pin lands on the fifth step, the third budget_exceeded."""
    queue_on(monkeypatch)
    rig = Rig(tmp_path, budget=1.5, hedge_s=60.0)
    rows = [rig.queue_timeout(), rig.late_admit(), rig.queue_timeout(), rig.late_admit(), rig.late_admit()]
    assert [r['reason'] for r in rows] == ['local_queue_timeout', 'budget_exceeded', 'local_queue_timeout',
                                          'budget_exceeded', 'budget_exceeded']
    assert [pinned(r) for r in rows] == [False, False, False, False, True]
    after = rig.ok()
    assert after['reason'] == 'pinned_sticky' and after['decision'] == 'forwarded'


def test_queue_off_wait_inside_ollama_is_budget_exceeded_and_counts(tmp_path, monkeypatch):
    """Case 5: LOCAL_QUEUE unset (today's live flags): the same wait happens inside Ollama and counts toward the pin."""
    rig = Rig(tmp_path, budget=1.0)
    rows = [rig.stalled_in_ollama() for _ in range(3)]
    assert [r['reason'] for r in rows] == ['budget_exceeded'] * 3
    assert [pinned(r) for r in rows] == [False, False, True]
    assert rows[2]['pin_cause'] == 'budget_exceeded'
    assert rig.client.http_calls == 3
