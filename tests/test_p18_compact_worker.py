"""P18: LLM_ROUTER_COMPACT_BG background compaction worker. Mocked map call only; no model, no network.

Each test runs against a tmp LLM_ROUTER_HOME. The clock is a manual one, so no test waits for 60 s."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from llm_router.proxy import chunk_store, compact_worker, mr_summary
from llm_router.token_estimate import est_tokens
from llm_router.toolkit import local_gate as g
from tests.test_p9_proxy_mr import SUMMARY, _session

LIMIT = 100_000   # est 110,843 for _session(pairs=40): over HARD (80k); SOFT 50k, TARGET 50k
TOOL_REPLY = {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}
TEXT_REPLY = {"content": [{"type": "text", "text": "done"}]}


def _est(b: dict) -> int:
    return est_tokens(json.dumps(b))


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


class MapStub:
    def __init__(self, reply=SUMMARY):
        self.reply, self.prompts, self.nps = reply, [], []

    async def __call__(self, prompt, num_predict):
        self.prompts.append(prompt)
        self.nps.append(num_predict)
        return self.reply


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    for v in ("LLM_ROUTER_COMPACT_BG", "LLM_ROUTER_COMPACT_BG_TOOL_GAPS", "LLM_ROUTER_CHUNK_STORE", "LLM_ROUTER_SUMMARY_GATE"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path / "home"


def mk(call=None, *, limit=LIMIT, clock=None, **kw):
    clock = clock or Clock()
    rows = []
    w = compact_worker.Worker(call or MapStub(), est=_est, limit_fn=lambda: limit, model="ollama/m",
                              clock=clock, sink=rows.append, **kw)
    w.rows = rows
    w.clock_ = clock
    return w


def live_turn(w, key="c", body=None, reply=TEXT_REPLY, paid=0):
    w.note_request(key)
    w.note_done(key, body if body is not None else _session(pairs=40), reply, paid)


def run(coro):
    return asyncio.run(coro)


def idle(w, secs=61.0):
    w.clock_.t += secs


def test_flags_default_off_and_registered(monkeypatch):
    from llm_router import env_registry
    assert not compact_worker.enabled() and not compact_worker.tool_gaps_enabled()
    monkeypatch.setenv("LLM_ROUTER_COMPACT_BG", "1")
    monkeypatch.setenv("LLM_ROUTER_COMPACT_BG_TOOL_GAPS", "on")
    assert compact_worker.enabled() and compact_worker.tool_gaps_enabled()
    for name in ("LLM_ROUTER_COMPACT_BG", "LLM_ROUTER_COMPACT_BG_TOOL_GAPS"):
        assert env_registry.ENV_REGISTRY[name][1] == "proxy/compact_worker.py"


def test_below_soft_threshold_maps_nothing():
    call = MapStub()
    w = mk(call, limit=400_000)   # est 110k is 27% of the limit
    live_turn(w)
    idle(w)
    assert run(w.tick()) is None and not call.prompts and w.counts()["queued"] == 0


def test_soft_threshold_maps_one_chunk_and_hard_is_only_counted():
    call = MapStub()
    w = mk(call, limit=200_000)   # 55%: over soft (100k), under hard (160k)
    live_turn(w)
    idle(w)
    row = run(w.tick())
    assert row["outcome"] == "stored" and len(call.prompts) == 1 and w.counts()["hard_crossings"] == 0
    w2 = mk(MapStub(), limit=LIMIT)  # 110% of the limit: over hard
    live_turn(w2)
    idle(w2)
    run(w2.tick())
    assert w2.counts()["hard_crossings"] == 1


def test_idle_gating_needs_more_than_60s_and_no_live_turn():
    call = MapStub()
    w = mk(call)
    live_turn(w)
    idle(w, 59.0)
    assert run(w.tick()) is None and not call.prompts
    idle(w, 2.0)   # 61 s since the turn finished
    assert run(w.tick())["outcome"] == "stored"
    w.note_request("c")   # a live turn in flight is never a gap
    idle(w, 600.0)
    assert run(w.tick()) is None


def test_tool_gap_needs_its_flag():
    call = MapStub()
    w = mk(call)   # flag off
    live_turn(w, reply=TOOL_REPLY)
    idle(w, 5.0)
    assert run(w.tick()) is None and not call.prompts
    w = mk(call, tool_gaps=True)
    live_turn(w, reply=TOOL_REPLY)
    idle(w, 5.0)
    assert run(w.tick())["outcome"] == "stored"
    w = mk(call, tool_gaps=True)
    live_turn(w, reply=TEXT_REPLY)   # a text reply is not a tool gap, only idle counts
    idle(w, 5.0)
    assert run(w.tick()) is None


def test_tool_gap_flag_read_from_env(monkeypatch):
    w = mk()
    live_turn(w, reply=TOOL_REPLY)
    idle(w, 5.0)
    assert run(w.tick()) is None
    monkeypatch.setenv("LLM_ROUTER_COMPACT_BG_TOOL_GAPS", "1")
    assert run(w.tick())["outcome"] == "stored"


def test_one_map_per_gap():
    call = MapStub()
    w = mk(call)
    live_turn(w)
    idle(w)
    assert run(w.tick()) is not None
    idle(w, 600.0)
    assert run(w.tick()) is None and len(call.prompts) == 1
    live_turn(w)   # a new gap
    idle(w)
    assert run(w.tick()) is not None and len(call.prompts) == 2


def test_store_landing_next_live_turn_pays_zero_maps(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MR_CLOSE_THRESHOLD", "60000")
    call = MapStub()
    w = mk(call)
    for _ in range(30):
        live_turn(w)
        idle(w)
        if run(w.tick()) is None:
            break
    n = len(call.prompts)
    assert n > 1 and w.counts()["stored"] == n and w.counts()["completed"] == n
    live = MapStub()
    store = chunk_store.ChunkStore()
    res = run(mr_summary.shorten(copy.deepcopy(_session(pairs=40)), live, cap=LIMIT, est=_est, store=store, model="ollama/m"))
    assert res.changed and res.row["map_calls"] == 0 and not live.prompts
    assert res.row["store_hits"] == res.row["chunks"] > 0 and res.row["store_misses"] == 0


def test_worker_key_matches_p9_key(monkeypatch):
    """Same chunk, same model, same num_predict, same salt: a P9 run first fills what the worker would have mapped."""
    call = MapStub()
    r = run(mr_summary.shorten(copy.deepcopy(_session(pairs=40)), call, cap=LIMIT, est=_est,
                               store=chunk_store.ChunkStore(), model="ollama/m"))
    first = len(call.prompts)
    assert r.changed and first > 0
    w = mk(MapStub())
    body = _session(pairs=40)
    plan = run(w._plan(body))
    assert plan.saved > 0 and plan.next_chunk is not None   # the P9 chunks are seen as stored, the next one is not
    other = compact_worker.Worker(MapStub(), est=_est, limit_fn=lambda: LIMIT, model="ollama/other", sink=lambda r: None)
    assert run(other._plan(body)).saved == 0               # a different model is a different key


def test_invalid_reply_is_not_stored():
    w = mk(MapStub(reply="no headings"))
    live_turn(w)
    idle(w)
    row = run(w.tick())
    assert row["outcome"] == "invalid" and w.counts()["stored"] == 0 and w.counts()["completed"] == 1
    assert not chunk_store.ChunkStore().entries


def test_map_failure_is_counted_and_never_raises():
    class Boom:
        async def __call__(self, prompt, num_predict):
            raise RuntimeError("backend down")

    w = mk(Boom())
    live_turn(w)
    idle(w)
    assert run(w.tick())["outcome"] == "failed" and w.counts()["failed"] == 1


@pytest.mark.timing
def test_live_request_preempts_a_running_map():
    started = {}

    async def scenario():
        gate = asyncio.Event()

        async def slow(prompt, num_predict):
            started["cancelled"] = False
            try:
                await gate.wait()
            except asyncio.CancelledError:
                started["cancelled"] = True
                raise
            return SUMMARY

        w = mk(slow, poll_s=0.01)
        live_turn(w)
        idle(w)
        t = asyncio.create_task(w.tick())
        for _ in range(200):
            if w._inflight is not None:
                break
            await asyncio.sleep(0.005)
        assert w._inflight is not None
        w.note_request("c")   # the interactive turn arrives
        row = await asyncio.wait_for(t, 5)
        return w, row

    w, row = run(scenario())
    assert row["outcome"] == "aborted" and started["cancelled"] is True
    assert w.counts()["aborted"] == 1 and w.counts()["stored"] == 0 and w._inflight is None
    assert not chunk_store.ChunkStore().entries   # nothing half-done landed


@pytest.mark.timing
def test_p12_interactive_waiter_preempts_background_map_and_gets_the_slot(tmp_path, monkeypatch):
    """With the real P12 queue: the background map holds the slot at ``background`` priority, an interactive
    waiter appears, ``should_yield`` turns true, the worker cancels ITS map (which releases the admission)."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("LLM_ROUTER_LOCAL_QUEUE", "on")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_SLOTS", "1")
    g._FS_CACHE.clear()
    monkeypatch.setattr(g, "_fstype_for", lambda p: "apfs")
    url = "http://localhost:59997"   # never contacted

    async def scenario():
        holding = asyncio.Event()

        async def bg_map(prompt, num_predict):
            adm = await g.aadmit(url, asyncio.get_running_loop().time() + 30, caller="test.bg", priority="background")
            holding.set()
            try:
                await asyncio.sleep(30)   # a long generation
            finally:
                adm.release()

        w = mk(bg_map, yield_check=lambda: g.should_yield(url), poll_s=0.02)
        live_turn(w)
        idle(w)
        t = asyncio.create_task(w.tick())
        await asyncio.wait_for(holding.wait(), 5)
        got = asyncio.get_running_loop().time()
        adm = await g.aadmit(url, got + 10, caller="test.live", priority="interactive")   # waits behind the holder
        waited = asyncio.get_running_loop().time() - got
        adm.release()
        return w, await asyncio.wait_for(t, 5), waited

    w, row, waited = run(scenario())
    assert row["outcome"] == "aborted" and w.counts()["aborted"] == 1 and waited < 5


