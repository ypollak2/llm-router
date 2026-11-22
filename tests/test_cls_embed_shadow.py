"""PR-4 cls_embed_shadow: the shadow lane records a verdict off the request path and never changes a decision.
A fake HTTP embed server only; no real Ollama server is contacted."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from llm_router.proxy import cls_embed, cls_embed_shadow
from tests.test_cls_embed import CANARY, _artifact, _Fake
from tests.test_proxy_cls_decide import Env, turn_body
from tests.test_proxy_local_mode import _first_call, _post

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED", "shadow")
    monkeypatch.delenv("LLM_ROUTER_LOCAL_MODEL_URL", raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:11434")
    art = tmp_path / "head.json"
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_ARTIFACT", str(art))
    _artifact(art)
    cls_embed.reset_state()
    cls_embed_shadow.reset()
    fake = _Fake()
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_URL", fake.url)
    yield tmp_path, fake
    fake.close()
    cls_embed.reset_state()
    cls_embed_shadow.reset()


def recs(tmp_path):
    p = tmp_path / cls_embed_shadow.SHADOW_NAME
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def mk(text="fix the bug", sha="sha1", origin="typed", step="turn_first", sid="s1"):
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}], "system": "x"}
    row = {"ts": 1.0, "session_id": sid, "text_sha": sha, "turn_origin": origin, "step_class": step,
           "msg_id": None}
    return body, row


LOCAL = SimpleNamespace(local=True, reason=None)


def run(coro):
    return asyncio.run(coro)


async def _sched_and_drain(*a, **k):
    out = cls_embed_shadow.schedule(*a, **k)
    await cls_embed_shadow.drain()
    return out


def test_verdict_recorded_when_fast_text_free(env):
    tmp, fake = env
    body, row = mk(CANARY)
    assert run(_sched_and_drain(body, row, LOCAL)) == cls_embed_shadow.SCHEDULED
    (r,) = recs(tmp)
    assert r["cls_emb_source"] == "embed" and r["cls_emb_tier"] == "local" and r["late"] is False
    assert r["rules_decision"] == "local" and r["session_id"] == "s1" and r["text_sha"] == "sha1"
    assert r["pop"] == "typed" and r["step_class"] == "turn_first"
    assert CANARY not in json.dumps(r) and CANARY not in (tmp / cls_embed_shadow.SHADOW_NAME).read_text()
    assert any(CANARY in b.get("input", "") for b in fake.bodies)  # the embed server did get text (as designed)


@pytest.mark.timing
def test_abstain_recorded_when_slow_and_request_not_blocked(env, monkeypatch):
    tmp, fake = env
    fake.delay = 1.0
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "250")

    async def go():
        body, row = mk()
        t0 = time.monotonic()
        assert cls_embed_shadow.schedule(body, row, LOCAL) == cls_embed_shadow.SCHEDULED
        sync_s = time.monotonic() - t0  # what the request path pays
        await cls_embed_shadow.drain()
        return sync_s, time.monotonic() - t0

    sync_s, total = run(go())
    assert sync_s < 0.05          # schedule returns at once; nothing is awaited on the request path
    (r,) = recs(tmp)
    assert r["cls_emb_source"] == "timeout" and r["cls_emb_tier"] is None and r["late"] is True
    assert 0.2 < total < 0.9      # the budget bounded the task although the server needs 1.0 s


@pytest.mark.timing
def test_loop_stays_responsive_while_embed_is_slow(env, monkeypatch):
    tmp, fake = env
    fake.delay = 1.0
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "2000")

    async def go():
        body, row = mk()
        cls_embed_shadow.schedule(body, row, LOCAL)
        ticks, t0 = 0, time.monotonic()
        while time.monotonic() - t0 < 0.4:
            await asyncio.sleep(0.01)
            ticks += 1
        await cls_embed_shadow.drain()
        return ticks

    assert run(go()) >= 25  # a blocked loop would tick ~0 times
    assert recs(tmp)[0]["cls_emb_source"] == "embed"


@pytest.mark.parametrize("kw,dec,expect", [
    ({"origin": None}, LOCAL, cls_embed_shadow.SKIP_NOT_HUMAN),            # continuation
    ({"origin": "task_notification"}, LOCAL, cls_embed_shadow.SKIP_NOT_HUMAN),
    ({"step": "side_call"}, LOCAL, cls_embed_shadow.SKIP_SIDE_CALL),
    ({}, SimpleNamespace(local=False, reason="claude_marker"), cls_embed_shadow.SKIP_MARKER),
], ids=["continuation", "notification", "side_call", "marker"])
def test_never_runs_for_side_calls_marker_or_non_human(env, kw, dec, expect):
    tmp, fake = env
    body, row = mk(**kw)
    assert run(_sched_and_drain(body, row, dec)) == expect
    assert fake.bodies == [] and recs(tmp) == []


def test_not_eligible_row_and_secret_text_skipped(env):
    tmp, fake = env
    body, row = mk()
    row["reason"] = "not_eligible"
    assert run(_sched_and_drain(body, row, LOCAL)) == cls_embed_shadow.SKIP_SIDE_CALL
    secret = "key sk-ant-api03-" + "Zq9xKd2LmP" * 5
    body, row = mk(secret)
    assert run(_sched_and_drain(body, row, LOCAL)) == cls_embed_shadow.SKIP_SECRET
    assert fake.bodies == []


def test_one_embed_per_turn_dedup(env):
    tmp, fake = env

    async def go():
        for _ in range(3):
            body, row = mk()
            cls_embed_shadow.schedule(body, row, LOCAL)
        await cls_embed_shadow.drain()

    run(go())
    assert len(recs(tmp)) == 1 and len(fake.bodies) == 1


def test_default_off_runs_nothing(env, monkeypatch):
    tmp, fake = env
    monkeypatch.delenv("LLM_ROUTER_CLS_EMBED")
    body, row = mk()
    assert run(_sched_and_drain(body, row, LOCAL)) == cls_embed_shadow.OFF
    assert cls_embed_shadow._lane is None  # no pool, no thread was even created
    assert fake.bodies == [] and recs(tmp) == []


@pytest.mark.timing
def test_dedicated_executor_saturation_does_not_starve_default_executor(env, monkeypatch):
    tmp, fake = env
    fake.delay = 1.2
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "100")

    async def go():
        for i in range(cls_embed_shadow.MAX_PENDING + 2):  # saturate the lane (4 busy + overflow)
            body, row = mk(f"prompt {i}", sha=f"sha{i}")
            cls_embed_shadow.schedule(body, row, LOCAL)
        t0 = time.monotonic()
        await asyncio.wait_for(asyncio.to_thread(lambda: 42), 0.5)  # the default executor is free
        used = time.monotonic() - t0
        await cls_embed_shadow.drain()
        return used

    assert run(go()) < 0.3
    rs = recs(tmp)
    assert len(rs) == cls_embed_shadow.MAX_PENDING + 2
    assert sum(r["cls_emb_source"] == "queue_full" for r in rs) >= 1  # overflow dropped, not queued
    names = {t.name for t in threading.enumerate()}
    assert not any(n.startswith("asyncio_") and "cls" in n for n in names)
    time.sleep(1.4)  # let the stuck workers end before the fixture closes the fake server


@pytest.mark.timing
def test_slot_frees_only_when_thread_ends(env, monkeypatch):
    tmp, fake = env
    fake.delay = 0.6
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "100")

    async def go():
        body, row = mk()
        cls_embed_shadow.schedule(body, row, LOCAL)
        await cls_embed_shadow.drain()          # task finished at the budget...
        during = cls_embed_shadow._lane.pending  # ...but the thread still holds its slot
        await asyncio.sleep(0.8)
        return during, cls_embed_shadow._lane.pending

    during, after = run(go())
    assert during == 1 and after == 0


def test_failure_never_raises_and_decision_untouched(env, monkeypatch):
    tmp, fake = env
    monkeypatch.setattr(cls_embed, "classify", lambda t: (_ for _ in ()).throw(RuntimeError(CANARY)))
    body, row = mk()
    dec = SimpleNamespace(local=True, reason=None)
    assert run(_sched_and_drain(body, row, dec)) == cls_embed_shadow.SCHEDULED
    assert dec.local is True and dec.reason is None
    (r,) = recs(tmp)
    assert r["cls_emb_source"] == "error" and CANARY not in json.dumps(r)
    from llm_router.paths import state_path
    fo = [p for p in state_path("").glob("*failopen*")]
    assert all(CANARY not in p.read_text() for p in fo if p.is_file())


# -- through the real proxy ----------------------------------------------------------


def _post_and_drain(env, body):
    async def go():
        resp = await _post(env.app, body)
        await cls_embed_shadow.drain()
        return resp
    return run(go())


@pytest.mark.timing
def test_proxy_flag_on_decisions_equal_flag_off_and_forward_not_delayed(tmp_path, monkeypatch, env):
    home, fake = env
    fake.delay = 1.0
    monkeypatch.setenv("LLM_ROUTER_CLS_EMBED_BUDGET_MS", "2000")
    body = turn_body(["rename the helper function"], loops=0)

    (tmp_path / "on").mkdir(exist_ok=True)
    (tmp_path / "off").mkdir(exist_ok=True)
    e_on = Env(tmp_path / "on")

    async def timed():
        t0 = time.monotonic()
        resp = await _post(e_on.app, body)
        served = time.monotonic() - t0
        await cls_embed_shadow.drain()
        return resp, served

    resp_on, served_s = run(timed())
    assert served_s < 0.9  # the serve did not wait the 1.0 s embed
    monkeypatch.delenv("LLM_ROUTER_CLS_EMBED")
    cls_embed_shadow.reset()
    e_off = Env(tmp_path / "off")
    resp_off = run(_post(e_off.app, body))
    assert resp_on.status_code == resp_off.status_code
    r_on, r_off = e_on.rows()[0], e_off.rows()[0]
    for k in ("decision", "reason", "step_class", "serve_mode"):
        assert r_on.get(k) == r_off.get(k)
    assert r_on["local_mode"].get("served") == r_off["local_mode"].get("served")
    assert r_on["local_mode"].get("reason") == r_off["local_mode"].get("reason")
    assert not any(k.startswith("cls_emb") for k in r_on)  # the ledger row is untouched
    (rec,) = recs(home)
    assert rec["session_id"] == r_on["session_id"] and rec["text_sha"] == r_on["text_sha"]
    assert rec["cls_emb_source"] == "embed" and rec["rules_decision"] == ("local" if r_on["local_mode"].get("served") else r_on["local_mode"].get("reason") or "claude")


def test_proxy_side_call_and_marker_never_embed(tmp_path, monkeypatch, env):
    home, fake = env
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_MARKER", "on")
    (tmp_path / "m").mkdir()
    e = Env(tmp_path / "m")
    marked = _first_call()
    marked["messages"][0]["content"] = [{"type": "text", "text": "[[llm-router:claude-only]]\nplease fix it"}]
    side = _first_call()
    side.pop("tools")
    _post_and_drain(e, marked)
    _post_and_drain(e, side)
    assert fake.bodies == [] and recs(home) == []
    _post_and_drain(e, turn_body(["control prompt with no marker"], loops=0))  # the control: this one does embed
    assert len(recs(home)) == 1
