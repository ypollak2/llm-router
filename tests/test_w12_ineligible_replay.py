"""W1-2 / UN-09 (GOAL-INELIG, M2): N >= 1,000 replay, >= 100 per door, 0 routes to an ineligible model.

Five doors drive their REAL chain builders over seeded resource-map states and prompts
(``tests/w12_replay_fixture.py``):

========  ==============================================  ==========================================
door      chain builder                                   decision row written by
========  ==============================================  ==========================================
router    ``router.route_and_call``                        ``cost.log_routing_decision`` (+ stamp)
gateway   ``gateway._route`` -> ``route_server`` -> router the same
hook      ``hooks.chain_builder.build_chain`` +           ``savings_logger.log_direct_to_db``
          ``direct_executor.execute_chain``
sdk       ``sdk.route`` (``build_chain`` + ``execute_chain``)   ``log_direct_to_db`` (door ``sdk``)
proxy     ``proxy.backends.choose_model``                 ``proxy.ledger.write_row``
========  ==============================================  ==========================================

Only the transport is stubbed: ``providers.call_llm`` (LiteLLM) for the router / gateway doors and the
``direct_executor`` provider call table (plus the Ollama liveness probes) for hook / SDK. No network, no
Ollama. The hook door is ``chain_builder.build_chain`` + ``execute_chain`` + ``log_direct_to_db``, the
three calls ``hooks/auto-route.py`` makes on its DIRECT path; the hook *script* (off limits, stdin
protocol) is not run.

Each door writes into its own throwaway ``usage.db`` (rows labelled replay: ``is_real = 0``, proxy rows
``session_kind = harness``; the counter reports those as ``synthetic``, separate from organic, D-52). Then
two checks must agree: the product counter (``ineligible_routes.compute``) and an independent predicate that
reads the generated map rows directly. Both must say 0.

Non-vacuity: (1) every door must have >= 100 evaluated rows; (2) planted ineligible decisions written
through the same stamp path MUST be counted, exactly (``planted == counted``, both > 0); (3) the mutant of
the spec, ``apply_to_chain`` / ``filter_providers`` disabled, must make BOTH checks go above 0.

NOT in this PR: the live 7-day count (tag criterion 9) waits for >= 300 organic decisions in >= 3 sessions.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from llm_router import cost, decision_stamp, failopen, ineligible_routes as ir, map_reader
from llm_router import resource_map as rm
from llm_router.types import LLMResponse
from tests import w12_replay_fixture as fx

#: One module-scoped replay (about 50 s on a loaded machine) feeds every test; pytest-timeout's default 30 s
#: would kill the first test that builds it, and ``xdist_group`` keeps one worker from building it twice.
pytestmark = [pytest.mark.timeout(300), pytest.mark.xdist_group("w12_replay")]

SEED = 20261029
#: Requests per door: 1,640 in all (spec: N >= 1,000, >= 100 per door). The router and gateway doors cost ~0.1 s
#: a request (every ``route_and_call`` re-opens and re-migrates ``usage.db``), so they get fewer; each door
#: must still show >= 100 EVALUATED rows (a route to a provider the map has no row for is not judged).
REQUESTS = {"router": 200, "gateway": 200, "hook": 320, "sdk": 440, "proxy": 480}
DOORS = tuple(REQUESTS)
MUTANT_REQUESTS = {"router": 20, "gateway": 20, "hook": 40, "sdk": 40, "proxy": 40}
PLANTED = 7
MIN_JUDGED_PER_DOOR = 150
ANSWER = ("A reasonable answer: check the queue depth first, then compare consumer lag against the "
          "autoscaler events, and only then look at the downstream timeout in the ledger service.")


_LOOP = asyncio.new_event_loop()   # one loop for the whole module: asyncio.run() pays ~0.3 s per call closing its executor


def _run(coro):
    """Run *coro* to completion, then let the fire-and-forget tasks it spawned (the ledger write) finish."""
    try:
        return _LOOP.run_until_complete(coro)
    finally:
        pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
        if pending:
            _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


@dataclass
class DoorRun:
    door: str
    db: Path
    proxy: Path
    served: list[tuple[str, bool | None]] = field(default_factory=list)   # (chosen id, independent verdict)
    no_route: int = 0              # states whose chain came back empty (nothing served, nothing written)

    @property
    def independent_ineligible(self) -> int:
        return sum(1 for _, ok in self.served if ok is False)

    @property
    def independent_evaluated(self) -> int:
        return sum(1 for _, ok in self.served if ok is not None)


def _world_env(mp: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Throwaway HOME / router home / config; keys present so every cloud provider is configured."""
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    mp.setenv("HOME", str(home))
    mp.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    for k in [k for k in os.environ if k.endswith(("_API_KEY", "_API_TOKEN", "_API_BASE"))]:
        mp.delenv(k)                    # the host's own keys must not add providers the map never heard of
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY"):
        mp.setenv(k, "k-replay")
    mp.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    mp.setenv("LLM_ROUTER_ENGINE_MODE", "off")
    mp.setenv("LLM_ROUTER_CONTEXT_INJECTION", "off")   # prompt enrichment (git / OKF reads), not routing
    mp.setenv("LLM_ROUTER_USER_ID", "replay")
    mp.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    mp.setattr("llm_router.team.get_project_id", lambda: "replay")
    mp.chdir(tmp_path)
    mp.setattr(cost, "_write_provenance", lambda: cost.PROVENANCE_RUNTIME)
    mp.setattr(failopen, "record", lambda *a, **k: None)
    # The module-scoped fixture runs before conftest's function-scoped guards, so do their job here: no local
    # model registry (it GETs :11434/api/tags), no codex / gemini CLI probes, and a socket guard that refuses
    # and records any connection to the Ollama port.
    import socket

    import llm_router.router as router_module

    mp.setattr("llm_router.agentic_registry.list_installed_models", lambda: [])
    mp.setattr(router_module, "is_codex_available", lambda: False)
    mp.setattr(router_module, "is_gemini_cli_available", lambda: False)
    real_connect = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == 11434:
            OLLAMA_CONNECTS.append(address)
            raise ConnectionRefusedError("W12 replay: no connection to Ollama")
        return real_connect(self, address)

    mp.setattr(socket.socket, "connect", guarded)
    # Every route_and_call re-opens usage.db and re-checks ~2,700 migration columns; once a column is seen the
    # answer cannot change for the same db, so remember it (cleared by _use_db whenever the db changes).
    real_exists = cost._column_exists

    async def column_exists(db, table, column):
        if (table, column) in _KNOWN_COLUMNS:
            return True
        found = await real_exists(db, table, column)
        if found:
            _KNOWN_COLUMNS.add((table, column))
        return found

    mp.setattr(cost, "_column_exists", column_exists)


