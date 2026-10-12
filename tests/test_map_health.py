from __future__ import annotations

import json

import pytest

from llm_router import attempt_log, map_health as mh, map_reader, resource_map as rm
from tests.test_resource_map import make_inputs, row

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "llm-home"))
    return tmp_path


def _local(name="qwen3.5:latest"):
    return {"resource": f"ollama/{name}", "kind": "local", "chain_id": f"ollama/{name}",
            "status": "connected", "reason": "ok", "capabilities": ["generate"]}


def _api(var="OPENAI_API_KEY", provider="openai"):
    return {"resource": f"api/{var}", "kind": "api", "chain_id": f"{provider}/api",
            "status": "connected", "reason": "key", "capabilities": ["generate"],
            "key": {"provider": provider}}


def _sub():
    return {"resource": "claude", "kind": "subscription", "chain_id": "anthropic/claude-subscription",
            "status": "connected", "capabilities": ["generate"]}


def _map(*rows, gen=T0):
    return {"schema": rm.SCHEMA, "generated_at": rm._iso(gen), "policy": "balanced", "resources": list(rows)}


def test_state_machine_enters_on_three_rolling_failures_not_two_or_spread():
    r = _local()
    assert mh.record(r, False, T0) is None
    assert r["status"] == "connected"
    assert mh.record(r, False, T0 + 10) is None
    assert r["status"] == "connected"
    assert mh.record(r, False, T0 + 20) == "enter"
    assert r["status"] == mh.STATE
    assert r["unavailable_until"] == T0 + 320
    r2 = _local()
    assert mh.record(r2, False, T0) is None
    assert mh.record(r2, False, T0 + 61) is None
    assert mh.record(r2, False, T0 + 122) is None
    assert r2["status"] == "connected"


def test_state_stragglers_and_success_do_not_clear_window_failures():
    r = _local()
    mh.record(r, False, T0)
    mh.record(r, True, T0 + 1)
    assert len(r["health"]["failures"]) == 1
    mh.record(r, False, T0 + 2)
    assert mh.record(r, False, T0 + 3) == "enter"
    before = json.dumps(r, sort_keys=True)
    assert mh.record(r, False, T0 + 4) is None
    assert json.dumps(r, sort_keys=True) == before


def test_reader_excludes_only_temporarily_unavailable_exact_model(tmp_path):
    data = _map(_local("a:1"), _local("b:1"))
    row(data, "ollama/a:1")["status"] = mh.STATE
    row(data, "ollama/a:1")["unavailable_until"] = T0 + 300
    rm.write_map(data, tmp_path / "m.json")
    v = map_reader.load_view(tmp_path / "m.json", now=T0)
    assert v is not None
    assert map_reader.apply_to_chain(["ollama/a:1", "ollama/b:1"], view=v) == ["ollama/b:1"]
    assert map_reader.apply_to_chain(["ollama/a:1"], view=v) == []


def test_try_leave_only_after_expiry_and_probe_success_starts_probation():
    r = _local()
    mh.record(r, False, T0)
    mh.record(r, False, T0 + 1)
    mh.record(r, False, T0 + 2)
    assert not mh.try_leave(r, T0 + 100, True)
    assert not mh.try_leave(r, T0 + 302, False)
    assert r["status"] == mh.STATE
    assert mh.try_leave(r, T0 + 303, True)
    assert r["status"] == "connected"
    assert r["health"]["probation_until"] == T0 + 423
    assert r["health"]["probation_successes"] == 0


def test_api_build_list_probe_false_stays_unavailable_true_leaves(tmp_path):
    old = _map(_api())
    a = row(old, "api/OPENAI_API_KEY")
    for t in (T0, T0 + 1, T0 + 2):
        mh.record(a, False, t)
    inp = make_inputs(tmp_path, env={"OPENAI_API_KEY": "x"}, models=())
    inp.now = T0 + 400
    inp.previous = old
    inp.list_ok = lambda r: False
    data = rm.build(inp)
    assert row(data, "api/OPENAI_API_KEY")["status"] == mh.STATE
    inp.previous = data
    inp.list_ok = lambda r: True
    left = rm.build(inp)
    assert row(left, "api/OPENAI_API_KEY")["status"] == "connected"
    assert row(left, "api/OPENAI_API_KEY")["health"]["probation_successes"] == 0


