"""AGT A.1: the SDK ``Router`` (sync + async), SDK Decision = MCP Decision in decide mode, budget.

Synthetic prompts only (``tests/fixtures/agt/a1_prompts_50.jsonl``, written for this test): the
GT corpus never enters a test. No provider is called: the SDK executors (``build_chain`` /
``execute_chain``, imported by ``llm_router.sdk`` at call time) and the MCP ``route_and_call`` are
fakes. Every state file lives under a tmp HOME, and the decide cache is in memory.

(a) decide mode: ``Router.run(p).decision`` equals the Decision MCP ``llm_route`` used for the same
    prompt, 50/50, and the decision-ledger rows of door ``sdk`` and door ``mcp`` match per text_sha.
(b) ``arun`` runs in a worker thread: two parallel calls take <= 1.3x one, and the loop never lags
    more than 100 ms.
(c) the ``routing_decisions`` row carries the caller's session and task ids.
(d) a spent budget raises ``BudgetExceeded`` before the executor runs.
(e) shadow mode (the default): no decision on the result, the gateway rules route, the engine
    shadows with door ``sdk``.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_router import engine, paths
from llm_router.hooks.direct_executor import DirectResult, ModelSpec
from llm_router.types import LLMResponse

FIXTURE = Path(__file__).parent / "fixtures" / "agt" / "a1_prompts_50.jsonl"
SID = "a1-caller-session-0001"
TASK = "a1-caller-task-0001"
PROMPT = "write a python function that merges two sorted lists"


def _prompts() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, temp_db):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    for k in ("LLM_ROUTER_ENGINE_MODE", "LLM_ROUTER_ENGINE_ABSTAIN", "LLM_ROUTER_ENSEMBLE",
              "LLM_ROUTER_DECISION_LEDGER"):
        monkeypatch.delenv(k, raising=False)
    engine.clear_cache(persistent=False)
    yield
    engine.shadow_drain()
    engine.clear_cache(persistent=False)


class _Exec:
    """Fake SDK executors: records calls, optional sleep, fixed answer."""

    def __init__(self, sleep: float = 0.0):
        self.sleep = sleep
        self.calls = 0
        self.chains: list[tuple] = []
        self._lock = threading.Lock()

    def build_chain(self, complexity, zone, task_type, **_k):
        with self._lock:
            self.chains.append((task_type, complexity))
        return [ModelSpec(provider="openai", model="gpt-4o-mini")]

    def execute_chain(self, request, chain, task_type, **_k):
        with self._lock:
            self.calls += 1
        if self.sleep:
            time.sleep(self.sleep)
        return DirectResult(text="answer", model=chain[0], latency_ms=5,
                            input_tokens=120, output_tokens=60)


@pytest.fixture
def fake(monkeypatch):
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de

    ex = _Exec()
    monkeypatch.setattr(cb, "build_chain", ex.build_chain)
    monkeypatch.setattr(cb, "get_current_pressure", lambda *a, **k: ("green", 5))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(de, "execute_chain", ex.execute_chain)
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: pytest.fail("agent loop not expected"))
    return ex


def _ctx():
    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    return ctx


# ── (a) SDK Decision = MCP Decision, decide mode ─────────────────────────────

def test_fixture_is_50_synthetic_prompts_over_5_task_types_x_3_tiers():
    rows = _prompts()
    assert len(rows) == 50 and len({r["text"] for r in rows}) == 50
    assert {(r["task_type"], r["tier"]) for r in rows} == {
        (t, k) for t in ("query", "research", "generate", "analyze", "code")
        for k in ("haiku", "sonnet", "opus")}
    assert sum(1 for r in rows if abs(len(r["text"]) - 599) <= 300) >= 10


async def test_a_sdk_decision_equals_mcp_llm_route_decision_50_of_50(fake, monkeypatch):
    from llm_router import Router, decision_ledger
    from llm_router.tools import routing

    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    seen: list[tuple[str, str, object]] = []
    real = engine.decide

    def spy(text, **kw):
        d = real(text, **kw)
        seen.append((kw.get("door"), text, d))
        return d

    monkeypatch.setattr(engine, "decide", spy)
    resp = LLMResponse(content="ok", model="openai/gpt-4o-mini", input_tokens=10, output_tokens=5,
                       cost_usd=0.00001, latency_ms=10.0, provider="openai")
    rac = AsyncMock(return_value=resp)
    monkeypatch.setattr(routing, "route_and_call", rac)

    router = Router(session=SID)
    rows = _prompts()
    matched, classes = 0, set()
    for row in rows:
        p = row["text"]
        seen.clear()
        sdk_d = router.run(p).decision
        assert sdk_d is not None, "decide mode: the SDK result carries the engine Decision"
        assert [(door, text) for door, text, _ in seen] == [("sdk", p)]

        seen.clear()
        await routing.llm_route(p, _ctx())
        mcp = [(text, d) for door, text, d in seen if door == "mcp"]
        assert len(mcp) == 1, seen
        mcp_text, mcp_d = mcp[0]
        assert mcp_text == p, "llm_route must classify the raw prompt (D-168 ii)"
        sent = rac.call_args.args[1]
        assert sent != p and p in sent, "the model still receives the boundary-wrapped prompt"
        assert sdk_d == mcp_d, (row["i"], sdk_d, mcp_d)
        matched += 1
        classes.add((sdk_d.task_type, sdk_d.complexity))
    assert matched == 50
    assert len(classes) >= 5, f"the fixture must exercise several classes, got {classes}"

    ledger = decision_ledger.read_ledger(paths.llm_router_home())

    def by_sha(door):
        return {r["text_sha"]: (r["tier"], r["confidence"], r["abstain"], r["reason"],
                                r["policy_version"], r["engine_version"])
                for r in ledger if r["door"] == door}

    sdk_rows, mcp_rows = by_sha("sdk"), by_sha("mcp")
    assert len(sdk_rows) == 50 and sdk_rows == mcp_rows
    assert {r["session_id"] for r in ledger if r["door"] == "sdk"} == {SID}


def test_a_door_agreement_sdk_site_matches_mcp_site_on_the_fixture(monkeypatch):
    import importlib.util
    import sys

    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    script = Path(__file__).resolve().parent.parent / "scripts" / "door_agreement.py"
    spec = importlib.util.spec_from_file_location("_door_agreement_a1", script)
    da = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = da
    spec.loader.exec_module(da)
    prompts = [r["text"] for r in _prompts()]
    out = da.measure(prompts, {s: da.SITES[s].load() for s in ("sdk", "mcp")})
    pair = out["pairs"]["sdk|mcp"]
    assert out["n"] == 50 and pair["task_type_disagree"] == 0 and pair["tier_disagree"] == 0


def test_door_agreement_sample_is_fixed_by_seed(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "door_agreement.py"
    spec = importlib.util.spec_from_file_location("_door_agreement_a1s", script)
    da = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = da
    spec.loader.exec_module(da)
    corpus = tmp_path / "c.jsonl"
    corpus.write_text("".join(json.dumps({"i": i, "text": r["text"]}) + "\n"
                              for i, r in enumerate(_prompts())))
    outs = []
    for k in range(2):
        o = tmp_path / f"o{k}.json"
        assert da.main(["--corpus", str(corpus), "--sites", "sdk,mcp", "--sample", "20",
                        "--seed", "168", "--out", str(o)]) == 0
        outs.append(json.loads(o.read_text()))
    assert outs[0]["n"] == 20 and outs[0]["sample"] == {"n": 20, "of": 50, "seed": 168}
    assert outs[0]["per_site"] == outs[1]["per_site"]
    assert outs[0]["pairs"]["sdk|mcp"]["tier_disagree"] == 0
    assert "text" not in json.dumps(outs[0]["per_site"])


# ── (b) sync + async ─────────────────────────────────────────────────────────

def test_b_sync_run_returns_a_result(fake):
    from llm_router import Router

    r = Router(session=SID).run(PROMPT)
    assert r.text == "answer" and r.model == "openai/gpt-4o-mini" and fake.calls == 1


async def test_b_arun_runs_off_the_loop_two_parallel_calls_overlap(fake):
    from llm_router import Router

    router = Router(session=SID)
    router.run(PROMPT)  # warm imports outside the timed section
    fake.sleep = 0.4

    t0 = time.perf_counter()
    r = await router.arun(PROMPT)
    one = time.perf_counter() - t0
    assert r.text == "answer"

    lag = 0.0
    stop = asyncio.Event()

    async def ticker():
        nonlocal lag
        while not stop.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            lag = max(lag, time.perf_counter() - t - 0.01)

    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    t0 = time.perf_counter()
    both = await asyncio.gather(router.arun(PROMPT), router.arun(PROMPT, task="a1-task-2"))
    two = time.perf_counter() - t0
    stop.set()
    await tick
    assert [b.text for b in both] == ["answer", "answer"]
    assert two <= 1.3 * one, (two, one)
    assert lag <= 0.1, lag


async def test_b_arun_keeps_the_callers_contextvars(fake):
    from llm_router import Router, call_identity

    router = Router(session=SID)
    with call_identity.scope(SID, task_id="outer-task-a1"):
        r = await router.arun(PROMPT)
    assert r.task_id == "outer-task-a1", "to_thread copies the context: the outer scope's task is kept"


# ── (c) caller ids on the rows ───────────────────────────────────────────────

def _db_rows(sql: str) -> list[tuple]:
    from llm_router.config import get_config

    con = sqlite3.connect(str(get_config().llm_router_db_path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_c_routing_decisions_row_carries_caller_session_and_task(fake):
    from llm_router import Router, route

    r = Router(session=SID).run(PROMPT, task=TASK)
    assert (r.session_id, r.task_id) == (SID, TASK)
    s = route(PROMPT + " again", session_id="a1-shim-session", task_id="a1-shim-task")
    assert (s.session_id, s.task_id) == ("a1-shim-session", "a1-shim-task")
    rows = _db_rows("SELECT session_id, task_id FROM routing_decisions ORDER BY rowid")
    assert rows == [(SID, TASK), ("a1-shim-session", "a1-shim-task")]


def test_c_decide_mode_ledger_row_carries_caller_ids(fake, monkeypatch):
    from llm_router import Router, decision_ledger

    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    Router(session=SID).run(PROMPT, task=TASK)
    rows = [r for r in decision_ledger.read_ledger(paths.llm_router_home()) if r["door"] == "sdk"]
    assert [(r["session_id"], r["task_id"]) for r in rows] == [(SID, TASK)]


# ── (d) budget ───────────────────────────────────────────────────────────────

def test_d_spent_budget_raises_before_the_executor_runs(fake, monkeypatch):
    import llm_router.hooks.savings_logger as sl
    from llm_router import BudgetExceeded, Router
    from llm_router.agents import budget

    assert BudgetExceeded is budget.BudgetExceeded
    monkeypatch.setattr(sl, "_cost_for", lambda *a, **k: 0.002)
    router = Router(session=SID, budget_usd=0.001)
    r = router.run(PROMPT)
    assert r.cost_usd == pytest.approx(0.002) and fake.calls == 1
    assert router.budget.consumed_usd == pytest.approx(0.002)
    with pytest.raises(BudgetExceeded) as exc:
        router.run(PROMPT + " second")
    assert fake.calls == 1, "the over-budget call must not reach the executor"
    assert exc.value.session_id == SID and exc.value.cap_usd == pytest.approx(0.001)


async def test_d_spent_budget_raises_on_arun_too(fake, monkeypatch):
    import llm_router.hooks.savings_logger as sl
    from llm_router import BudgetExceeded, Router

    monkeypatch.setattr(sl, "_cost_for", lambda *a, **k: 0.002)
    router = Router(budget_usd=0.001)
    await router.arun(PROMPT)
    with pytest.raises(BudgetExceeded):
        await router.arun(PROMPT)
    assert fake.calls == 1


def test_d_no_budget_never_raises_and_bad_budget_is_refused(fake, monkeypatch):
    import llm_router.hooks.savings_logger as sl
    from llm_router import Router

    monkeypatch.setattr(sl, "_cost_for", lambda *a, **k: 5.0)
    router = Router()
    for _ in range(3):
        router.run(PROMPT)
    assert fake.calls == 3 and router.budget is None
    for bad in (-1, float("nan"), float("inf"), "1", True):
        with pytest.raises(ValueError):
            Router(budget_usd=bad)


# ── (e) shadow mode ──────────────────────────────────────────────────────────

def test_e_shadow_mode_rules_route_and_engine_shadows_with_door_sdk(fake):
    from llm_router import Router
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    assert engine.wire_mode() == "shadow"
    r = Router(session=SID).run(PROMPT)
    assert r.decision is None
    s = classify_signals(PROMPT, GATEWAY_POLICY)
    assert fake.chains == [(s.task_type.value, s.complexity.value)]
    assert engine.shadow_drain()
    log = paths.state_path(engine.SHADOW_LOG)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [row["door"] for row in rows] == ["sdk"]
    assert (rows[0]["rules_task_type"], rows[0]["rules_complexity"]) == fake.chains[0]


# ── surface ──────────────────────────────────────────────────────────────────

def test_tier_maps_to_complexity_and_matches_the_engine(fake):
    from llm_router import Router, sdk

    assert sdk._TIER_COMPLEXITY == engine._TIER_COMPLEXITY
    router = Router(session=SID)
    for tier, cx in (("haiku", "simple"), ("sonnet", "moderate"), ("opus", "complex")):
        fake.chains.clear()
        router.run(PROMPT, tier=tier)
        assert fake.chains[0][1] == cx
    with pytest.raises(ValueError):
        router.run(PROMPT, tier="gpt")


def test_parent_project_policy_are_accepted_not_unimplemented(fake):
    from llm_router import Router

    router = Router(session=SID, project="proj-a1", policy="default")
    assert (router.project, router.policy) == ("proj-a1", "default")
    assert router.run(PROMPT, parent="a1-parent-session", context="be brief").text == "answer"


def test_exports():
    import llm_router

    for name in ("Router", "BudgetExceeded", "route", "RouteResult", "RoutingError"):
        assert name in llm_router.__all__ and getattr(llm_router, name) is not None
    assert "sdk" in engine.DOORS
