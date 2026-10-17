"""The proxy local gate (``llm_router.proxy.local_gate``, ``--local-gate on``).

PLAN v16 P2.1 t5 / P2.10 t3 groundwork (D-195, D-196). The eligibility file is
hand-written in the shape ``record_competence`` writes (``{model: {task_type: {tier:
lane}}}``); that writer is not on main yet. Anthropic is a mocked upstream and the local
model a fake backend; no test reaches the network or a real Ollama. Every test runs under a
tmp ``LLM_ROUTER_HOME`` (promote-gate grandfather file, quality-breaker state, units feed).
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import httpx
import pytest

from llm_router import capability_filter
from llm_router.proxy import ledger, local_gate
from llm_router.proxy import server as ps
from llm_router.proxy.translate import from_ollama

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
MODEL = "ollama/fake:1"
BARE = "fake:1"


def _lane(verdict="eligible", n=120, **kw):
    eligible = verdict == "eligible"
    return {"eligible": eligible, "verdict": verdict, "grade": "eligible" if eligible else "failed",
            "n": n, **kw}


def _elig(*entries):
    """``entries``: ``(model, task_type, tier, lane)``."""
    data: dict = {}
    for model, task, tier, lane in entries:
        data.setdefault(model, {}).setdefault(task, {})[tier] = lane
    return data


def _req_after(tool="Read", inp=None) -> dict:
    """The fixture continuation, its last answered tool call replaced by ``tool``."""
    body = json.loads(FIXTURE.read_text())
    last = [b for m in body["messages"] if m["role"] == "assistant" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_use"][-1]
    last["name"] = tool
    last["input"] = inp or {"file_path": "/work/repo/pkg/mod01.py"}
    return body


def _first_call() -> dict:
    body = json.loads(FIXTURE.read_text())
    body["messages"] = body["messages"][:2]
    return body


def _ollama(content="", calls=None):
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls or []},
            "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 5}


def _call(name, args):
    return {"function": {"name": name, "arguments": args}}


class Backend:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply or _ollama("Looks fine."), exc, []

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        if self.exc:
            raise self.exc
        m, err = from_ollama(self.reply, body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


class Upstream:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"type":"message"}'),
                              headers={"content-type": "application/json"})


class Breaker:
    def __init__(self, allowed=True, exc=None, state=None):
        self.allowed, self.exc, self.state, self.calls = allowed, exc, state, []

    def __call__(self, lever, task_type):
        self.calls.append((lever, task_type))
        if self.exc:
            raise self.exc
        return type("D", (), {"allowed": self.allowed, "state": self.state, "reason": "test"})()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_QUALITY_BREAKER_PATH", str(home / "quality_breaker.json"))
    decision = {"task_type": "code", "complexity": "moderate", "chain_head": [MODEL], "model": MODEL}

    async def _choose(text, pinned, **_kw):
        return dict(decision)

    monkeypatch.setattr(ps, "choose_model", _choose)

    class Env:
        up = Upstream()
        ledger = tmp_path / "proxy_calls.jsonl"
        policy = decision
        elig = home / "local_eligibility.json"
        backends: list = []

        def write(self, data):
            self.elig.write_text(json.dumps(data))

        def promote(self, *models):
            (home / "grandfathered_models.json").write_text(json.dumps({"at": 1, "ids": list(models)}))
            (home / "grandfathered_models.init").write_text("")

        def deps(self, **kw):
            return local_gate.GateDeps(watchdog=lambda: None, **kw)

        def app(self, backend, *, breaker=None, deps=None, gate=True, real_breaker=False, **cfg):
            self.backends = []

            def factory(model):
                self.backends.append(model)
                return backend

            config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=self.ledger, trim="none",
                                    hedge_s=None, warm_up=False, local_gate=gate, **cfg)
            client = httpx.AsyncClient(transport=httpx.MockTransport(self.up))
            deps = deps or self.deps()
            if real_breaker:
                deps.breaker = None
            else:
                deps.breaker = deps.breaker or (lambda lever, task, model, b=breaker or Breaker(): b(lever, task))
            return ps.build_app(config, client=client, backend_factory=factory, local_gate_deps=deps)

        def rows(self):
            return ledger.read_rows(self.ledger)

        def units(self):
            return local_gate.read_units(path=local_gate.units_path())

    e = Env()
    e.up = Upstream()
    e.write(_elig((BARE, "tech_op", "simple", _lane())))
    e.promote(MODEL)
    return e


async def _post(app, body):
    headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=headers)


def _next_turn(body, tool, inp):
    """The request Claude Code sends after running a tool call ``tool(inp)`` (id ``t``)."""
    body = copy.deepcopy(body)
    body["messages"] = [m for m in body["messages"] if m["role"] != "system"]
    body["messages"].append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": tool, "input": inp}]})
    body["messages"].append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]})
    return body


def _gate_row(rows):
    (row,) = rows
    assert row["local_gate"]["reason"] in local_gate.REASONS
    return row


# ── served ────────────────────────────────────────────────────────────────────

async def test_eligible_tech_op_continuation_is_served_locally(env):
    backend = Backend()
    r = await _post(env.app(backend), _req_after("Read"))
    assert r.status_code == 200 and env.up.requests == []
    row = _gate_row(env.rows())
    assert row["decision"] == "served" and row["prev_tool_class"] == "technical_op"
    assert row["local_gate"] == {"lane": "tech_op", "model": MODEL, "reason": "served_local"}
    assert row["model"] == MODEL and len(backend.calls) == 1


async def test_bash_read_only_command_is_a_tech_op_lane(env):
    backend = Backend()
    await _post(env.app(backend), _req_after("Bash", {"command": "git status && ls -la"}))
    assert _gate_row(env.rows())["local_gate"]["lane"] == "tech_op"


async def test_model_comes_from_the_lane_not_the_policy_and_highest_n_wins(env):
    env.write(_elig(("small:1", "tech_op", "simple", _lane(n=70)),
                    ("big:9", "tech_op", "simple", _lane(n=300)),
                    ("ghost:1", "tech_op", "simple", _lane("failed", n=999))))
    env.promote("ollama/big:9", "ollama/small:1")
    backend = Backend()
    await _post(env.app(backend), _req_after("Glob", {"pattern": "*.py"}))
    row = _gate_row(env.rows())
    assert row["decision"] == "served" and row["model"] == "ollama/big:9"
    assert env.backends == ["ollama/big:9"] and env.policy["model"] == MODEL


async def test_qa_lane_after_web_tools_uses_the_tier(env):
    env.policy.update(task_type="query", complexity="simple")
    env.write(_elig((BARE, "qa", "simple", _lane()), (BARE, "qa", "moderate", _lane("failed"))))
    backend = Backend()
    await _post(env.app(backend), _req_after("WebFetch", {"url": "https://example.org"}))
    assert _gate_row(env.rows())["local_gate"] == {"lane": "qa", "model": MODEL, "reason": "served_local"}


async def test_qa_moderate_tier_ineligible_forwards(env):
    env.policy.update(task_type="query", complexity="moderate")
    env.write(_elig((BARE, "qa", "simple", _lane()), (BARE, "qa", "moderate", _lane("failed"))))
    backend = Backend()
    await _post(env.app(backend), _req_after("WebSearch", {"query": "x"}))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["local_gate"]["reason"] == "ineligible_lane"
    assert backend.calls == []


# ── ineligible lanes, unreadable file ────────────────────────────────────────

@pytest.mark.parametrize("lane", [
    _lane("failed"), _lane("inconclusive"), _lane("not_informative"),
    {**_lane(), "eligible": False},                       # flag disagrees with the verdict
    {**_lane(), "grade": "probe"},                        # grade disagrees
    {**_lane(), "eligible": "true"},                      # not the boolean
    {"verdict": "eligible", "grade": "eligible", "n": 90},  # no eligible flag at all
])
async def test_ineligible_or_failed_lane_forwards_and_never_calls_the_backend(env, lane):
    env.write(_elig((BARE, "tech_op", "simple", lane)))
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["reason"] == "ineligible_lane"
    assert row["local_gate"]["reason"] == "ineligible_lane" and row["local_gate"]["lane"] == "tech_op"
    assert backend.calls == [] and env.backends == []


@pytest.mark.parametrize("content", [None, "", "   ", "{not json", "[]", "{}", "42", '{"a": 1'])
async def test_missing_empty_or_corrupt_file_forwards_never_local(env, content):
    if content is None:
        env.elig.unlink()
    else:
        env.elig.write_text(content)
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["reason"] == "eligibility_unreadable"
    assert row["local_gate"]["reason"] == "eligibility_unreadable"
    assert backend.calls == [] and env.backends == [] and len(env.up.requests) == 1


async def test_a_file_that_is_a_directory_is_unreadable(env):
    env.elig.unlink()
    env.elig.mkdir()
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    assert _gate_row(env.rows())["local_gate"]["reason"] == "eligibility_unreadable" and backend.calls == []


async def test_a_pin_does_not_make_an_ineligible_lane_serve(env):
    env.write(_elig((BARE, "tech_op", "simple", _lane("failed"))))
    backend = Backend()
    await _post(env.app(backend, model=MODEL), _req_after("Read"))
    assert _gate_row(env.rows())["decision"] == "forwarded" and backend.calls == []


# ── breaker ───────────────────────────────────────────────────────────────────

async def test_tripped_breaker_forwards_with_breaker_open(env):
    br = Breaker(allowed=False)
    backend = Backend()
    await _post(env.app(backend, breaker=br), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["local_gate"]["reason"] == "breaker_open"
    assert br.calls == [("techop_local", "tech_op")] and backend.calls == []


async def test_qa_and_code_lanes_use_the_proxy_local_lever_keyed_by_lane(env):
    env.policy.update(task_type="code", complexity="simple")
    env.write(_elig((BARE, "code", "simple", _lane())))
    br = Breaker(allowed=False)
    await _post(env.app(Backend(), breaker=br), _req_after("WebFetch", {"url": "u"}))
    assert br.calls == [("proxy_local", "code")]


async def test_breaker_exception_forwards(env):
    backend = Backend()
    await _post(env.app(backend, breaker=Breaker(exc=RuntimeError("boom"))), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["local_gate"]["reason"] == "breaker_open"
    assert backend.calls == []


def _real_breaker_state(env, *, opened_ago_s):
    state = {"version": 1, "classes": {"techop_local:tech_op": {
        "state": "open", "opened_at": time.time() - opened_ago_s}}}
    Path(env.elig.parent / "quality_breaker.json").write_text(json.dumps(state))


async def test_real_breaker_open_within_cooldown_forwards(env):
    _real_breaker_state(env, opened_ago_s=60)
    backend = Backend()
    await _post(env.app(backend, real_breaker=True), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["local_gate"]["reason"] == "breaker_open" and backend.calls == []


async def test_half_open_lets_exactly_one_probe_through(env):
    _real_breaker_state(env, opened_ago_s=10 * 86400)         # cooldown elapsed -> half_open
    backend = Backend()
    app = env.app(backend, real_breaker=True)
    await _post(app, _req_after("Read"))
    await _post(app, _req_after("Grep", {"pattern": "x"}))
    first, second = env.rows()
    assert first["decision"] == "served" and first["local_gate"]["reason"] == "served_local"
    assert second["decision"] == "forwarded" and second["local_gate"]["reason"] == "breaker_open"
    assert len(backend.calls) == 1


# ── gate off ──────────────────────────────────────────────────────────────────

async def test_gate_off_reads_no_file_and_adds_no_key(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the eligibility file was read with the gate off")

    monkeypatch.setattr(local_gate, "load_eligibility", boom)
    monkeypatch.setattr(local_gate, "eligible_model", boom)
    env.elig.write_text("{corrupt")
    backend = Backend()
    app = env.app(backend, gate=False)
    r = await _post(app, _req_after("Read"))
    assert r.status_code == 200
    row = env.rows()[0]
    assert "local_gate" not in row
    assert row["decision"] == "served" and row["model"] == MODEL      # the policy's pick, as before


async def test_gate_off_is_the_same_row_as_before_the_gate_existed(env):
    """Row keys with the gate off equal those of a build that has no gate: ``local_gate`` is the
    only key the gate adds, and only when on."""
    await _post(env.app(Backend(), gate=False), _req_after("Read"))
    await _post(env.app(Backend(), gate=True), _req_after("Read"))
    off, on = env.rows()
    assert set(on) - set(off) == {"local_gate"} and set(off) <= set(on)


async def test_routing_off_is_unchanged_and_reads_no_file(env, monkeypatch):
    monkeypatch.setattr(local_gate, "load_eligibility",
                        lambda *a, **k: pytest.fail("read with routing off"))
    await _post(env.app(Backend(), steps=frozenset()), _req_after("Read"))
    row = env.rows()[0]
    assert row["decision"] == "forwarded" and row["reason"] == "routing_off" and "local_gate" not in row


def test_gate_defaults_off_and_parses(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROXY_LOCAL_GATE", raising=False)
    assert ps.ProxyConfig().local_gate is False and ps.ProxyConfig.from_env().local_gate is False
    monkeypatch.setenv("LLM_ROUTER_PROXY_LOCAL_GATE", "on")
    assert ps.ProxyConfig.from_env().local_gate is True
    monkeypatch.setenv("LLM_ROUTER_PROXY_LOCAL_GATE", "maybe")
    with pytest.raises(ValueError):
        ps.ProxyConfig.from_env()


def test_gate_refuses_to_combine_with_serve_local_agent():
    cfg = ps.ProxyConfig(local_gate=True, serve="local-agent", model=MODEL, trim="none")
    with pytest.raises(ValueError, match="local-gate"):
        ps.build_app(cfg)


# ── steps that have no lane ──────────────────────────────────────────────────

async def test_turn_first_is_forwarded_and_never_reaches_the_gate(env):
    backend = Backend()
    await _post(env.app(backend), _first_call())
    row = env.rows()[0]
    assert row["decision"] == "forwarded" and row["reason"] == "not_eligible" and backend.calls == []


@pytest.mark.parametrize("step_class", ["turn_first", "subagent_first", "subagent_turn",
                                         "harness_turn", "side_call", "unknown"])
async def test_only_continuations_get_a_lane(env, step_class):
    gate = local_gate.LaneGate(frozenset({"continuation"}), 30.0, env.deps(breaker=Breaker()))
    row = {"step_class": step_class, "prev_tool_class": "technical_op"}
    d = await gate.decide(_req_after("Read"), row, env.policy)
    assert (d.serve, d.reason) == (False, "not_continuation")


@pytest.mark.parametrize("prev", ["edit", "exec", "agent", "other", None])
async def test_exec_edit_agent_other_stay_on_claude(env, prev):
    gate = local_gate.LaneGate(frozenset({"continuation"}), 30.0, env.deps(breaker=Breaker()))
    d = await gate.decide(_req_after("Read"), {"step_class": "continuation", "prev_tool_class": prev},
                          env.policy)
    assert (d.serve, d.reason) == (False, "lane_unmapped")


@pytest.mark.parametrize("task,cx,lane", [
    ("query", "simple", ("qa", "simple")), ("research", "moderate", ("qa", "moderate")),
    ("analyze", "simple", ("qa", "simple")), ("code", "moderate", ("code", "moderate")),
    ("query", "complex", None), ("generate", "simple", None), ("code", None, None)])
def test_web_continuation_lane_mapping(task, cx, lane):
    got = local_gate.lane_of({"step_class": "continuation", "prev_tool_class": "web"},
                             {"task_type": task, "complexity": cx})
    assert (got if isinstance(got, tuple) else None) == lane


async def test_a_mixed_step_with_a_write_is_exec_so_it_is_not_served(env):
    body = _req_after("Bash", {"command": "ls && touch x"})
    backend = Backend()
    await _post(env.app(backend), body)
    row = _gate_row(env.rows())
    assert row["prev_tool_class"] == "exec" and row["local_gate"]["reason"] == "lane_unmapped"
    assert backend.calls == []


# ── P0.15, F14, health, fit ──────────────────────────────────────────────────

async def test_unpromoted_model_is_promote_gated(env):
    (env.elig.parent / "grandfathered_models.json").write_text(json.dumps({"at": 1, "ids": []}))
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["local_gate"]["reason"] == "promote_gated"
    assert row["local_gate"]["model"] == MODEL and backend.calls == []


async def test_model_lacking_tools_is_capability_excluded(env, monkeypatch):
    monkeypatch.setattr(capability_filter, "caps_of",
                        lambda model, view=None: capability_filter.ModelCaps(tools=False))
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["local_gate"]["reason"] == "capability_excluded" and backend.calls == []


async def test_model_with_too_small_a_window_is_capability_excluded(env, monkeypatch):
    monkeypatch.setattr(capability_filter, "caps_of",
                        lambda model, view=None: capability_filter.ModelCaps(window=200))
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    assert _gate_row(env.rows())["local_gate"]["reason"] == "capability_excluded"


async def test_irreducible_tokens_over_the_serving_window_is_pack_over_window(env):
    backend = Backend()
    await _post(env.app(backend, deps=env.deps(window=lambda m: 50)), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["local_gate"]["reason"] == "pack_over_window" and backend.calls == []


async def test_unsuitable_task_type_is_no_local_model(env):
    env.policy.update(task_type="generate")
    backend = Backend()
    await _post(env.app(backend), _req_after("Read"))
    assert _gate_row(env.rows())["local_gate"]["reason"] == "no_local_model" and backend.calls == []


async def test_open_backend_health_and_a_hung_watchdog_are_backend_unhealthy(env):
    backend = Backend()
    await _post(env.app(backend, deps=env.deps(backend_open=lambda m: True)), _req_after("Read"))
    hung = local_gate.GateDeps(watchdog=lambda: "hung")
    await _post(env.app(backend, deps=hung), _req_after("Read"))
    assert [r["local_gate"]["reason"] for r in env.rows()] == ["backend_unhealthy", "backend_unhealthy"]
    assert backend.calls == []


async def test_a_gate_that_raises_forwards(env):
    def boom(model, lane):
        raise RuntimeError("x")

    backend = Backend()
    await _post(env.app(backend, deps=env.deps(promoted=boom)), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "forwarded" and row["local_gate"]["reason"] == "gate_error"


# ── replies ───────────────────────────────────────────────────────────────────

async def test_an_edit_shaped_local_reply_is_never_served(env):
    backend = Backend(_ollama("", [_call("Edit", {"file_path": "/work/repo/pkg/mod01.py",
                                                  "old_string": "a", "new_string": "b"})]))
    await _post(env.app(backend), _req_after("Read"))
    row = _gate_row(env.rows())
    assert row["decision"] == "fallback" and row["reason"] == "edit_to_claude"
    assert row["local_gate"]["reason"] == "local_failed" and len(env.up.requests) == 1


async def test_a_local_backend_failure_falls_back_to_claude(env):
    backend = Backend(exc=ConnectionError("down"))
    r = await _post(env.app(backend), _req_after("Read"))
    assert r.status_code == 200 and len(env.up.requests) == 1
    row = _gate_row(env.rows())
    assert row["decision"] == "fallback" and row["reason"] == "backend_error"
    assert row["local_gate"] == {"lane": "tech_op", "model": MODEL, "reason": "local_failed"}


# ── breaker feed ──────────────────────────────────────────────────────────────

async def _serve_read(env, app):
    body = _req_after("Read", {"file_path": "/a.py"})
    r = await _post(app, body)
    assert env.rows()[-1]["decision"] == "served"
    return body, r


async def test_a_reread_within_two_calls_is_a_failure_unit(env):
    backend = Backend(_ollama("", [_call("Read", {"file_path": "/work/repo/x.py"})]))
    app = env.app(backend)
    body, _ = await _serve_read(env, app)
    served = ("Read", {"file_path": "/work/repo/x.py"})
    # request 1: the results of the served step itself; request 2: its next call re-reads x.py
    await _post(app, _next_turn(body, *served))
    assert env.units() == []
    body2 = _next_turn(body, "Read", {"file_path": "/work/repo/x.py"})
    await _post(app, body2)
    (unit,) = env.units()
    assert unit["outcome"] == "redo" and unit["lever"] == "techop_local" and unit["task_type"] == "tech_op"
    assert unit["model"] == MODEL and unit["kind"] == "techop_local"


async def test_two_calls_without_a_reread_is_a_success_unit(env):
    backend = Backend(_ollama("", [_call("Read", {"file_path": "/work/repo/x.py"})]))
    app = env.app(backend, steps=frozenset({"continuation"}))
    body, _ = await _serve_read(env, app)
    await _post(app, _next_turn(body, "Read", {"file_path": "/work/repo/x.py"}))   # served step's results
    await _post(app, _next_turn(body, "Read", {"file_path": "/work/repo/y.py"}))   # call 1
    assert env.units() == []
    await _post(app, _next_turn(body, "Grep", {"pattern": "z"}))                   # call 2
    (unit,) = env.units()
    assert unit["outcome"] == "used"


def test_feed_units_open_the_real_breaker(env):
    from llm_router import quality_breaker as qb

    feed = local_gate.FailureFeed()
    for i in range(qb.min_n()):
        local_gate._append_unit({"ts": local_gate.datetime.now(local_gate.timezone.utc).isoformat(),
                                 "lever": "techop_local", "task_type": "tech_op", "model": MODEL,
                                 "outcome": "redo", "session_kind": None}, feed.path)
    d = qb.should_route("techop_local", "tech_op", model=MODEL, units_fn=lambda **kw: local_gate.read_units())
    assert d.allowed is False and d.state == "open"