OLLAMA_CONNECTS: list = []
_KNOWN_COLUMNS: set = set()


def _use_db(path: Path, monkeypatch) -> None:
    import llm_router.config as config_module

    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(path))
    config_module._config = None
    decision_stamp.reset_cache()
    _KNOWN_COLUMNS.clear()


def _install(state: fx.MapState, monkeypatch) -> dict:
    """Write the map for *state* and point every chain input (dynamic table, Ollama inventory) at it."""
    import llm_router.dynamic_routing as dr
    from llm_router.config import RouterConfig
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends

    doc = fx.map_document(state, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    rm.write_map(doc)
    map_reader._cache.clear()
    decision_stamp.reset_cache()
    backends._chain_cache.clear()
    locals_ = state.local_models
    base = list(state.base_chain)
    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda profile, task: list(base))
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: list(locals_))
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", ",".join(m.split("/", 1)[1] for m in locals_))
    monkeypatch.setattr(hook_cb, "_ollama_models",
                        lambda *a, **k: ([ModelSpec("ollama", m.split("/", 1)[1]) for m in locals_], []))
    return doc


def _stub_transport(monkeypatch) -> None:
    """The only fakes: LiteLLM-level call and the direct executor's provider table / Ollama probes."""
    from llm_router import providers
    from llm_router.hooks import direct_executor as de

    async def call_llm(model, messages, **kw):
        prov = model.split("/", 1)[0]
        return LLMResponse(content=ANSWER, model=model, input_tokens=40, output_tokens=30, cost_usd=0.0,
                           latency_ms=5.0, provider=prov)

    monkeypatch.setattr(providers, "call_llm", call_llm)

    async def no_cache(*a, **k):    # the semantic dedup cache would answer a repeated prompt without building a chain
        return None

    monkeypatch.setattr("llm_router.semantic_cache.check", no_cache)
    monkeypatch.setattr("llm_router.semantic_cache.store", no_cache)
    fake = lambda prompt, model, timeout, history, system: (ANSWER, {"input_tokens": 40, "output_tokens": 30})  # noqa: E731
    monkeypatch.setattr(de, "_PROVIDER_CALLS", {p: fake for p in ("ollama", "gemini", "openai", "deepseek", "groq")})
    monkeypatch.setattr(de, "available_ollama_models", lambda timeout=0.5: None)
    monkeypatch.setattr(de, "ollama_is_alive", lambda timeout=0.5: True)
    monkeypatch.setattr(de, "_okf_inject", lambda prompt, **k: prompt)
    monkeypatch.setattr(de, "_okf_enrich", lambda *a, **k: None)
    monkeypatch.setattr(de, "_paid_budget_exhausted", lambda provider: False)
    monkeypatch.setattr(de, "_provider_reset_blocked", lambda provider: False)
    monkeypatch.setattr("llm_router.ollama_watchdog.gate", lambda *a, **k: None)
    # file / command prompts take the SDK's agent loop, whose only transport is the Ollama chat call it makes
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", lambda **k: ANSWER)


