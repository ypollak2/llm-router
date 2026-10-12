"""P1.6 wiring in SHADOW (PLAN v16 D-72): rules route, the engine only logs.

Per door: shadow mode routes exactly as engine-off does, and writes one shadow row (Decision fields
plus the rules tier, no prompt text). An engine exception never reaches routing. Synthetic prompts
only: these prove mechanics, not accuracy.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import capped_log, engine, failopen

SRC = Path(engine.__file__).parent
HOOKS = SRC / "hooks"
SECRET_WORDS = "zebra-kumquat-7731"  # a marker that must never appear in a shadow row
PROMPT = "explain why the nightly build is slow and compare the two caching strategies " + SECRET_WORDS
LONG = PROMPT + " with more detail " * 40


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    for k in ("LLM_ROUTER_ENGINE_MODE", "LLM_ROUTER_ENGINE_ABSTAIN", "LLM_ROUTER_ENSEMBLE"):
        monkeypatch.delenv(k, raising=False)
    engine.clear_cache(persistent=False)
    yield
    engine.clear_cache(persistent=False)


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def rows() -> list[dict]:
    assert engine.shadow_drain(), "background shadow queue did not drain"
    return capped_log.read_dicts(engine.state_path(engine.SHADOW_LOG))


# Each door: a callable returning what that door routes on.
def _proxy(text):
    from llm_router.proxy import backends

    r = asyncio.run(backends.tier_classify(text, None, anthropic=True))
    return r["task_type"], r["complexity"]


def _proxy_chain(text):
    from llm_router.proxy import backends

    t, c, _chain = asyncio.run(backends.policy_chain(text))
    return t, c


def _gateway(text):
    from llm_router import gateway

    return gateway._classify(text)


def _hook(text):
    r = _load("hook_shadow", "auto-route.py").classify_prompt(text)
    return None if r is None else (r["task_type"], r["complexity"], r["method"])


def _agent(text):
    return _load("agent_shadow", "agent-route.py")._classify(text)


def _gemini(text):
    return asyncio.run(_load("gemini_shadow", "gemini-cli-auto-route.py")._complexity(text))[0]


def _router(text):
    from llm_router import router
    from llm_router.types import RoutingProfile

    cfg = SimpleNamespace(llm_router_profile=RoutingProfile.BALANCED)
    return router._resolve_profile(None, None, None, text, None, cfg)[1].value


def _mcp(text, monkeypatch=None):
    from llm_router import ensemble

    r = asyncio.run(ensemble.classify_for_routing(text))
    return r.inferred_task_type.value, r.complexity.value


@pytest.fixture()
def mcp_rules(monkeypatch):
    """The MCP door's rules path is an LLM; stub the cloud classifier it falls back to (ENSEMBLE=off)."""
    from llm_router import classifier
    from llm_router.types import ClassificationResult, Complexity, TaskType

    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "off")

    async def fake(prompt, **kw):
        return ClassificationResult(complexity=Complexity.MODERATE, confidence=0.9, reasoning="stub",
                                    inferred_task_type=TaskType.ANALYZE, classifier_model="stub",
                                    classifier_cost_usd=0.0, classifier_latency_ms=1.0)

    monkeypatch.setattr(classifier, "classify_complexity", fake)


DOORS = {
    "proxy": (_proxy, "proxy"), "proxy_chain": (_proxy_chain, "proxy"), "gateway": (_gateway, "gateway"),
    "hook": (_hook, "hook"), "agent_route": (_agent, "agent_route"), "gemini_cli": (_gemini, "gemini_cli"),
    "router": (_router, "router"), "mcp": (_mcp, "mcp"),
}


@pytest.mark.parametrize("name", sorted(DOORS))
@pytest.mark.parametrize("prompt", [PROMPT, LONG])
def test_shadow_routes_like_engine_off_and_writes_a_row(name, prompt, monkeypatch, mcp_rules):
    fn, door = DOORS[name]
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "off")
    off = fn(prompt)
    assert rows() == [], "engine off must not write a shadow row"

    monkeypatch.delenv("LLM_ROUTER_ENGINE_MODE")  # the default is shadow
    assert engine.wire_mode() == "shadow"
    shadow = fn(prompt)
    assert shadow == off, (name, shadow, off)
    got = rows()
    assert got and all(r["door"] == door for r in got), (name, got)
    r = got[-1]
    assert r["tier"] in engine.TIERS and r["rules_tier"] in engine.TIERS
    assert r["engine_version"] == engine.ENGINE_VERSION
    assert SECRET_WORDS not in json.dumps(got), "a shadow row must hold no prompt text"
    assert not any(k in r for k in ("text", "prompt"))


def test_hook_gemini_agent_shadow_is_l1_only_and_other_doors_run_the_full_engine():
    _hook(PROMPT)
    _agent(PROMPT)
    _gemini(PROMPT)
    _gateway(PROMPT)
    by_door = {r["door"]: r for r in rows()}
    assert {"hook", "agent_route", "gemini_cli", "gateway"} <= set(by_door)
    for d in ("hook", "agent_route", "gemini_cli"):
        assert by_door[d]["l1_only"] is True and by_door[d]["layer"] == "L1"
    assert by_door["gateway"]["l1_only"] is False
    assert all(r["light"] is True and r["needs_tools"] is None for r in by_door.values())


def test_shadow_never_runs_the_capability_detector(monkeypatch):
    """P0.9-e: the proxy's tier decision must not pay for the detector; neither may its shadow."""
    from llm_router import capabilities

    calls = []
    monkeypatch.setattr(capabilities, "detect_capabilities", lambda *a, **k: calls.append(a))
    _proxy(PROMPT)  # the rules path of tier_classify skips the detector (capabilities=False)
    assert rows() and calls == [], calls