def test_probation_failure_doubles_backoff_to_cap_and_successes_reset():
    r = _local()
    for i in range(3):
        mh.record(r, False, T0 + i)
    since = r["health"]["since"]
    for expected in (600, 1200, 2400, 3600, 3600):
        now = r["unavailable_until"]
        assert mh.try_leave(r, now, True)
        assert mh.record(r, False, now + 1) == "reenter"
        assert r["health"]["backoff_s"] == expected
        assert r["health"]["since"] == since
    now = r["unavailable_until"]
    assert mh.try_leave(r, now, True)
    assert mh.record(r, True, now + 1) is None
    assert mh.record(r, True, now + 2) is None
    assert mh.record(r, True, now + 3) == "probation_end"
    assert r["health"]["backoff_s"] == 0
    assert r["health"]["since"] is None
    assert r["health"]["probation_until"] is None


def test_lapsed_probation_counts_next_failure_normally():
    r = _local()
    for i in range(3):
        mh.record(r, False, T0 + i)
    mh.try_leave(r, r["unavailable_until"], True)
    assert mh.record(r, False, r["health"]["probation_until"]) is None
    assert r["status"] == "connected"
    assert len(r["health"]["failures"]) == 1


def test_long_alternating_failures_do_not_enter():
    r = _local()
    events = []
    for i in range(200):
        events.append(mh.record(r, i % 2 == 1, T0 + i * 300))
        assert r["status"] == "connected"
    assert not [e for e in events if e]
    assert r["status"] == "connected"


def test_three_outages_lifecycle_and_chain_filters(tmp_path):
    r = _local()
    enters = leaves = ends = filtered = 0
    now = T0
    for _ in range(3):
        for i in range(3):
            if mh.record(r, False, now + i) == "enter":
                enters += 1
        data = _map(json.loads(json.dumps(r)))
        rm.write_map(data, tmp_path / "m.json")
        for _ in range(20):
            v = map_reader.load_view(tmp_path / "m.json", now=now)
            filtered += map_reader.apply_to_chain([r["chain_id"]], view=v) == []
        if mh.try_leave(r, r["unavailable_until"], True):
            leaves += 1
        probation_until = r["health"]["probation_until"]
        for i in range(3):
            if mh.record(r, True, probation_until - 3 + i) == "probation_end":
                ends += 1
        now += 1000
    assert (enters, leaves, ends, filtered) == (3, 3, 3, 60)


def test_absent_after_24h_boundary():
    r = _local()
    for i in range(3):
        mh.record(r, False, T0 + i)
    since = r["health"]["since"]
    mh.mark_absent_for_compile(r, since + 23 * 3600 + 59 * 60)
    assert not r["health"]["absent_for_compile"]
    mh.mark_absent_for_compile(r, since + mh.ABSENT_AFTER_S)
    assert r["health"]["absent_for_compile"] is True


def test_carry_forward_deep_copies_and_drops_vanished(tmp_path):
    old = rm.build(make_inputs(tmp_path, models=("qwen3.5:latest", "nimble:9b")))
    q = row(old, "ollama/qwen3.5:latest")
    for i in range(3):
        mh.record(q, False, T0 + i)
    inp = make_inputs(tmp_path, models=("qwen3.5:latest",), policy="local-first")
    inp.now = T0 + 10
    inp.previous = old
    data = rm.build(inp)
    q2 = row(data, "ollama/qwen3.5:latest")
    assert q2["status"] == mh.STATE
    assert q2["health"] == q["health"] and q2["health"] is not q["health"]
    assert q2["unavailable_until"] == q["unavailable_until"]
    assert q2["effective_priority"] is None
    assert all(r["resource"] != "ollama/nimble:9b" for r in data["resources"])


def test_failed_probe_stays_unavailable_and_free_probe_uses_default_body(tmp_path):
    old = rm.build(make_inputs(tmp_path, models=("qwen3.5:latest",)))
    q = row(old, "ollama/qwen3.5:latest")
    for i in range(3):
        mh.record(q, False, T0 + i)
    calls = []
    inp = make_inputs(tmp_path, models=("qwen3.5:latest",))
    inp.now = T0 + 400
    inp.previous = old

    def post(url, body):
        calls.append((url, body))
        if url.endswith("/api/tags"):
            return None
        if url.endswith("/api/show"):
            return {"capabilities": ["completion"]}
        if url.endswith("/api/generate"):
            return {}
        return None

    inp.http_post = post
    data = rm.build(inp)
    assert row(data, "ollama/qwen3.5:latest")["status"] == mh.STATE
    assert calls[-1][0].endswith("/api/generate")
    assert calls[-1][1] == {"model": "qwen3.5:latest", "prompt": "hi", "stream": False,
                            "options": {"num_predict": 1}}