def _new_db(root: Path, door: str, monkeypatch) -> DoorRun:
    d = root / f"db-{door}"
    d.mkdir(exist_ok=True)
    db = d / "test_usage.db"
    _use_db(db, monkeypatch)
    return DoorRun(door, db, d / "proxy_calls.jsonl")


def _served(run: DoorRun, chosen: str | None, doc: dict) -> None:
    if chosen is None:
        run.no_route += 1
        return
    run.served.append((chosen, fx.eligible_by_map_rows(doc, chosen)))


# ── one function per door: real chain builder in, chosen model + written decision row out ─────────

def _door_router(run: DoorRun, state: fx.MapState, prompt: str, doc: dict, model: str | None = None) -> None:
    from llm_router.router import BudgetExceededError, NoRoutableModelsError, route_and_call
    from llm_router.types import Complexity, TaskType

    try:
        resp = _run(route_and_call(TaskType(state.task), prompt, complexity_hint=Complexity(state.complexity),
                                   model_override=model))
    except (NoRoutableModelsError, BudgetExceededError):   # nothing routable: no route, no routed row
        _served(run, None, doc)
        return
    _served(run, resp.model, doc)


def _door_gateway(run: DoorRun, state: fx.MapState, prompt: str, doc: dict, model: str | None = None) -> None:
    from fastapi import HTTPException

    from llm_router import gateway

    try:
        out = _run(gateway._route(prompt, state.task, state.complexity, prefer_model=model))
    except HTTPException:
        _served(run, None, doc)
        return
    _served(run, f"{out.model.provider}/{out.model.model}", doc)


def _door_hook(run: DoorRun, state: fx.MapState, prompt: str, doc: dict) -> None:
    from llm_router.hooks.chain_builder import build_chain
    from llm_router.hooks.direct_executor import execute_chain
    from llm_router.hooks.savings_logger import log_direct_to_db

    chain = build_chain(state.complexity, "green", state.task)
    result = execute_chain(prompt, chain, state.task, timeout=2)
    if result is None:
        _served(run, None, doc)
        return
    log_direct_to_db(result=result, prompt=prompt, task_type=state.task, complexity=state.complexity,
                     classifier_type="hook", session_id=f"replay-hook-{run.door}", door="hook",
                     layer="heuristic")
    _served(run, f"{result.model.provider}/{result.model.model}", doc)


def _door_sdk(run: DoorRun, state: fx.MapState, prompt: str, doc: dict) -> None:
    from llm_router import RoutingError, route

    try:
        r = route(prompt, task_type=state.task, complexity=state.complexity, session_id=f"replay-sdk-{run.door}")
    except RoutingError:
        _served(run, None, doc)
        return
    _served(run, r.model, doc)


def _door_proxy(run: DoorRun, state: fx.MapState, prompt: str, doc: dict) -> None:
    from llm_router.proxy import backends, ledger

    pick = _run(backends.choose_model(prompt, None))
    model = pick["model"]
    if model is None:
        _served(run, None, doc)
        return
    prov, name = model.split("/", 1)
    ledger.write_row({"ts": time.time(), "served_model": name, "served_by": prov,
                      "session_id": "replay-proxy", "session_kind": "harness"}, run.proxy)
    _served(run, model, doc)


_DRIVERS = {"router": _door_router, "gateway": _door_gateway, "hook": _door_hook,
            "sdk": _door_sdk, "proxy": _door_proxy}