@pytest.mark.parametrize("name", sorted(DOORS))
def test_an_engine_exception_does_not_affect_routing(name, monkeypatch, mcp_rules):
    fn, _door = DOORS[name]
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "off")
    want = fn(PROMPT)

    def boom(*a, **k):
        raise RuntimeError("engine blew up")

    monkeypatch.setattr(engine, "decide", boom)
    monkeypatch.setattr(engine, "_decide_light", boom)
    monkeypatch.setattr(engine, "_compute", boom)
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    monkeypatch.delenv("LLM_ROUTER_ENGINE_MODE")
    assert fn(PROMPT) == want
    assert engine.shadow_drain()
    assert "LR-FO-ENGINE-SHADOW" in seen, (name, seen)
    assert rows() == []


def test_an_unwritable_shadow_log_does_not_affect_routing(monkeypatch):
    want = _gateway(PROMPT)
    assert engine.shadow_drain()

    def full(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(capped_log, "append", full)
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    assert _gateway(PROMPT) == want
    assert engine.shadow_drain() and seen == ["LR-FO-ENGINE-SHADOW"]


def test_server_doors_queue_the_shadow_and_hooks_run_it_inline(monkeypatch):
    import threading

    seen = []
    real = engine._shadow_run
    monkeypatch.setattr(engine, "_shadow_run", lambda *a: seen.append(threading.current_thread().name) or real(*a))
    _gateway(PROMPT)
    _hook(PROMPT)
    assert engine.shadow_drain()
    assert any(n.startswith("llm_router-engine-shadow") for n in seen)          # gateway: worker thread
    assert threading.current_thread().name in seen                                # hook: inline


def test_a_full_shadow_queue_drops_and_records_not_blocks(monkeypatch):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(engine, "_shadow_run", lambda *a: gate.wait(5))
    monkeypatch.setattr(engine, "_SHADOW_QUEUE_MAX", 3)
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    try:
        for _ in range(6):
            engine.shadow(PROMPT, door="gateway", rules_task="query", rules_complexity="simple", background=True)
        assert seen == ["LR-FO-ENGINE-SHADOW-DROP"] * 3
    finally:
        gate.set()
        engine.shadow_drain()


def test_the_hook_survives_an_engine_import_failure(monkeypatch):
    hook = _load("hook_shadow", "auto-route.py")
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "off")
    want = hook.classify_prompt(PROMPT)
    monkeypatch.delenv("LLM_ROUTER_ENGINE_MODE")
    monkeypatch.setattr(engine, "decides", lambda: (_ for _ in ()).throw(ImportError("gone")))
    assert hook.classify_prompt(PROMPT) == want


# ── the mode flag ────────────────────────────────────────────────────────────


def test_mode_defaults_to_shadow_and_decide_is_explicit(monkeypatch):
    assert engine.wire_mode() == "shadow" and not engine.decides()
    for v, mode in (("decide", "decide"), (" DECIDE ", "decide"), ("off", "off"), ("shadow", "shadow"),
                    ("engine", "shadow"), ("", "shadow"), ("1", "shadow")):
        monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", v)
        assert engine.wire_mode() == mode, v
    assert engine.decides() is False  # "1" -> shadow


def test_decide_mode_lets_the_engine_decide_and_writes_no_shadow_row(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    d = engine.decide(PROMPT, door="gateway")
    assert _gateway(PROMPT) == (d.task_type, d.complexity)
    assert rows() == []


def test_mcp_door_keeps_the_llm_first_default_and_engine_is_opt_in(monkeypatch):
    from llm_router import ensemble

    assert ensemble._engine_mode() == "legacy" and ensemble._ensemble_enabled()  # main's default
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "off")
    assert ensemble._engine_mode() == "off" and not ensemble._ensemble_enabled()
    monkeypatch.delenv("LLM_ROUTER_ENSEMBLE")
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    assert ensemble._engine_mode() == "engine" and not ensemble._ensemble_enabled()
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "legacy")
    assert ensemble._engine_mode() == "legacy"


def test_the_mode_env_var_is_registered():
    from llm_router.env_registry import ENV_REGISTRY

    assert ENV_REGISTRY["LLM_ROUTER_ENGINE_MODE"][1] == "engine.py"


def test_shadow_row_records_the_rules_tier_and_agreement():
    d = engine.shadow("what is the capital of Portugal", door="gateway", rules_task="query",
                      rules_complexity="simple")
    r = rows()[-1]
    assert d is not None and r["rules_tier"] == "haiku" and r["rules_complexity"] == "simple"
    assert r["agree"] is (r["tier"] == "haiku" and r["task_type"] == "query")
    assert engine.shadow("x", door="gateway", rules_task=None, rules_complexity=None) is not None
    with pytest.raises(ValueError):
        engine._decide_light("x", door="nope", lexical=False)


def test_shadow_never_records_to_the_decision_ledger_but_decide_does():
    """Shadow reaches ``_compute`` through ``_decide_light``, not ``decide``: a shadow call adds a
    shadow row and no ``decisions.jsonl`` row (P1.7), so a decide-mode door is the only writer."""
    from llm_router import decision_ledger

    for bg in (False, True):
        engine.shadow("refactor the auth module across the codebase", door="hook", rules_task="code",
                      rules_complexity="complex", background=bg)
    assert engine.shadow_drain()
    assert len(rows()) == 2 and decision_ledger.read_ledger() == []
    engine.decide("refactor the auth module across the codebase", door="hook")
    assert len(decision_ledger.read_ledger()) == 1
