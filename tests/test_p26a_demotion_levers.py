"""P2.6-a (PLAN v16, D-140): automatic demotion levers with recovery.

Three levers, one breaker (``quality_breaker``), one state file (``quality_breaker.json``):

* ``proxy_haiku``: opened by the ``proxy/haiku_guard.py`` triggers, closed by the breaker's own
  half-open probe over Haiku-served turns after the 24 h cooldown. The owner override file
  (``tier_overrides.json``) stays one-way and beats the breaker.
* ``techop_local``: the lever key and its trigger/recovery. NOT live: ``technical_ops.py`` has no
  call site (P2.1 is blocked on P2.0).
* local code: already gated by ``local_agent/capability.BreakerCache`` on ``("proxy", task)`` and by
  ``zero_claude_edit`` on ``("zero_claude_edit", "code")``; only trigger and recovery are tested here.

Fixtures are synthetic ids, models and timestamps; no prompt text.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from llm_router import paths
from llm_router import quality_breaker as qb
from llm_router.local_agent.capability import BreakerCache
from llm_router.proxy import haiku_guard as hg
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt
from tests.test_proxy_haiku_guard import SONNET as G_SONNET, _redo_ledger, _retry_ledger, _sha, _turn, _yaml
from tests.test_proxy_tiers import HAIKU, SONNET, Upstream, _app, _first, _no_system_reminders, _post, _req

COOLDOWN = qb.cooldown_seconds()
T0 = 1_791_400_000.0  # the trip clock


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR", str(tmp_path / "no_audits"))
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS", str(tmp_path / "no_shadow.jsonl"))


def _quiet(_policy_unused=None):
    return []


def _state() -> dict:
    return json.loads(paths.state_path("quality_breaker.json").read_text())["classes"]


def _haiku_class() -> dict:
    return _state()["proxy_haiku:haiku"]


def _policy(tmp_path, rewrite=True):
    return pt.ClaudeTierPolicy.load(_yaml(tmp_path, rewrite))


def _trip(tmp_path, policy=None, *, now=T0, rows=None):
    """Trip the guard through ``run_once`` (the real path) on a 6/30 redo ledger."""
    policy = policy or _policy(tmp_path)
    ev = hg.run_once(policy, now=now, read_rows=lambda: rows if rows is not None else _redo_ledger(30, 6, now),
                     notify=lambda r, e: None)
    return policy, ev


def _probe_rows(start: float, n: int, redone: int) -> list[dict]:
    """``n`` Haiku-served human turns from ``start`` on, one per session, ``redone`` of them redone
    (an escalation in the next human turn); the rest are followed by three more human turns, so
    their redo window is closed and they count as used."""
    rows = []
    for i in range(n):
        t = start + 60.0 * (i + 1)
        sid = f"p{i}"
        rows.append(_turn(sid, t))
        if i < redone:
            rows.append(_turn(sid, t + 20.0, model=G_SONNET, reason="escalation"))
        else:
            rows += [_turn(sid, t + 20.0 * (k + 1), model=G_SONNET, reason="config_pinned") for k in range(3)]
    return rows


def _decide(now: float, rows: list[dict]):
    return hg.breaker_decision(now=now, read_rows=lambda: rows)


def _into_half_open(tmp_path):
    """Tripped at T0, cooldown elapsed: the first evaluation after it enters half_open."""
    policy, _ = _trip(tmp_path)
    t1 = T0 + COOLDOWN
    d = _decide(t1, [])
    assert d.allowed and d.state == qb.HALF_OPEN
    return policy, t1


# ── proxy_haiku: trigger ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("rows_for", "trips"), [
    (lambda now: _redo_ledger(30, 5, now), True),    # 5/30 = 16.7% > 15% at n = 30
    (lambda now: _redo_ledger(30, 4, now), False),   # 4/30 = 13.3%, one below
    (lambda now: _retry_ledger(100, 2, now), True),  # tier_retry 2/100 > 1% at n = 100
    (lambda now: _retry_ledger(100, 1, now), False),
])
def test_trigger_crossing_the_guard_threshold_demotes_and_one_below_does_not(tmp_path, rows_for, trips):
    policy = _policy(tmp_path)
    rows = rows_for(T0)
    ev = hg.run_once(policy, now=T0, read_rows=lambda: rows, notify=lambda r, e: None)
    if trips:
        assert ev["action"] == "trip" and ev["tripped"]
        assert policy.haiku_rewrite is False and policy.haiku_breaker["state"] == qb.OPEN
        assert _haiku_class()["state"] == qb.OPEN and _haiku_class()["opened_at"] == T0
        assert qb.class_state("proxy_haiku", "haiku") == qb.OPEN
    else:
        assert ev["action"] == "ok" and not ev["tripped"]
        assert policy.haiku_rewrite is True
        assert qb.class_state("proxy_haiku", "haiku") == qb.CLOSED


def test_a_trip_leaves_the_override_file_and_the_yaml_alone_and_survives_a_restart(tmp_path):
    y = _yaml(tmp_path, True)
    before = _sha(y)
    policy = pt.ClaudeTierPolicy.load(y)
    _trip(tmp_path, policy, now=time.time())  # the real clock: a restart evaluates the breaker "now"
    assert not hg.override_path().exists()
    assert _sha(y) == before
    assert pt.ClaudeTierPolicy.load(y).haiku_rewrite is False  # a proxy restart keeps the demotion


def test_a_second_run_while_open_does_not_trip_again(tmp_path):
    policy, _ = _trip(tmp_path)
    notes = []
    again = hg.run_once(policy, now=T0 + 3600, read_rows=lambda: _redo_ledger(30, 6, T0 + 3600),
                        notify=lambda r, e: notes.append(r))
    assert again["action"] == "already_off" and notes == []
    assert _haiku_class()["opened_at"] == T0  # the cooldown did not restart


# ── proxy_haiku: recovery ────────────────────────────────────────────────────


def test_no_recovery_before_the_cooldown(tmp_path):
    policy, _ = _trip(tmp_path)
    d = _decide(T0 + COOLDOWN - 1, [])
    assert d.allowed is False and d.state == qb.OPEN and "cooldown" in d.reason
    hg.apply_override(policy, now=T0 + COOLDOWN - 1)
    assert policy.haiku_rewrite is False


def test_cooldown_elapsed_switches_the_rewrite_on_as_a_probe(tmp_path):
    policy, t1 = _into_half_open(tmp_path)
    hg.apply_override(policy, now=t1)
    assert policy.haiku_rewrite is True and policy.haiku_breaker is None
    assert qb.class_state("proxy_haiku", "haiku") == qb.HALF_OPEN


def test_probe_pending_below_probe_size_stays_half_open(tmp_path):
    _into_half_open(tmp_path)
    t1 = T0 + COOLDOWN
    d = _decide(t1 + 3600, _probe_rows(t1, qb.probe_size() - 1, 0))
    assert d.state == qb.HALF_OPEN and d.allowed and "pending" in d.reason


def test_a_successful_probe_closes_the_lever_and_stamps_recovery(tmp_path):
    policy, t1 = _into_half_open(tmp_path)
    t2 = t1 + 3600
    d = _decide(t2, _probe_rows(t1, qb.probe_size(), 0))
    assert d.state == qb.CLOSED and d.allowed and "probe passed" in d.reason
    assert _haiku_class()["recovered_at"] == t2 and _haiku_class()["opened_at"] is None
    assert qb.recovered_at("proxy_haiku", "haiku") == t2


def test_a_failed_probe_reopens_and_restarts_the_cooldown(tmp_path):
    policy, t1 = _into_half_open(tmp_path)
    t2 = t1 + 3600
    d = _decide(t2, _probe_rows(t1, qb.probe_size(), qb.probe_size()))
    assert d.state == qb.OPEN and d.allowed is False and "probe failed" in d.reason
    assert _haiku_class()["opened_at"] == t2 and "recovered_at" not in _haiku_class()
    assert _decide(t2 + COOLDOWN - 1, []).allowed is False  # a full cooldown again
    assert _decide(t2 + COOLDOWN, []).state == qb.HALF_OPEN


@pytest.mark.parametrize(("redone", "passes"), [(2, True), (3, False)])  # threshold 0.5: 2/5 passes, 3/5 does not
def test_the_probe_uses_the_breakers_own_threshold(tmp_path, redone, passes):
    _, t1 = _into_half_open(tmp_path)
    d = _decide(t1 + 3600, _probe_rows(t1, qb.probe_size(), redone))
    assert (d.state == qb.CLOSED) is passes


def test_probe_ignores_units_from_before_half_open_and_open_windows_are_not_a_pass(tmp_path):
    _, t1 = _into_half_open(tmp_path)
    old = _probe_rows(t1 - 7200, qb.probe_size(), 0)  # served before the probe started
    assert _decide(t1 + 3600, old).state == qb.HALF_OPEN
    fresh_open = [_turn(f"o{i}", t1 + 60 * (i + 1)) for i in range(qb.probe_size())]  # window still open
    d = _decide(t1 + 600, fresh_open)
    assert d.state == qb.HALF_OPEN and "inconclusive" in d.reason  # unknown is never read as a pass


def test_the_loop_restores_the_live_policy_through_a_full_cycle(tmp_path, monkeypatch):
    policy, _ = _trip(tmp_path)
    t1 = T0 + COOLDOWN
    ledger_rows: list[dict] = []
    monkeypatch.setattr("llm_router.proxy.ledger.read_rows", lambda *a, **k: list(ledger_rows))
    hg.apply_override(policy, now=t1)  # tick 1: cooldown elapsed -> half-open, rewrite on
    assert policy.haiku_rewrite is True
    ledger_rows += _probe_rows(t1, qb.probe_size(), 0)
    hg.apply_override(policy, now=t1 + 3600)  # tick 2: probe passed
    assert policy.haiku_rewrite is True and qb.class_state("proxy_haiku", "haiku") == qb.CLOSED
    assert not hg.override_path().exists()


def test_old_evidence_cannot_retrip_after_a_recovery_but_new_evidence_can(tmp_path):
    policy, t1 = _into_half_open(tmp_path)
    t2 = t1 + 3600
    assert _decide(t2, _probe_rows(t1, qb.probe_size(), 0)).state == qb.CLOSED
    hg.apply_override(policy, now=t2)  # the loop's tick restores the live policy
    assert policy.haiku_rewrite is True
    old = _redo_ledger(30, 6, T0)  # the evidence that tripped it, still inside the 7-day window
    ev = hg.run_once(policy, now=t2 + 60, read_rows=lambda: old, notify=lambda r, e: None)
    assert ev["action"] == "ok" and qb.class_state("proxy_haiku", "haiku") == qb.CLOSED
    t3 = t2 + 7200
    new = _redo_ledger(30, 6, t3)  # all of it after the recovery
    ev = hg.run_once(policy, now=t3, read_rows=lambda: new, notify=lambda r, e: None)
    assert ev["action"] == "trip" and qb.class_state("proxy_haiku", "haiku") == qb.OPEN


def test_recovery_never_turns_on_a_rewrite_the_yaml_left_off(tmp_path):
    policy = _policy(tmp_path, rewrite=False)
    qb.trip("proxy_haiku", "haiku", reason="x", now=T0 - 2 * COOLDOWN)
    assert hg.apply_override(policy) is False
    assert policy.haiku_rewrite is False and getattr(policy, "haiku_breaker", None) is None


# ── proxy_haiku: the owner override wins and is never touched ────────────────


def _owner_override(reason="owner"):
    p = hg.override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"haiku_rewrite": False, "reason": reason, "ts": T0}) + "\n")
    return p


def test_owner_override_beats_a_recovered_breaker_and_is_never_rewritten(tmp_path):
    p = _owner_override()
    raw = p.read_bytes()
    policy, _ = _trip(tmp_path)
    assert policy.haiku_override["reason"] == "owner"  # the policy already carries the override
    t1 = T0 + COOLDOWN
    for _ in range(3):
        hg.apply_override(policy, now=t1)
    assert policy.haiku_rewrite is False
    # even a breaker that has fully recovered cannot turn the rewrite on past the file
    qb.trip("proxy_haiku", "haiku", reason="x", now=T0 - 2 * COOLDOWN)
    assert _decide(t1, []).state == qb.HALF_OPEN
    assert _decide(t1 + 3600, _probe_rows(t1, qb.probe_size(), 0)).state == qb.CLOSED
    fresh = _yaml(tmp_path, True)
    assert pt.ClaudeTierPolicy.load(fresh).haiku_rewrite is False
    assert p.exists() and p.read_bytes() == raw


def test_a_full_breaker_cycle_never_creates_the_override_file(tmp_path):
    _, t1 = _into_half_open(tmp_path)
    _decide(t1 + 3600, _probe_rows(t1, qb.probe_size(), 0))
    assert not hg.override_path().exists()


def test_override_cannot_be_used_to_turn_the_rewrite_on_even_with_the_breaker_open(tmp_path):
    p = hg.override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"haiku_rewrite": True}))
    qb.trip("proxy_haiku", "haiku", reason="x", now=time.time())
    assert _policy(tmp_path).haiku_rewrite is False  # the breaker, not the file, turned it off


# ── proxy_haiku: fail-open ───────────────────────────────────────────────────


def _corrupt_state():
    p = paths.state_path("quality_breaker.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    return p


def test_corrupt_breaker_state_fails_open(tmp_path):
    _corrupt_state()
    policy = _policy(tmp_path)
    assert policy.haiku_rewrite is True  # load did not raise and did not demote
    assert qb.class_state("proxy_haiku", "haiku") == qb.CLOSED
    assert hg.breaker_decision(now=T0, read_rows=lambda: []) is None
    assert hg.apply_override(policy) is False and policy.haiku_rewrite is True
    # the guard itself still trips (and repairs the file) instead of raising
    ev = hg.run_once(policy, now=T0, read_rows=lambda: _redo_ledger(30, 6, T0), notify=lambda r, e: None)
    assert ev["action"] == "trip" and policy.haiku_rewrite is False
    assert qb.class_state("proxy_haiku", "haiku") == qb.OPEN


def test_wrong_shaped_breaker_state_fails_open(tmp_path):
    p = paths.state_path("quality_breaker.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"classes": ["not", "an", "object"]}))
    assert qb.class_state("proxy_haiku", "haiku") == qb.CLOSED
    assert qb.trip("proxy_haiku", "haiku", reason="x", now=T0) is True


def test_an_unreadable_probe_never_raises_and_reads_allowed(tmp_path):
    qb.trip("proxy_haiku", "haiku", reason="x", now=T0 - 2 * COOLDOWN)

    def boom(**_kw):
        raise RuntimeError("ledger gone")

    d = qb.fed_decision("proxy_haiku", "haiku", units_fn=boom, now=T0)
    assert d.allowed is True and "fail-open" in d.reason


def test_an_unwritable_state_does_not_stop_the_live_demotion(tmp_path, monkeypatch):
    monkeypatch.setattr(qb, "_write_state", lambda state: False)
    policy, ev = _trip(tmp_path)
    assert ev["action"] == "trip" and policy.haiku_rewrite is False  # in-process demotion holds
    assert qb.class_state("proxy_haiku", "haiku") == qb.CLOSED  # nothing persisted, nothing raised


# ── proxy_haiku: the door (real proxy, fake upstream) ────────────────────────


@pytest.fixture
def simple(monkeypatch):
    from llm_router.proxy import backends as pb

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "query", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


async def _second_call_model(tmp_path) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    up = Upstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True})
    assert (await _post(app, _first())).status_code == 200
    assert (await _post(app, _no_system_reminders(_req()))).status_code == 200
    return json.loads(up.requests[1].content)["model"]


async def test_door_open_breaker_serves_sonnet_and_a_probe_serves_haiku(tmp_path, simple):
    assert await _second_call_model(tmp_path / "a") == HAIKU  # control: no breaker state, rewrite on
    qb.trip("proxy_haiku", "haiku", reason="x", now=time.time())
    assert await _second_call_model(tmp_path / "b") == SONNET  # demoted at proxy start
    # cooldown elapsed: the proxy starts half-open and the Haiku traffic is the probe
    paths.state_path("quality_breaker.json").unlink()
    qb.trip("proxy_haiku", "haiku", reason="x", now=time.time() - COOLDOWN - 1)
    assert await _second_call_model(tmp_path / "c") == HAIKU
    assert qb.class_state("proxy_haiku", "haiku") == qb.HALF_OPEN


async def test_door_owner_override_beats_the_breaker_in_the_live_proxy(tmp_path, simple):
    qb.trip("proxy_haiku", "haiku", reason="x", now=time.time() - COOLDOWN - 1)  # would be a probe
    _owner_override()
    assert await _second_call_model(tmp_path) == SONNET


async def test_door_a_guard_trip_in_the_running_proxy_demotes_the_next_turn(tmp_path, simple):
    now = time.time()

    def run(policy):
        hg.run_once(policy, now=now, read_rows=lambda: _redo_ledger(30, 6, now), notify=lambda r, e: None)

    up = Upstream()
    import yaml
    from tests.test_proxy_tiers import _raw_policy
    raw = _raw_policy()
    raw["haiku_rewrite"] = True
    raw["stickiness"]["switch_after_first_call"] = True
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(raw))
    import httpx
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_ON,
                         tier_policy=str(tmp_path / "t.yaml"), ledger_path=tmp_path / "proxy_calls.jsonl")
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(up)), haiku_guard_run=run)
    async with app.router.lifespan_context(app):
        for _ in range(300):
            if qb.class_state("proxy_haiku", "haiku") == qb.OPEN:
                break
            await asyncio.sleep(0.01)
        assert qb.class_state("proxy_haiku", "haiku") == qb.OPEN
        assert (await _post(app, _first())).status_code == 200
        assert (await _post(app, _no_system_reminders(_req()))).status_code == 200
    assert json.loads(up.requests[1].content)["model"] == SONNET
    assert not hg.override_path().exists()


# ── techop_local: lever key, trigger, recovery (not live: no call site) ──────


def _unit(lever: str, task: str, outcome: str, ts: float, model: str = "ollama/qwen") -> dict:
    return {"session_id": "s", "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "kind": "k", "lever": lever, "task_type": task, "model": model, "outcome": outcome,
            "signal": "test"}


def _fn(units):
    return lambda **_kw: list(units)


def test_techop_local_lever_key_is_the_literal_string_the_units_will_carry():
    assert (qb.LEVER_TECHOP_LOCAL, qb.TASK_TECHOP_LOCAL) == ("techop_local", "tech_op")
    assert qb.class_key(qb.LEVER_TECHOP_LOCAL, qb.TASK_TECHOP_LOCAL) == "techop_local:tech_op"


@pytest.mark.parametrize(("failing", "opens"), [(20, True), (19, False)])  # the existing min_n = 20
def test_techop_local_trigger_at_the_existing_threshold(failing, opens):
    units = [_unit("techop_local", "tech_op", "redo", 1000.0 + i) for i in range(failing)]
    d = qb.should_route(qb.LEVER_TECHOP_LOCAL, qb.TASK_TECHOP_LOCAL, units_fn=_fn(units), now=2000.0)
    assert (d.allowed, d.state) == ((False, qb.OPEN) if opens else (True, qb.CLOSED))
    assert d.key == "techop_local:tech_op"


def test_techop_local_trip_is_isolated_from_other_levers():
    units = [_unit("techop_local", "tech_op", "redo", 1000.0 + i) for i in range(20)]
    qb.should_route("techop_local", "tech_op", units_fn=_fn(units), now=2000.0)
    assert qb.should_route("proxy", "tech_op", units_fn=_fn(units), now=2000.0).allowed is True
    assert qb.should_route("techop_local", "code", units_fn=_fn(units), now=2000.0).allowed is True


def test_techop_local_recovery_needs_cooldown_and_a_passing_probe():
    bad = [_unit("techop_local", "tech_op", "redo", 1000.0 + i) for i in range(20)]
    qb.should_route("techop_local", "tech_op", units_fn=_fn(bad), now=2000.0)
    early = qb.should_route("techop_local", "tech_op", units_fn=_fn(bad), now=2000.0 + COOLDOWN - 1)
    assert early.state == qb.OPEN and early.allowed is False
    t1 = 2000.0 + COOLDOWN
    half = qb.should_route("techop_local", "tech_op", units_fn=_fn(bad), now=t1)
    assert half.state == qb.HALF_OPEN and half.allowed is True
    good = [_unit("techop_local", "tech_op", "used", t1 + 1 + i) for i in range(qb.probe_size())]
    ok = qb.should_route("techop_local", "tech_op", units_fn=_fn(bad + good), now=t1 + 60)
    assert ok.state == qb.CLOSED and ok.allowed is True


def test_techop_local_failed_probe_stays_demoted():
    bad = [_unit("techop_local", "tech_op", "redo", 1000.0 + i) for i in range(20)]
    qb.should_route("techop_local", "tech_op", units_fn=_fn(bad), now=2000.0)
    t1 = 2000.0 + COOLDOWN
    qb.should_route("techop_local", "tech_op", units_fn=_fn(bad), now=t1)
    worse = [_unit("techop_local", "tech_op", "redo", t1 + 1 + i) for i in range(qb.probe_size())]
    d = qb.should_route("techop_local", "tech_op", units_fn=_fn(bad + worse), now=t1 + 60)
    assert d.state == qb.OPEN and d.allowed is False
    again = qb.should_route("techop_local", "tech_op", units_fn=_fn(bad + worse), now=t1 + 61)
    assert again.allowed is False  # cooldown restarted


def test_techop_local_corrupt_state_fails_open():
    _corrupt_state()
    bad = [_unit("techop_local", "tech_op", "used", 1000.0 + i) for i in range(5)]
    d = qb.should_route("techop_local", "tech_op", units_fn=_fn(bad), now=2000.0)
    assert d.allowed is True and d.state == qb.CLOSED


# ── local code lever: (proxy, task) via BreakerCache and (zero_claude_edit, code) ──


def _cache(units, clock):
    return BreakerCache(fn=lambda lever, task: qb.should_route(lever, task, units_fn=_fn(units()), now=clock[0]),
                        ttl_s=0.0)


@pytest.mark.parametrize(("failing", "allowed"), [(20, False), (19, True)])
def test_local_code_proxy_lever_trigger_through_the_capability_cache(failing, allowed):
    units = [_unit("proxy", "code", "redo", 1000.0 + i) for i in range(failing)]
    ok, why = asyncio.run(_cache(lambda: units, [2000.0]).allows("code"))
    assert ok is allowed
    assert ("OPENED" in why) is (not allowed)


def test_local_code_proxy_lever_recovers_through_the_capability_cache():
    units = [_unit("proxy", "code", "redo", 1000.0 + i) for i in range(20)]
    clock = [2000.0]
    cache = _cache(lambda: units, clock)
    assert asyncio.run(cache.allows("code"))[0] is False
    clock[0] = 2000.0 + COOLDOWN - 1
    assert asyncio.run(cache.allows("code"))[0] is False  # no recovery before the cooldown
    clock[0] = 2000.0 + COOLDOWN
    assert asyncio.run(cache.allows("code"))[0] is True  # half-open: the probe routes
    t1 = clock[0]
    units += [_unit("proxy", "code", "redo", t1 + 1 + i) for i in range(qb.probe_size())]
    clock[0] = t1 + 60
    assert asyncio.run(cache.allows("code"))[0] is False  # failed probe stays demoted
    t2 = clock[0] + COOLDOWN
    clock[0] = t2
    assert asyncio.run(cache.allows("code"))[0] is True
    units += [_unit("proxy", "code", "used", t2 + 1 + i) for i in range(qb.probe_size())]
    clock[0] = t2 + 60
    ok, why = asyncio.run(cache.allows("code"))
    assert ok is True and "CLOSED" in why  # passed probe restores


@pytest.mark.parametrize(("failing", "allowed"), [(20, False), (19, True)])
def test_local_code_zero_claude_edit_lever_trigger_and_recovery(failing, allowed):
    units = [_unit("zero_claude_edit", "code", "discarded", 1000.0 + i) for i in range(failing)]
    d = qb.should_route("zero_claude_edit", "code", units_fn=_fn(units), now=2000.0)
    assert d.allowed is allowed
    if allowed:
        return
    t1 = 2000.0 + COOLDOWN
    assert qb.should_route("zero_claude_edit", "code", units_fn=_fn(units), now=t1).state == qb.HALF_OPEN
    good = [_unit("zero_claude_edit", "code", "used", t1 + 1 + i) for i in range(qb.probe_size())]
    d = qb.should_route("zero_claude_edit", "code", units_fn=_fn(units + good), now=t1 + 60)
    assert d.state == qb.CLOSED and d.allowed is True


def test_local_code_levers_do_not_share_a_class():
    units = [_unit("zero_claude_edit", "code", "redo", 1000.0 + i) for i in range(20)]
    assert qb.should_route("zero_claude_edit", "code", units_fn=_fn(units), now=2000.0).allowed is False
    assert qb.should_route("proxy", "code", units_fn=_fn(units), now=2000.0).allowed is True


def test_local_code_corrupt_state_fails_open():
    _corrupt_state()
    ok, why = asyncio.run(_cache(lambda: [], [2000.0]).allows("code"))
    assert ok is True