def _replay(root: Path, door: str, n: int, monkeypatch, *, seed: int = SEED) -> DoorRun:
    run = _new_db(root, door, monkeypatch)
    for i in range(n):
        state = fx.map_state_for(seed, door, i)
        doc = _install(state, monkeypatch)
        _DRIVERS[door](run, state, fx.prompt_for(seed, door, i), doc)
    if run.db.exists():     # replay rows are labelled and reported separately (D-52)
        _label_replay(run.db)
    return run


def _label_replay(db: Path) -> None:
    con = sqlite3.connect(db)
    con.execute("UPDATE routing_decisions SET is_real = 0")
    con.commit()
    con.close()


def _count(run: DoorRun) -> dict:
    return ir.compute(days=1.0, until=time.time() + 60, db_path=run.db, proxy_path=run.proxy)


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    """The whole replay, once: ``{door: DoorRun}`` plus the still-active patch context for the controls."""
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("w12-replay")
    _world_env(mp, root)
    _stub_transport(mp)
    runs = {}
    for door, n in REQUESTS.items():
        t0 = time.monotonic()
        runs[door] = _replay(root, door, n, mp)
        print(f"W12 replay {door}: {n} requests in {time.monotonic() - t0:.1f}s")
    yield type("Replay", (), {"runs": runs, "mp": mp, "root": root})
    import llm_router.config as config_module

    mp.undo()
    config_module._config = None
    decision_stamp.reset_cache()
    map_reader._cache.clear()


# ── the replay ────────────────────────────────────────────────────────────────

def test_replay_n_per_door_zero_ineligible_and_both_checks_agree(replay):
    total_requests = sum(REQUESTS.values())
    assert total_requests >= 1000
    detail = []
    judged = 0
    for door, run in replay.runs.items():
        res = _count(run)
        # non-vacuity: the door produced routes the counter could judge, at least 100 of them
        assert res["evaluated_n"] >= 100, (door, res["evaluated_n"], run.no_route)
        assert run.independent_evaluated >= 100, (door, run.independent_evaluated)
        assert res["not_evaluable"] == 0, (door, res["not_evaluable"])      # every row carried a resolvable stamp
        assert res["rows_read"] == len(run.served), (door, res["rows_read"], len(run.served))
        # the two checks must both say 0: the product counter, and the predicate straight from the map rows
        assert res["ineligible"] == 0, (door, res["examples"])
        assert run.independent_ineligible == 0, (door, [s for s in run.served if s[1] is False][:5])
        # and they judged the same routes (the independent predicate abstains on providers the map lacks)
        assert res["evaluated_n"] == run.independent_evaluated, (door, res["evaluated_n"], run.independent_evaluated)
        # replay rows are labelled and kept apart from organic ones
        assert res["populations"]["synthetic"].get("eligible", 0) == res["evaluated_n"], (door, res["populations"])
        assert res["organic_evaluated_n"] == 0
        assert len(run.served) + run.no_route == REQUESTS[door], (door, len(run.served), run.no_route)
        assert res["evaluated_n"] >= MIN_JUDGED_PER_DOOR, (door, res["evaluated_n"])
        judged += res["evaluated_n"]
        detail.append(f"{door}={res['evaluated_n']}/{REQUESTS[door]}")
    assert judged >= 1000, judged                        # M2: min n 1,000 JUDGED decisions (07-ACCEPTANCE:122)
    assert OLLAMA_CONNECTS == [], OLLAMA_CONNECTS        # hermetic: nothing reached for the Ollama port
    print(f"W12 replay judged total={judged}; evaluated/requests: " + " ".join(detail))


ALL_DOWN = 8


@pytest.mark.parametrize("door", DOORS)
def test_every_provider_disconnected_routes_nowhere(replay, door):
    """Maps that disconnect every provider the chain could use: the door serves nothing and records nothing
    (not counted in the 1,000: a separate, deliberately hostile set)."""
    (replay.root / "alldown").mkdir(exist_ok=True)
    run = _new_db(replay.root / "alldown", door, replay.mp)
    for i in range(ALL_DOWN):
        state = fx.all_down_state(SEED, door, i)
        doc = _install(state, replay.mp)
        _DRIVERS[door](run, state, fx.prompt_for(SEED, door, i), doc)
    assert run.served == [] and run.no_route == ALL_DOWN, (door, run.served, run.no_route)
    assert not run.db.exists() or sqlite3.connect(run.db).execute(
        "select count(*) from routing_decisions").fetchone()[0] == 0, door