@pytest.mark.timing
def test_never_more_than_one_map_in_flight():
    peak = {"now": 0, "max": 0}

    async def scenario():
        async def call(prompt, num_predict):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.05)
            peak["now"] -= 1
            return SUMMARY

        w = mk(call, poll_s=0.01)
        for key in ("a", "b", "c"):
            live_turn(w, key=key)
        idle(w)
        await asyncio.gather(*(w.tick() for _ in range(5)))
        return w

    w = run(scenario())
    assert peak["max"] == 1 and w.counts()["started"] == 1


def test_worker_never_cancels_a_task_it_did_not_start():
    async def scenario():
        foreign = asyncio.create_task(asyncio.sleep(30))
        w = mk(MapStub())
        live_turn(w)
        w.note_request("c")   # a live request with no map of ours in flight
        await asyncio.sleep(0)
        alive = not foreign.done() and w._preempt is False
        foreign.cancel()
        return alive

    assert run(scenario())


def test_thrash_stop_after_three_consecutive_refills(monkeypatch):
    """The prompt keeps growing back over SOFT right after each finished cycle: stop for that conversation."""
    call = MapStub()
    w = mk(call, limit=140_000)
    pairs = 40
    stopped_at = None
    for cycle in range(8):
        pairs += 25   # the conversation refills
        for _ in range(40):
            live_turn(w, body=_session(pairs=pairs))
            idle(w)
            row = run(w.tick())
            if row is None or row["outcome"] == "thrash_stop":
                break
        if w.counts()["thrash_stops"]:
            stopped_at = cycle
            break
    assert stopped_at is not None and w.counts()["thrash_stops"] == 1
    n = len(call.prompts)
    live_turn(w, body=_session(pairs=pairs + 40))
    idle(w)
    assert run(w.tick()) is None and len(call.prompts) == n   # stopped for this conversation
    live_turn(w, key="other", body=_session(pairs=pairs + 40))   # another conversation is unaffected
    idle(w)
    assert run(w.tick()) is not None
    stops = [r for r in w.rows if r["outcome"] == "thrash_stop"]
    assert len(stops) == 1 and stops[0]["refills"] == compact_worker.THRASH_REFILLS


def test_ledger_rows_hold_counts_and_timings_only():
    w = mk(MapStub())
    live_turn(w, paid=2)
    idle(w)
    run(w.tick())
    blob = json.dumps(w.rows)
    assert "Goal and user constraints" not in blob and "line 0 of output" not in blob
    allowed = {"ts", "outcome", "map_s", "counts", "maps_paid", "refills", "before_start"}
    assert all(set(r) <= allowed for r in w.rows)
    assert w.counts()["live_turns"] == 1 and w.counts()["live_maps_paid"] == 2
    assert {"queued", "started", "aborted", "completed", "stored", "thrash_stops"} <= set(w.counts())


def test_non_local_turn_forgets_the_conversation():
    w = mk(MapStub())
    w.note_request("c")
    w.note_done("c", _session(pairs=40), None)
    assert "c" not in w.convs
    idle(w)
    assert run(w.tick()) is None
