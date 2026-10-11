"""P0.15-b/-c/-d/-e/-f (DISC-PROMOTE-1): an unvalidated local model never leads a chain.

"Unvalidated" = not user-configured, not grandfathered, not graded (nothing is graded
before 16.3, so: not configured and not grandfathered). D-60c = A: such a model is the
LAST entry of a simple (BUDGET) query/generate/analyze chain and absent from every other
chain. D-62 = A: models installed at the first run of an upgrade are grandfathered; a
fresh install grandfathers nothing; the file is written once and never recreated.

Models in the fixtures
    A  ollama/old-a:7b       grandfathered (installed at the first run)
    B  ollama/new-b:7b       new, unvalidated
    C  ollama/new-c:70b      new, unvalidated (the "has a measured ceiling" case: a ceiling is not a grade)
    D  ollama/cfg-d:8b       user-configured
    X  openai_compat/new-x   new, unvalidated
    Y  openai_compat/cfg-y   user-configured
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path

import pytest

from llm_router import model_discovery as md
from tests.fixtures.dyn import homes
from tests.fixtures.dyn.fake_ollama import FakeLocalServer
from tests.fixtures.dyn.net_guard import ForeignHostConnection, net_guard
from tests.fixtures.dyn.router_chain import (
    FIXTURES, INJECTED, FakeConfig, all_chains, build, isolated, pattern,
)

A, B, C, D, X, Y = INJECTED
NON_CRITICAL = {f"BUDGET/{t}" for t in ("QUERY", "GENERATE", "ANALYZE")}
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "llm_router"


@pytest.fixture
def state(tmp_path, monkeypatch):
    """An empty state dir; every variable the gate reads is cleared."""
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    for v in ("LLM_ROUTER_PROMOTE_GATE", "LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS",
              "OLLAMA_MODELS", "LLM_ROUTER_DISCOVERY_TTL_HOURS", "OLLAMA_HOST"):
        monkeypatch.delenv(v, raising=False)
    return home


def first_run_with(state: Path, installed: list[str]) -> None:
    """An upgrade day: discovery.json lists ``installed``, then the first gated call runs."""
    homes.write_discovery(state, [m.removeprefix("ollama/") for m in installed])
    md.ensure_grandfather([m for m in installed])
    assert md.grandfather_status() == "ok"


def world(**kw) -> FakeConfig:
    return FakeConfig(**kw)


FULL = dict(ollama=[A, B, C, D], compat=[X, Y], configured_ollama="cfg-d:8b", configured_compat="cfg-y")


# ═══ P0.15-b: the router door (Ollama and openai-compat) ═══════════════════════

def check_router_door(state: Path) -> dict[str, int]:
    """12 chains: B, C, X are last on 3 and absent from 9; A, D, Y keep their place in 12 of 12."""
    first_run_with(state, [A])
    chains = all_chains(world(**FULL))
    only_validated = all_chains(world(ollama=[A, D], compat=[Y], configured_ollama="cfg-d:8b",
                                      configured_compat="cfg-y"))
    new = {B, C, X}
    last = absent = first = kept = 0
    for key, chain in chains.items():
        assert chain[0] not in new, f"{key}: an unvalidated model leads: {chain}"
        first += chain[0] in new
        present = [m for m in chain if m in new]
        if key in NON_CRITICAL:
            assert chain[-3:] == [B, C, X], f"{key}: unvalidated models are not the tail: {chain}"
            last += 1
        else:
            assert present == [], f"{key}: unvalidated model on a critical chain: {chain}"
            absent += 1
        stripped = [m for m in chain if m not in new]
        assert stripped == only_validated[key], f"{key}: validated models moved: {stripped} != {only_validated[key]}"
        kept += 1
        assert all(m in chain for m in (A, D, Y)) or key.startswith("PREMIUM"), key
    return {"last": last, "absent": absent, "first": first, "front_kept": kept}


def test_router_door_3_last_9_absent_0_first_12_front_kept(state):
    assert check_router_door(state) == {"last": 3, "absent": 9, "first": 0, "front_kept": 12}


@pytest.mark.parametrize("fixture,new", [("ollama_B_only", B), ("compat_X_only", X)])
def test_a_single_new_model_is_last_in_3_of_12_and_absent_from_9(state, fixture, new):
    first_run_with(state, [A])
    chains = all_chains(world(**FIXTURES[fixture]))
    last = [k for k, c in chains.items() if c[-1] == new]
    absent = [k for k, c in chains.items() if new not in c]
    assert sorted(last) == sorted(NON_CRITICAL)
    assert len(absent) == 9 and not any(c[0] == new for c in chains.values())


@pytest.mark.parametrize("policy", ["local-first", "cost", "quality", "quota-exhaustion", "dynamic"])
def test_a_non_balanced_policy_does_not_move_the_tail_forward(state, policy):
    first_run_with(state, [A])
    chain = build("BUDGET", "QUERY", world(**{**FULL, "routing_policy": policy}))
    assert chain[-3:] == [B, C, X], (policy, chain)
    for t in ("CODE",):
        assert not {B, C, X} & set(build("BUDGET", t, world(**{**FULL, "routing_policy": policy})))


def test_configured_by_a_routing_yaml_pin_keeps_its_place(state):
    from llm_router.repo_config import RepoConfig, TaskRouteOverride

    first_run_with(state, [A])
    repo = RepoConfig(routing={"query": TaskRouteOverride(model=B)})
    chain = build("BUDGET", "QUERY", world(**FULL), repo)
    assert chain[0] == B, "a model the user pinned is configured, not unvalidated"
    assert C in chain[-3:] and chain[-2:] == [C, X]


def test_the_agentic_pick_cannot_pin_an_unvalidated_model(state, monkeypatch):
    first_run_with(state, [A])
    monkeypatch.setattr("llm_router.agentic_registry.best_agentic_model", lambda *a, **k: B)
    chain = build("BUDGET", "ANALYZE", world(**FULL))
    assert chain[0] != B and chain[-3:] == [B, C, X]


def test_tail_models_obey_block_providers(state):
    from llm_router.repo_config import RepoConfig

    first_run_with(state, [A])
    chain = build("BUDGET", "QUERY", world(**FULL), RepoConfig(block_providers=["ollama"]))
    assert not any(m.startswith("ollama/") for m in chain) and chain[-1] == X


def test_the_proxy_policy_chain_inherits_the_router_door(state, monkeypatch):
    first_run_with(state, [A])
    from llm_router.proxy import backends

    monkeypatch.setattr("llm_router.config.get_config", lambda: world(**FULL))
    seen = {}
    for text, want_tail in (("What is a closure?", True), ("refactor this function to be async and add tests", False)):
        backends._chain_cache.clear()
        with isolated():
            task, complexity, chain = asyncio.run(backends.policy_chain(text))
        seen[task] = chain
        assert complexity == "simple" and chain[0] not in (B, C, X)
        assert (chain[-3:] == [B, C, X]) is want_tail
        assert want_tail or not {B, C, X} & set(chain)
    backends._chain_cache.clear()
    assert set(seen) == {"query", "code"}
    assert "_build_and_filter_chain" in (SRC / "proxy" / "backends.py").read_text()


# ═══ escape valve: LLM_ROUTER_PROMOTE_GATE=off restores today's chains ═════════

RECORDED = json.loads((ROOT / "tests" / "fixtures" / "dyn" / "p015_main_patterns.json").read_text())


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_gate_off_restores_the_chains_origin_main_builds(state, monkeypatch, name):
    """6 fixtures x 12 chains, compared with chains recorded from origin/main 4e2d7be5."""
    first_run_with(state, [A])
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    got = {k: pattern(v) for k, v in all_chains(world(**FIXTURES[name])).items()}
    assert got == RECORDED[name]


def test_the_recorded_chains_are_not_vacuous(state):
    """With the gate ON the mixed fixture differs from main in exactly the chains it should."""
    first_run_with(state, [A])
    got = {k: pattern(v) for k, v in all_chains(world(**FULL)).items()}
    differs = [k for k in got if got[k] != RECORDED["mixed_all"][k]]
    assert len(differs) == 12 and RECORDED["mixed_all"]["BUDGET/QUERY"][2] == B


def test_gate_off_is_logged(state, monkeypatch, caplog):
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.setattr(md, "_gate_off_logged", False)
    with caplog.at_level("WARNING", logger="llm_router.model_discovery"):
        md.order_for_chain([B], "query", critical=True)
    assert "LLM_ROUTER_PROMOTE_GATE=off" in caplog.text


# ═══ P0.15-e: the hook door ═════════════════════════════════════════════════════

def check_hook_door(state: Path, monkeypatch) -> dict[str, int]:
    from llm_router.hooks import chain_builder as cb

    first_run_with(state, [A])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "cfg-d:8b")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: ["old-a:7b", "new-b:7b", "new-c:70b", "cfg-d:8b"])
    last = absent = 0
    for complexity in ("simple", "moderate", "complex"):
        for task in ("query", "generate", "analyze", "code"):
            chain = cb._build_chain(complexity, "green", task)
            ids = [f"{s.provider}/{s.model}" for s in chain]
            new = [i for i in ids if i in (B, C)]
            if complexity == "simple" and task != "code":
                assert ids[-2:] == [B, C], f"{complexity}/{task}: {ids}"
                assert ids[0] not in (B, C)
                last += 1
            else:
                assert new == [], f"{complexity}/{task}: unvalidated model on a critical chain: {ids}"
                absent += 1
            if not (complexity == "complex" and task in ("code",)):
                assert A in ids and D in ids or complexity == "complex", f"{complexity}/{task}: {ids}"
    return {"last": last, "absent": absent}


def test_hook_door_3_last_9_absent_over_12_chains(state, monkeypatch):
    assert check_hook_door(state, monkeypatch) == {"last": 3, "absent": 9}


def test_hook_high_complexity_branch_no_longer_carries_the_unvalidated_model(state, monkeypatch):
    """The `complex`/`deep_reasoning` branch (chain_builder.py, the ollama + externals returns)."""
    from llm_router.hooks import chain_builder as cb

    first_run_with(state, [A])
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: ["old-a:7b", "new-b:7b"])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for zone in ("green", "yellow", "orange", "red", "critical"):
        for complexity in ("complex", "deep_reasoning"):
            ids = [f"{s.provider}/{s.model}" for s in cb._build_chain(complexity, zone, "query")]
            assert A in ids and B not in ids, (zone, complexity, ids)


def test_build_chain_public_path_still_strips_local_for_qa(state, monkeypatch):
    from llm_router.hooks import chain_builder as cb

    first_run_with(state, [A])
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: ["old-a:7b", "new-b:7b"])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert not any(s.provider == "ollama" for s in cb.build_chain("simple", "green", "query"))
    code = [f"{s.provider}/{s.model}" for s in cb.build_chain("simple", "green", "code")]
    assert A in code and B not in code


def test_gate_off_puts_every_model_in_front_on_the_hook(state, monkeypatch):
    from llm_router.hooks import chain_builder as cb

    first_run_with(state, [A])
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: ["new-b:7b", "old-a:7b"])
    for task in ("query", "code"):
        for complexity in ("simple", "moderate"):
            ids = [f"{s.provider}/{s.model}" for s in cb._build_chain(complexity, "green", task)]
            assert ids[:2] == [B, A]


def test_the_auto_route_hook_keeps_unvalidated_models_out_of_code_classification(state, monkeypatch):
    import importlib.util

    first_run_with(state, [A])
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setattr(md, "available_ollama_models_nowait", lambda **k: ["old-a:7b", "new-b:7b"])
    spec = importlib.util.spec_from_file_location("auto_route_p015", SRC / "hooks" / "auto-route.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.OLLAMA_CODE_MODELS == ["old-a:7b"]
    assert mod.OLLAMA_MODELS == ["old-a:7b", "new-b:7b"]
    assert mod.OLLAMA_MODEL == "old-a:7b"
    assert (ROOT / "hooks" / "auto-route.py").read_bytes() == (SRC / "hooks" / "auto-route.py").read_bytes()


# ═══ P0.15-c: the grandfather file ═════════════════════════════════════════════

def grandfather_bytes(state: Path) -> bytes:
    return (state / "grandfathered_models.json").read_bytes()


def check_grandfather_survives_probes(state: Path) -> None:
    """3 probes that rewrite discovery.json; 4 ids; the upgrade install shape."""
    from llm_router import discover

    homes.write_discovery(state, ["old-a:7b", "old-b:7b"])
    (state / "usage.db").write_bytes(b"")
    with FakeLocalServer(["old-a:7b", "old-b:7b"]) as srv:
        os.environ["OLLAMA_HOST"] = srv.url
        try:
            md.available_ollama_models_nowait(refresh=False)
            md.ensure_grandfather(["ollama/old-a:7b", "ollama/old-b:7b"])
            first = grandfather_bytes(state)
            ids = set(json.loads(first)["ids"])
            assert ids == {"ollama/old-a:7b", "ollama/old-b:7b"}
            srv.set_models(["old-a:7b", "new-z:7b"])                       # old-b removed, new-z pulled
            md._write_cache(md.probe_ollama())                              # probe 1 (model_discovery writer)
            discover._update_discovery_cache([{"name": n} for n in srv.models])   # probe 2 (discover writer)
            srv.set_models(["old-a:7b", "new-z:7b", "new-y:7b"])
            md._write_cache(md.probe_ollama())                              # probe 3
        finally:
            os.environ.pop("OLLAMA_HOST", None)
    cache = json.loads((state / "discovery.json").read_text())["models"]
    assert "ollama/new-z:7b" in cache and "ollama/old-b:7b" not in cache, "discovery.json was really rewritten"
    assert grandfather_bytes(state) == first, "a probe rewrote the grandfather file"


def test_grandfather_file_survives_3_probes(state):
    check_grandfather_survives_probes(state)


def test_matching_is_by_id_only(state):
    first_run_with(state, [A, "ollama/old-b:7b"])
    front, tail = md.order_for_chain(
        [A, "ollama/old-b:7b", "ollama/old-a:13b", "ollama/new-z:7b", "ollama/OLD-A:7B"], "query", critical=False)
    assert front == [A, "ollama/old-b:7b"]
    assert tail == ["ollama/old-a:13b", "ollama/new-z:7b", "ollama/OLD-A:7B"]


def test_an_id_absent_at_first_run_is_not_grandfathered(state):
    first_run_with(state, [A])
    ids = set(json.loads(grandfather_bytes(state))["ids"])
    assert ids == {A} and B not in ids
    front, tail = md.order_for_chain([A, B], "query", critical=False)
    assert (front, tail) == ([A], [B])


def test_a_fresh_install_grandfathers_nothing(state):
    """No discovery.json, profile.yaml or usage.db: the first probe must not look like prior state."""
    with FakeLocalServer(["first-model:7b"]) as srv:
        os.environ["OLLAMA_HOST"] = srv.url
        try:
            assert md.available_ollama_models() == ["first-model:7b"]      # probe -> writes discovery.json
        finally:
            os.environ.pop("OLLAMA_HOST", None)
    assert (state / "discovery.json").exists()
    assert json.loads(grandfather_bytes(state))["ids"] == []
    front, tail = md.order_for_chain(["ollama/first-model:7b"], "query", critical=False)
    assert (front, tail) == ([], ["ollama/first-model:7b"])


def test_profile_yaml_written_first_on_a_fresh_install_is_not_prior_state(state):
    from llm_router import auto_profile

    auto_profile.save_profile("routing: {}\n")
    assert json.loads(grandfather_bytes(state))["ids"] == []
    assert md.order_for_chain([A], "query", critical=False) == ([], [A])


def test_the_bandit_cannot_pull_an_unvalidated_model_forward(state):
    first_run_with(state, [A])
    chain = ["openai/gpt-4o-mini", B, A, "deepseek/deepseek-chat"]
    assert md.sink_unvalidated(chain, "query") == ["openai/gpt-4o-mini", A, "deepseek/deepseek-chat", B]
    import os as _os
    _os.environ["LLM_ROUTER_PROMOTE_GATE"] = "off"
    try:
        assert md.sink_unvalidated(chain, "query") == chain
    finally:
        _os.environ.pop("LLM_ROUTER_PROMOTE_GATE")


@pytest.mark.parametrize("prior", ["discovery.json", "profile.yaml"])
def test_any_one_prior_state_file_makes_it_an_upgrade(state, prior):
    (state / prior).write_text("{}" if prior.endswith(".json") else "x")
    md.ensure_grandfather([A])
    assert json.loads(grandfather_bytes(state))["ids"] == [A]


def test_a_deleted_file_is_not_recreated_and_doctor_says_so(state):
    first_run_with(state, [A])
    (state / "grandfathered_models.json").unlink()
    md.ensure_grandfather([A, B])
    md.order_for_chain([A, B], "query", critical=False)
    assert not (state / "grandfathered_models.json").exists()
    assert md.grandfather_status() == "missing"
    assert md.order_for_chain([A], "query", critical=False) == ([], [A])      # nothing grandfathered thereafter
    from llm_router.commands import doctor

    assert any("deleted and is not recreated" in line for line in doctor._floor_report())


def test_the_file_is_written_atomically_and_only_once(state):
    first_run_with(state, [A])
    first = grandfather_bytes(state)
    md.ensure_grandfather([A, B, C])
    assert grandfather_bytes(state) == first
    assert not list(state.glob(".grandfathered_models.json.*.tmp"))


def test_an_unwritable_home_fails_closed_and_does_not_raise(state, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(os, "link", boom)
    md.ensure_grandfather([A])
    assert md.order_for_chain([A], "query", critical=False) == ([], [A])


# ═══ D-62 with the real cost code (no mocked usage.db), and the review nits ═══

def test_a_real_fresh_install_is_not_an_upgrade_even_though_the_router_creates_usage_db(state):
    """Reviewer repro: cost._get_db creates an EMPTY usage.db before the injection door runs."""
    cfg = world(ollama=[A, B], compat=[X])
    chain = build("BUDGET", "QUERY", cfg, mock_cost=False)
    assert (state / "usage.db").exists(), "the real cost code must have created usage.db (else this proves nothing)"
    assert not (state / "discovery.json").exists()
    assert json.loads(grandfather_bytes(state))["ids"] == []
    assert chain[-3:] == [A, B, X]


def test_an_empty_usage_db_is_not_history_but_a_routed_call_is(state):
    import sqlite3

    db = state / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, model TEXT, provider TEXT)")
    con.commit()
    assert not md._usage_db_has_history(db)
    con.execute("INSERT INTO usage (model, provider) VALUES ('openai/gpt-4o-mini', 'openai')")
    con.commit()
    con.close()
    assert md._usage_db_has_history(db)
    md.ensure_grandfather([A])
    assert json.loads(grandfather_bytes(state))["ids"] == [A]


def test_an_upgrade_with_routing_history_is_grandfathered_through_the_real_router(state):
    import sqlite3

    con = sqlite3.connect(state / "usage.db")
    con.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY, model TEXT, provider TEXT)")
    con.execute("INSERT INTO usage (model, provider) VALUES ('ollama/old-a:7b', 'ollama')")
    con.commit()
    con.close()
    build("BUDGET", "QUERY", world(ollama=[A]), mock_cost=False)
    assert json.loads(grandfather_bytes(state))["ids"] == [A]


def test_auto_detected_openai_compat_models_are_grandfathered_with_ollama_on_upgrade(state):
    homes.write_discovery(state, ["old-a:7b"])
    build("BUDGET", "QUERY", world(ollama=[A], compat=[X]))
    assert set(json.loads(grandfather_bytes(state))["ids"]) == {A, X}


def test_gate_off_does_not_write_the_grandfather_file(state, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    homes.write_discovery(state, ["old-a:7b"])
    md.ensure_grandfather([A])
    build("BUDGET", "QUERY", world(ollama=[A]))
    assert not (state / "grandfathered_models.json").exists() and not (state / "grandfathered_models.init").exists()
    monkeypatch.delenv("LLM_ROUTER_PROMOTE_GATE")
    md.ensure_grandfather([A])                     # switching the gate on later still gets its first run
    assert json.loads(grandfather_bytes(state))["ids"] == [A]


def test_embedding_models_are_not_grandfathered_by_the_discovery_writer(state):
    from llm_router import discover

    homes.write_discovery(state, ["old-a:7b"])
    discover._update_discovery_cache([{"name": "old-a:7b"}, {"name": "nomic-embed-text:latest"}])
    assert json.loads(grandfather_bytes(state))["ids"] == [A]


# ═══ P0.15-f: the caller audit ═════════════════════════════════════════════════

NAMES = ("available_ollama_models", "available_ollama_models_nowait", "all_ollama_models",
         "get_cached_ollama_models", "all_openai_compat_models")
SCAN_ROOTS = (ROOT / "src" / "llm_router", ROOT / "scripts")

#: (path relative to the repo, enclosing function, called name, ordinal of that call in the function)
#: -> (kind, why). `order` calls feed a chain and must sit in a function that calls
#: order_for_chain; `existence` calls only ask "is there / which models exist".
CALLERS: dict[tuple[str, str, str, int], tuple[str, str]] = {
    ("src/llm_router/router.py", "_build_and_filter_chain", "all_ollama_models", 0): ("existence", "provider filter: is any Ollama model there"),
    ("src/llm_router/router.py", "_build_and_filter_chain", "all_ollama_models", 1): ("order", "door D1, Ollama injection"),
    ("src/llm_router/router.py", "_build_and_filter_chain", "all_openai_compat_models", 0): ("existence", "first-run grandfather input (ensure_grandfather); injects nothing"),
    ("src/llm_router/router.py", "_build_and_filter_chain", "all_openai_compat_models", 1): ("order", "door D1, openai-compat injection"),
    ("src/llm_router/hooks/chain_builder.py", "_ollama_models", "available_ollama_models", 0): ("order", "door D2, the hook chain builder"),
    ("src/llm_router/hooks/auto-route.py", "_load_discovered_ollama_split", "available_ollama_models", 0): ("order", "door D2b, hook classifier models (older-llm_router import path)"),
    ("src/llm_router/hooks/auto-route.py", "_load_discovered_ollama_split", "available_ollama_models_nowait", 0): ("order", "door D2b, the _nowait variant"),
    ("src/llm_router/config.py", "ollama_models_for_profile", "all_ollama_models", 0): ("existence", "legacy wrapper; no caller (asserted below)"),
    ("src/llm_router/config.py", "all_ollama_models", "get_cached_ollama_models", 0): ("existence", "the lister; the router gates its result at D1"),
    ("src/llm_router/auto_profile.py", "generate_profile_yaml", "get_cached_ollama_models", 0): ("existence", "profile.yaml tier listing; read only as 'tier present' by dynamic_routing"),
    ("src/llm_router/discover.py", "_scan_ollama", "get_cached_ollama_models", 0): ("existence", "capability scan feeding the dead top-level chain_builder (no src caller)"),
    ("src/llm_router/discover.py", "filter_ollama_by_installed", "get_cached_ollama_models", 0): ("existence", "drops chain entries that are NOT installed; adds none"),
    ("src/llm_router/zero_claude_edit.py", "maybe_replace", "available_ollama_models", 0): ("existence", "is the configured edit model installed"),
    ("src/llm_router/judge.py", "_select_judge_model", "get_cached_ollama_models", 0): ("existence", "picks a grader for a stored row, not a routed answer"),
    ("src/llm_router/hooks/direct_executor.py", "execute_chain", "available_ollama_models", 0): ("existence", "is each chain entry pulled; adds none"),
    ("src/llm_router/model_discovery.py", "first_installed", "available_ollama_models", 0): ("existence", "auxiliary pick (warm-up, compressor), not a routing chain"),
    ("src/llm_router/model_discovery.py", "ensure_grandfather", "all_openai_compat_models", 0): ("existence", "first-run grandfather input (P0.15-2); records ids, injects nothing"),
    ("src/llm_router/model_discovery.py", "<module>", "available_ollama_models", 0): ("existence", "the detached cache refresher"),
    ("scripts/bench_draft_acceptance.py", "preflight", "available_ollama_models", 0): ("existence", "benchmark script: is the model installed"),
}


def _uses_in_source(text: str, rel: str) -> list[tuple[str, str, str, int]]:
    """(file, function, canonical name, line) for every use of the five names in one source text.

    Import aliases are resolved (`from m import available_ollama_models as _a; _a()` is a use of
    available_ollama_models), and `getattr(obj, "all_ollama_models")` counts too."""
    tree = ast.parse(text)
    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            for a in node.names:
                if a.name.rsplit(".", 1)[-1] in NAMES and a.asname:
                    alias[a.asname] = a.name.rsplit(".", 1)[-1]
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(n: ast.AST) -> str:
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return n.name
        return "<module>"

    found = []
    for node in ast.walk(tree):
        # A *use*, not only a call: `asyncio.to_thread(get_cached_ollama_models)` passes the
        # function without calling it and is exactly as much a door.
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = alias.get(node.id, node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in NAMES \
                and isinstance(parents.get(node), ast.Call) \
                and getattr(parents[node].func, "id", getattr(parents[node].func, "attr", "")) in ("getattr", "hasattr"):
            name = node.value
        if name in NAMES:
            found.append((rel, enclosing(node), name, node.lineno))
    return found


def _ordinals(found) -> list[tuple[str, str, str, int, str]]:
    # ordinal = position by line number within (file, function, name)
    out, seen = [], {}
    for rel, fn, name, line in sorted(found):
        n = seen.get((rel, fn, name), 0)
        seen[(rel, fn, name)] = n + 1
        out.append((rel, fn, name, n, f"{rel}:{line}"))
    return out


def _calls() -> list[tuple[str, str, str, int, str]]:
    """Every use of the five names under src/llm_router and scripts: (file, function, name, ordinal, where)."""
    found = []
    for base in SCAN_ROOTS:
        for path in sorted(base.rglob("*.py")):
            found += _uses_in_source(path.read_text(), str(path.relative_to(ROOT)))
    return _ordinals(found)


def order_result_used(source: str, fn: str) -> bool:
    """True when function ``fn`` in ``source`` calls order_for_chain AND uses what it returns:
    every assigned name except ``_tail_unused*`` is read again in the function."""
    node = next((n for n in ast.walk(ast.parse(source))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn), None)
    if node is None:
        return False
    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "order_for_chain"]
    if not calls:
        return False
    loads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    for call in calls:
        owner = next((n for n in ast.walk(node)
                      if isinstance(n, (ast.Assign, ast.Return, ast.Expr)) and getattr(n, "value", None) is call), None)
        if owner is None or isinstance(owner, ast.Expr):
            return False                       # bare call: the result is thrown away
        if isinstance(owner, ast.Return):
            continue
        for t in owner.targets:
            for name in ast.walk(t):
                if isinstance(name, ast.Name) and name.id not in loads and not name.id.startswith("_tail_unused"):
                    return False
    return True


def _function_source(rel: str, fn: str) -> str:
    text = (ROOT / rel).read_text()
    if fn == "<module>":
        return text
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn:
            return ast.get_source_segment(text, node) or ""
    return ""


def find_unmarked(calls) -> list[str]:
    return [f"{where} {name}() in {fn} (#{n})" for rel, fn, name, n, where in calls
            if (rel, fn, name, n) not in CALLERS]


@pytest.mark.timeout(120)  # AST walk of all of src; 30 s default starved under -n auto (#434 CI)
def test_every_caller_of_the_five_list_functions_is_marked_order_or_existence():
    calls = _calls()
    assert len(calls) >= 10, f"the audit saw only {len(calls)} callers; it is not looking at the code"
    unmarked = find_unmarked(calls)
    assert not unmarked, (
        "a new caller of an Ollama/openai-compat list function appeared. If it builds or orders a "
        "chain it MUST go through model_discovery.order_for_chain (mark it `order`); if it only asks "
        "what exists, mark it `existence` with the reason:\n  " + "\n  ".join(unmarked))
    present = {(rel, fn, name, n) for rel, fn, name, n, _ in calls}
    stale = [k for k in CALLERS if k not in present]
    assert not stale, f"audit rows with no matching call (code moved?): {stale}"


def test_every_order_caller_sits_in_a_function_that_calls_the_gate():
    orders = [k for k, (kind, _) in CALLERS.items() if kind == "order"]
    assert len(orders) == 5
    for rel, fn, name, n in orders:
        assert "order_for_chain(" in _function_source(rel, fn), f"{rel}:{fn} injects without the gate"
        assert order_result_used((ROOT / rel).read_text(), fn), f"{rel}:{fn} calls the gate but drops its result"


def test_the_audit_resolves_import_aliases_and_getattr():
    src = ("from llm_router.model_discovery import available_ollama_models as _a\n"
           "def inject():\n    return _a()\n"
           "def sneaky(cfg):\n    return getattr(cfg, 'all_ollama_models')()\n")
    found = _ordinals(_uses_in_source(src, "src/llm_router/_canary.py"))
    assert [(fn, name) for _, fn, name, *_ in found] == [("inject", "available_ollama_models"),
                                                         ("sneaky", "all_ollama_models")]
    assert len(find_unmarked(found)) == 2


def test_the_audit_notices_a_gate_whose_result_is_dropped():
    bare = "def f(models):\n    order_for_chain(models, 'q', critical=False)\n    return models\n"
    unused = "def f(models):\n    front, tail = order_for_chain(models, 'q', critical=False)\n    return models\n"
    half = "def f(models):\n    front, tail = order_for_chain(models, 'q', critical=False)\n    return front\n"
    used = "def f(models):\n    front, tail = order_for_chain(models, 'q', critical=False)\n    return front + tail\n"
    assert not order_result_used(bare, "f")
    assert not order_result_used(unused, "f")
    assert not order_result_used(half, "f")
    assert order_result_used(used, "f")


def test_the_audit_finds_at_least_ten_callers_and_both_kinds():
    calls = _calls()
    kinds = {CALLERS[(rel, fn, name, n)][0] for rel, fn, name, n, _ in calls if (rel, fn, name, n) in CALLERS}
    assert len(calls) >= 10 and kinds == {"order", "existence"}


@pytest.mark.timeout(120)  # AST walk of all of src; 30 s default starved under -n auto (#434 CI)
def test_the_audit_notices_a_new_ungated_caller():
    """Negative control: a fresh injection site (not in the table) is reported."""
    canary = ("src/llm_router/_canary.py", "inject", "all_ollama_models", 0, "src/llm_router/_canary.py:2")
    assert find_unmarked(_calls()) == []
    assert find_unmarked(_calls() + [canary]) == ["src/llm_router/_canary.py:2 all_ollama_models() in inject (#0)"]


def test_the_legacy_wrapper_has_no_caller():
    for base in SCAN_ROOTS:
        for path in base.rglob("*.py"):
            if path.name == "config.py":
                continue
            assert "ollama_models_for_profile(" not in path.read_text(), path


# ═══ a later map reorder must not move the tail forward (#423 follow-up guard) ═══

def test_the_tail_is_appended_after_every_reorder_in_the_router():
    """#423 adds `apply_to_chain` inside `_build_and_filter_chain`. This guard turns red if anything
    that reassigns `models_to_try` is placed AFTER the unvalidated tail is appended."""
    tree = ast.parse((SRC / "router.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_and_filter_chain")
    body = fn.body
    idx = next(i for i, st in enumerate(body)
               if isinstance(st, ast.If) and ast.unparse(st.test) == "_unvalidated_tail")
    after = body[idx + 1:]
    assert after and isinstance(after[-1], ast.Return), "the tail must be the last step before `return`"
    for st in after[:-1]:
        for node in ast.walk(st):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "models_to_try"
                                                     for t in node.targets):
                pytest.fail("models_to_try is reassigned after the unvalidated tail was appended")


# ═══ with a fresh resource map (#423 readers) the tail still does not move ═══

def _ollama_first_map(monkeypatch):
    from llm_router import map_reader

    view = map_reader.MapView(
        known=frozenset({"ollama", "openai_compat", "openai", "anthropic", "deepseek", "gemini"}),
        connected=frozenset({"ollama", "openai_compat", "openai", "anthropic", "deepseek", "gemini"}),
        kinds=frozenset({"local", "api"}),
        rank_by_id={}, rank_by_provider={"ollama": 0, "openai_compat": 1, "deepseek": 2, "anthropic": 3, "openai": 9},
        orders=True)
    monkeypatch.setattr(map_reader, "load_view", lambda *a, **k: view)


@pytest.mark.parametrize("policy", ["balanced", "local-first"])
def test_router_tail_survives_a_fresh_map(state, monkeypatch, policy):
    first_run_with(state, [A])
    _ollama_first_map(monkeypatch)
    chains = all_chains(world(**{**FULL, "routing_policy": policy}))
    new = {B, C, X}
    for key, chain in chains.items():
        assert chain[0] not in new, (key, chain)
        if key in NON_CRITICAL:
            assert chain[-3:] == [B, C, X], (key, chain)
        else:
            assert not new & set(chain), (key, chain)


def test_hook_tail_survives_a_fresh_map(state, monkeypatch):
    """The hook applies the map AFTER the chain is built; `ollama/*` is ranked by provider.

    Today D-14 strips every local model from a Q&A chain, which is the only kind that can carry a
    tail, so the strip is switched off here to exercise the map step on its own."""
    from llm_router.hooks import chain_builder as cb

    monkeypatch.setattr("llm_router.qa_policy.strip_local_for_qa", lambda models, *a, **k: models)

    first_run_with(state, [A])
    _ollama_first_map(monkeypatch)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "cfg-d:8b")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(md, "available_ollama_models", lambda *a, **k: ["old-a:7b", "new-b:7b", "cfg-d:8b"])
    for task in ("generate", "analyze"):
        ids = [f"{s.provider}/{s.model}" for s in cb.build_chain("simple", "green", task)]
        assert B in ids and ids[-1] == B and ids[0] in (A, D), ids
    for task in ("code",):
        ids = [f"{s.provider}/{s.model}" for s in cb.build_chain("simple", "green", task)]
        assert B not in ids and ids[0] in (A, D), ids


# ═══ mutants: each check above turns red when its door is broken (P1.12-a, lite) ═══

def test_mutant_gate_dead_turns_the_router_check_red(state, monkeypatch):
    monkeypatch.setattr("llm_router.router.order_for_chain", lambda models, tc, **k: (list(models), []))
    with pytest.raises(AssertionError):
        check_router_door(state)


def test_mutant_tail_in_front_turns_the_router_check_red(state, monkeypatch):
    real = md.order_for_chain
    monkeypatch.setattr("llm_router.router.order_for_chain",
                        lambda models, tc, **k: ([m for m in models], []) if True else real(models, tc, **k))
    with pytest.raises(AssertionError):
        check_router_door(state)


def test_mutant_grandfather_rewritten_on_every_probe_turns_the_c_check_red(state, monkeypatch):
    real = md.ensure_grandfather

    def rewriting(ids=()):
        for name in ("grandfathered_models.init", "grandfathered_models.json"):
            (md._cache_path().parent / name).unlink(missing_ok=True)
        real(ids)

    monkeypatch.setattr(md, "ensure_grandfather", rewriting)
    monkeypatch.setattr("llm_router.discover.ensure_grandfather", rewriting, raising=False)
    with pytest.raises(AssertionError):
        check_grandfather_survives_probes(state)


def test_mutant_hook_door_ungated_turns_the_e_check_red(state, monkeypatch):
    from llm_router.hooks import chain_builder as cb

    monkeypatch.setattr(cb, "_ollama_models",
                        lambda task_type="code", *, critical=True: (
                            [cb.ModelSpec("ollama", m) for m in ("old-a:7b", "new-b:7b", "new-c:70b", "cfg-d:8b")], []))
    with pytest.raises(AssertionError):
        check_hook_door(state, monkeypatch)


def test_net_guard_refuses_a_foreign_host_canary():
    import socket

    with net_guard() as attempts:
        s = socket.socket()
        s.settimeout(0.2)
        with pytest.raises(ForeignHostConnection):
            s.connect(("192.0.2.1", 80))        # TEST-NET-1: never routable
        s.close()
    assert attempts == ["192.0.2.1"]


def test_fake_ollama_returns_exactly_what_it_is_told():
    import urllib.request

    with FakeLocalServer(["m1:7b", "m2:7b"]) as srv, net_guard() as attempts:
        got = json.loads(urllib.request.urlopen(f"{srv.url}/api/tags", timeout=2).read())
        assert [m["name"] for m in got["models"]] == ["m1:7b", "m2:7b"]
        srv.set_models(["m3:7b"])
        got = json.loads(urllib.request.urlopen(f"{srv.url}/v1/models", timeout=2).read())
        assert [m["id"] for m in got["data"]] == ["m3:7b"]
    assert attempts == [] and srv.requests == ["/api/tags", "/v1/models"]