# ── positive controls ─────────────────────────────────────────────────────────

def _plant_state() -> fx.MapState:
    """gemini is disconnected in this map; ollama / openai are connected."""
    return fx.MapState(
        statuses={"ollama": "connected", "gemini": "disconnected", "openai": "connected",
                  "deepseek": None, "groq": None},
        base_chain=("ollama/qwen3:8b", "gemini/gemini-2.5-flash", "openai/gpt-4o-mini"),
        task="query", complexity="simple")


def _plant(run: DoorRun, mp, n: int) -> None:
    """*n* decisions recorded as routed to the disconnected provider, each through its door's own writer:
    ``log_direct_to_db`` (hook, sdk), ``cost.log_routing_decision`` + ``decision_stamp.stamp`` (router, gateway),
    ``ledger.write_row`` (proxy)."""
    from llm_router.hooks.direct_executor import DirectResult, ModelSpec
    from llm_router.hooks.savings_logger import log_direct_to_db
    from llm_router.proxy import ledger

    _use_db(run.db, mp)
    _install(_plant_state(), mp)
    if run.door == "proxy":
        for _ in range(n):
            ledger.write_row({"ts": time.time(), "served_model": "gemini-2.5-flash", "served_by": "gemini",
                              "session_id": "replay-proxy", "session_kind": "harness"}, run.proxy)
        return
    if run.door in ("hook", "sdk"):
        for _ in range(n):
            log_direct_to_db(result=DirectResult(text=ANSWER, model=ModelSpec("gemini", "gemini-2.5-flash"),
                                                 latency_ms=5, input_tokens=10, output_tokens=10),
                             prompt="planted", task_type="query", complexity="simple",
                             classifier_type=run.door, session_id=f"replay-{run.door}", door=run.door,
                             layer="heuristic")
        _label_replay(run.db)
        return

    async def write() -> None:
        for _ in range(n):
            await cost.log_routing_decision(
                prompt="planted", task_type="query", profile="balanced", classifier_type="heuristic",
                classifier_model=None, classifier_confidence=None, classifier_latency_ms=None,
                complexity="simple", recommended_model="gemini/gemini-2.5-flash",
                base_model="gemini/gemini-2.5-flash", was_downshifted=False, budget_pct_used=None,
                quality_mode=None, final_model="gemini/gemini-2.5-flash", final_provider="gemini",
                success=True, input_tokens=10, output_tokens=10, cost_usd=0.0, latency_ms=5.0,
                reason_code="router_chain", **decision_stamp.stamp("heuristic", None, "router_chain"))

    _run(write())
    _label_replay(run.db)


@pytest.mark.parametrize("door", DOORS)
def test_planted_ineligible_routes_are_counted_exactly(replay, door, tmp_path):
    """Positive control: PLANTED ineligible decisions on top of the real replay of the door are ALL counted,
    and the replay's own rows still add nothing (counter 0 before, exactly PLANTED after)."""
    import shutil

    src = replay.runs[door]
    copy = DoorRun(door, tmp_path / "test_usage.db", tmp_path / "proxy_calls.jsonl")
    if src.db.exists():
        shutil.copy(src.db, copy.db)
    if src.proxy.exists():
        shutil.copy(src.proxy, copy.proxy)
    before = _count(copy)
    assert before["ineligible"] == 0 and before["evaluated_n"] >= 100, (door, before["evaluated_n"])
    _plant(copy, replay.mp, PLANTED)
    after = _count(copy)
    assert PLANTED > 0 and after["ineligible"] is not None and after["ineligible"] > 0   # a vacuous 0 is impossible
    assert after["ineligible"] == PLANTED, (door, after["ineligible"], after["examples"][:3])
    assert after["evaluated_n"] == before["evaluated_n"] + PLANTED
    assert {e["model"] for e in after["examples"]} == {"gemini/gemini-2.5-flash"}