def test_record_outcome_persists_without_touching_generated_at_and_noop_cases(tmp_path):
    p = tmp_path / "resource_map.json"
    data = _map(_local(), _api(), _sub())
    rm.write_map(data, p)
    gen = rm.load_map(p)["generated_at"]
    assert mh.record_outcome("ollama/qwen3.5:latest", False, now=T0, path=p) is None
    assert mh.record_outcome("ollama/qwen3.5:latest", False, now=T0 + 1, path=p) is None
    assert mh.record_outcome("ollama/qwen3.5:latest", False, now=T0 + 2, path=p) == "enter"
    got = rm.load_map(p)
    assert got["generated_at"] == gen
    assert row(got, "ollama/qwen3.5:latest")["status"] == mh.STATE
    before = p.stat().st_mtime_ns
    assert mh.record_outcome("unknown/model", False, now=T0, path=p) is None
    assert p.stat().st_mtime_ns == before
    missing = tmp_path / "missing.json"
    assert mh.record_outcome("ollama/qwen3.5:latest", False, now=T0, path=missing) is None
    assert not missing.exists()


def test_record_outcome_api_fallback_and_subscription_ignored(tmp_path):
    p = tmp_path / "resource_map.json"
    rm.write_map(_map(_api(), _sub()), p)
    assert mh.record_outcome("openai/gpt-4o-mini", False, now=T0, path=p) is None
    assert mh.record_outcome("openai/gpt-4o-mini", False, now=T0 + 1, path=p) is None
    assert mh.record_outcome("openai/gpt-4o-mini", False, now=T0 + 2, path=p) == "enter"
    assert row(rm.load_map(p), "api/OPENAI_API_KEY")["status"] == mh.STATE
    before = json.dumps(rm.load_map(p), sort_keys=True)
    assert mh.record_outcome("anthropic/claude-subscription", False, now=T0, path=p) is None
    assert json.dumps(rm.load_map(p), sort_keys=True) == before


def test_tracker_hooks_record_model_health_but_rate_limit_does_not(monkeypatch):
    from llm_router.health import HealthTracker

    seen = []
    monkeypatch.setattr(mh, "record_outcome", lambda model, ok: seen.append((model, ok)))
    monkeypatch.setattr(HealthTracker, "write_snapshot", lambda self: None)
    t = HealthTracker()
    t.record_failure("ollama", model="ollama/a")
    t.record_failure("ollama", model="ollama/a")
    t.record_failure("ollama", model="ollama/a")
    t.record_success("ollama", model="ollama/a")
    t.record_rate_limit("ollama", cooldown_seconds=1)
    assert seen == [("ollama/a", False), ("ollama/a", False),
                    ("ollama/a", False), ("ollama/a", True)]


def test_attempt_log_hooks_ok_timeout_only(monkeypatch):
    seen = []
    monkeypatch.setattr(mh, "record_outcome", lambda model, ok: seen.append((model, ok)))
    attempt_log.record("m", attempt_log.OK, 1)
    attempt_log.record("m", attempt_log.TIMEOUT, 1)
    attempt_log.record("m", attempt_log.EMPTY, 1)
    assert seen == [("m", True), ("m", False)]

def test_clean_success_does_not_rewrite(tmp_path):
    p = tmp_path / "resource_map.json"
    rm.write_map(_map(_local("a")), p)
    before = p.stat().st_mtime_ns
    assert mh.record_outcome("ollama/a", True, now=T0, path=p) is None
    assert p.stat().st_mtime_ns == before


def test_free_probe_runs_once_for_recent_failures(tmp_path):
    old = _map(_local())
    mh.record(old["resources"][0], False, T0)
    inp = make_inputs(tmp_path, models=("qwen3.5:latest",))
    inp.now = T0 + 1
    inp.previous = old
    calls = []
    inp.probe_local = lambda base, name: calls.append((base, name)) or False
    data = rm.build(inp)
    assert len(calls) == 1
    assert len(row(data, "ollama/qwen3.5:latest")["health"]["failures"]) == 2
