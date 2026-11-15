"""PR-A A1: ledger observability fields on serve-on rows only (proxy/obs_fields.py)."""

from __future__ import annotations

import hashlib


from llm_router.proxy import cache_cost, obs_fields
from tests import test_proxy_local_mode as T

Backend, _call, _first_call, _next_turn = T.Backend, T._call, T._first_call, T._next_turn
_ollama, _post, _served_blocks = T._ollama, T._post, T._served_blocks
env = T.env
_overrides_readable = T._overrides_readable

CAP = 2
NEW = ("conv", "pin_cause", "pin_age_steps", "served_model", "pop")


def _bash(i):
    return _ollama("", [_call("Bash", {"command": f"echo step{i}"})])


async def _drive(app, n):
    base, out, prev = _first_call(), [], None
    for _ in range(n):
        r = await _post(app, base if prev is None else _next_turn(base, prev))
        out.append(r)
        blocks = _served_blocks(r)
        prev = {"content": blocks} if blocks else prev
    return base, out


async def test_cap_pin_rows_carry_conv_cause_age_pop_and_served_model(env, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_POP_LABEL", raising=False)
    monkeypatch.delenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", raising=False)
    app = env.app(Backend(*[_bash(i) for i in range(20)]), loop_max_consecutive=CAP)
    base, _ = await _drive(app, CAP + 3)
    rows = env.rows()
    assert [r["reason"] for r in rows[CAP:]] == ["loop_guard", "pinned_claude", "pinned_claude"]
    key = cache_cost.conversation_key(base, rows[0]["session_id"])
    want = hashlib.sha256(key.encode()).hexdigest()[:12]
    assert all(r["conv"] == want for r in rows) and key not in str(rows)
    # served rows: the backend model name, no pin fields
    assert all(r["served_model"] == T.MODEL for r in rows[:CAP])
    assert all("served_model" not in r for r in rows[CAP:])
    assert all("pin_cause" not in r for r in rows[:CAP])
    # the pin row, then every later pinned row repeats the cause; age counts rows since the pin
    assert (rows[CAP]["pin_cause"], rows[CAP]["pin_age_steps"]) == ("loop_guard:cap", 0)
    assert (rows[CAP + 1]["pin_cause"], rows[CAP + 1]["pin_age_steps"]) == ("loop_guard:cap", 1)
    assert (rows[CAP + 2]["pin_cause"], rows[CAP + 2]["pin_age_steps"]) == ("loop_guard:cap", 2)
    assert all(r["pop"].endswith("_main") for r in rows)
    assert all(r["loop_guard_trigger"] == "cap" for r in rows if r["reason"] == "loop_guard")


async def test_repeat_trip_sets_trigger_and_cause(env, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", raising=False)
    same = _ollama("", [_call("Bash", {"command": "same"})])
    app = env.app(Backend(same, same, same), loop_max_consecutive=50)
    await _drive(app, 3)
    rows = [r for r in env.rows() if r["reason"] == "loop_guard"]
    assert rows and all(r["loop_guard_trigger"] == "repeat" for r in rows)
    assert rows[0]["pin_cause"] == "loop_guard:repeat" and rows[0]["pin_age_steps"] == 0


async def test_pop_label_env_valid_and_invalid(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_POP_LABEL", "bench_a1")
    app = env.app(Backend(_bash(0)))
    await _drive(app, 1)
    assert env.rows()[0]["pop"] == "bench_a1"
    assert obs_fields.population({"step_class": "continuation", "session_kind": "organic", "is_main_thread": True}) == "bench_a1"
    monkeypatch.setenv("LLM_ROUTER_POP_LABEL", "Bad Label!")
    assert obs_fields.population({"session_kind": "organic", "is_main_thread": False}) == "organic_sub"


def test_population_rules(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_POP_LABEL", raising=False)
    assert obs_fields.population({"step_class": "side_call", "reason": "claude_marker"}) == "side_call"
    assert obs_fields.population({"step_class": "turn_first", "reason": "claude_marker"}) == "marker"
    assert obs_fields.population({"session_kind": "coordinator", "is_main_thread": True}) == "coordinator_main"
    assert obs_fields.population({"session_kind": "organic", "is_main_thread": False}) == "organic_sub"


async def test_off_mode_rows_have_none_of_the_new_fields(env, tmp_path):
    import httpx
    from llm_router.proxy import server as ps

    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=env.ledger, serve="off", trim="none",
                            model=T.MODEL, warm_up=False)
    app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(env.up)),
                       backend_factory=lambda m: Backend(_bash(0)))
    await _post(app, _first_call())
    rows = env.rows()
    assert rows and not any(k in r for r in rows for k in NEW if k != "served_model")


async def test_obs_failure_never_fails_the_request(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(obs_fields, "add_fields", boom)
    app = env.app(Backend(_bash(0)))
    r = await _post(app, _first_call())
    assert r.status_code == 200 and env.rows()[0]["decision"] == "served"