def test_mutant_filter_disabled_makes_both_checks_go_above_zero(replay, monkeypatch):
    """The spec's mutant: ``apply_to_chain`` (and the dynamic table's ``filter_providers``) disabled. The replay of
    the same kind of states then routes to disconnected providers, and BOTH checks must see it, in the same number."""
    monkeypatch.setattr(map_reader, "apply_to_chain", lambda chain, key=lambda m: m, **k: list(chain))
    monkeypatch.setattr(map_reader, "filter_providers", lambda providers: providers)
    sums = {}
    for door, n in MUTANT_REQUESTS.items():
        mroot = replay.root / "mutant"
        mroot.mkdir(exist_ok=True)
        run = _replay(mroot, door, n, monkeypatch, seed=SEED + 1)
        res = _count(run)
        assert res["ineligible"] is not None and res["ineligible"] > 0, (door, res["evaluated_n"], run.no_route)
        assert run.independent_ineligible > 0, door
        assert res["ineligible"] == run.independent_ineligible, (door, res["ineligible"], run.independent_ineligible)
        sums[door] = res["ineligible"]
    print("W12 mutant ineligible per door: " + " ".join(f"{d}={v}" for d, v in sums.items()))


# ── caller-named models (review M1 / S1) ──────────────────────────────────────

SUBSCRIPTION_MODEL = "anthropic/claude-haiku-4-5"   # what an Anthropic-format gateway client names
NAMED_REQUESTS = {"router": 220, "gateway": 220}
PIN_REQUESTS = 25


def _named_state(seed: int, door: str, i: int) -> fx.MapState:
    """A map state whose local rows include ``qwen3:8b`` (the subscription fallback chain carries it)."""
    st = fx.map_state_for(seed, door, i)
    base = tuple(dict.fromkeys(("ollama/qwen3:8b", *st.base_chain)))
    return fx.MapState(statuses=st.statuses, base_chain=base, task=st.task, complexity=st.complexity)


def _named_replay(root: Path, door: str, n: int, mp, model: str, *, pin_down: str | None = None,
                  seed: int = SEED + 2) -> DoorRun:
    run = _new_db(root, door, mp)
    for i in range(n):
        state = _named_state(seed, door, i)
        if pin_down:    # the pinned provider is disconnected in this map
            state = fx.MapState(statuses={**state.statuses, pin_down: "disconnected", "ollama": "connected"},
                                base_chain=state.base_chain, task=state.task, complexity=state.complexity)
        doc = _install(state, mp)
        _DRIVERS[door](run, state, fx.prompt_for(seed, door, i), doc, model=model)
    _label_replay(run.db)
    return run


@pytest.fixture
def subscription_mode(replay, monkeypatch):
    import llm_router.config as config_module

    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    config_module._config = None
    yield
    config_module._config = None


@pytest.mark.parametrize("door", ["router", "gateway"])
def test_subscription_override_fallback_replay_zero_ineligible(replay, subscription_mode, door):
    """Review M1: a caller-named ``anthropic/*`` model in subscription mode falls back to the static balanced
    chain; that chain answers to the map like every other. >= 100 judged decisions per door, both checks 0."""
    mroot = replay.root / "named"
    mroot.mkdir(exist_ok=True)
    run = _named_replay(mroot, door, NAMED_REQUESTS[door], replay.mp, SUBSCRIPTION_MODEL)
    res = _count(run)
    assert res["evaluated_n"] >= 100, (door, res["evaluated_n"], run.no_route)
    assert res["ineligible"] == 0, (door, res["examples"])
    assert run.independent_ineligible == 0 and run.independent_evaluated == res["evaluated_n"], door


@pytest.mark.parametrize("door", ["router", "gateway"])
def test_explicit_pin_to_a_disconnected_model_is_honoured_and_counted(replay, door):
    """Review S1: a caller who names a model gets it (``return [model_override]``); the counter still counts
    that as an ineligible route, as 07-ACCEPTANCE M2 says pins are counted. Counter == number of pinned requests."""
    mroot = replay.root / "pin"
    mroot.mkdir(exist_ok=True)
    run = _named_replay(mroot, door, PIN_REQUESTS, replay.mp, "openai/gpt-4o-mini", pin_down="openai")
    res = _count(run)
    assert PIN_REQUESTS > 0 and run.no_route == 0
    assert {m for m, _ in run.served} == {"openai/gpt-4o-mini"}
    assert res["ineligible"] == PIN_REQUESTS == run.independent_ineligible, (door, res["ineligible"])


def test_subscription_override_fallback_honours_the_map(tmp_path, monkeypatch):
    """Review M1, regression (fails on main): openai is disconnected in the map; the fallback chain for a named
    ``anthropic/*`` model used to be returned unfiltered and served ``openai/gpt-4o``."""
    import llm_router.config as config_module
    from llm_router.router import route_and_call
    from llm_router.types import Complexity, TaskType

    _world_env(monkeypatch, tmp_path)
    _stub_transport(monkeypatch)
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    _use_db(tmp_path / "m1.db", monkeypatch)
    state = fx.MapState(statuses={"ollama": "connected", "gemini": "connected", "openai": "disconnected",
                                  "deepseek": "disconnected", "groq": "disconnected"},
                        base_chain=("ollama/qwen3:8b", "gemini/gemini-2.5-flash"), task="query",
                        complexity="moderate")
    _install(state, monkeypatch)
    seen = _run(route_and_call(TaskType.QUERY, "summarise the retry policy in two lines",
                               complexity_hint=Complexity.MODERATE, model_override=SUBSCRIPTION_MODEL))
    config_module._config = None
    assert seen.provider not in {"openai", "deepseek", "groq"}, seen.model


def test_subject_specialist_honours_the_map(tmp_path, monkeypatch):
    """Review S2, regression (fails on main): a policy specialist fronted after the build skipped the map."""
    from llm_router.router import route_and_call
    from llm_router.types import Complexity, TaskType
    from tests.test_benchmark_cat_g import _policy

    _world_env(monkeypatch, tmp_path)
    _stub_transport(monkeypatch)
    _use_db(tmp_path / "s2.db", monkeypatch)
    monkeypatch.setattr("llm_router.policy.get_active_policy",
                        lambda: _policy("w12", workhorses=["gemini/gemini-2.5-flash"],
                                        specialists={"code": "openai/gpt-4o-mini"}))

    def served(openai_status: str) -> str:
        state = fx.MapState(statuses={"ollama": None, "gemini": "connected", "openai": openai_status,
                                      "deepseek": None, "groq": None},
                            base_chain=("gemini/gemini-2.5-flash", "openai/gpt-4o-mini"), task="code",
                            complexity="simple")
        _install(state, monkeypatch)
        return _run(route_and_call(TaskType.CODE, "write a function that reverses a list",
                                   complexity_hint=Complexity.SIMPLE,
                                   classification_data={"subject": "code"})).model

    assert served("connected") == "openai/gpt-4o-mini"      # the specialist leads when the map allows it
    assert served("disconnected").startswith("gemini/")     # and yields to the map when it does not


# ── the finding the replay made ──────────────────────────────────────────────

def test_precision_tier_fronts_openai_only_when_the_map_allows_it(tmp_path, monkeypatch):
    """A short exact-answer prompt is fronted with ``openai/gpt-4o-mini``; that injection used to skip the map."""
    from llm_router.router import route_and_call
    from llm_router.types import Complexity, TaskType

    def served(openai_status: str) -> str:
        state = fx.MapState(statuses={"ollama": None, "gemini": "connected", "openai": openai_status,
                                      "deepseek": None, "groq": None},
                            base_chain=("gemini/gemini-2.5-flash", "openai/gpt-4o-mini"), task="query",
                            complexity="simple")
        _install(state, monkeypatch)
        return _run(route_and_call(TaskType.QUERY, "what is 17 * 23?", complexity_hint=Complexity.SIMPLE)).model

    _world_env(monkeypatch, tmp_path)
    _stub_transport(monkeypatch)
    _use_db(tmp_path / "precision.db", monkeypatch)
    assert served("connected") == "openai/gpt-4o-mini"      # the tier works when the map allows it
    assert served("disconnected").startswith("gemini/")     # and yields to the map when it does not


# ── the generator itself ─────────────────────────────────────────────────────

def test_fixture_is_deterministic_and_matches_the_door_corpus_shape():
    a = [fx.prompt_for(SEED, "hook", i) for i in range(400)]
    assert a == [fx.prompt_for(SEED, "hook", i) for i in range(400)]
    assert a != [fx.prompt_for(SEED + 1, "hook", i) for i in range(400)]
    lens = sorted(len(p) for p in a)
    assert lens[40] < 120 and lens[-40] > 600                 # short questions and long pastes both present
    assert any(any("\u0590" <= c <= "\u05ff" for c in p) for p in a)   # Hebrew present
    s1 = [fx.map_state_for(SEED, "sdk", i) for i in range(50)]
    assert s1 == [fx.map_state_for(SEED, "sdk", i) for i in range(50)]
    assert len({tuple(sorted(s.statuses.items(), key=str)) for s in s1}) > 25   # map states genuinely vary
