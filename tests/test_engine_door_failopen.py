"""P1.6 nits (PR #447 review): server doors fail open to the rules, and tiers.py stays out of
the engine unless it decides.

Every server door that touches the engine (gateway, proxy backends x2, router, MCP ensemble, the
delegate bound check) goes through ``llm_router.engine_door``. If the engine import or ``decides()``
raises, the door returns the rules result and records ``LR-FO-ENGINE-DOOR``.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from llm_router import engine, engine_door, failopen
from tests import test_engine_shadow as _shadow_tests

DOORS, PROMPT = _shadow_tests.DOORS, _shadow_tests.PROMPT
mcp_rules = _shadow_tests.mcp_rules  # the MCP rules-path stub fixture

SERVER_DOORS = ["proxy", "proxy_chain", "gateway", "router", "mcp"]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    for k in ("LLM_ROUTER_ENGINE_MODE", "LLM_ROUTER_ENGINE_ABSTAIN"):
        monkeypatch.delenv(k, raising=False)
    engine.clear_cache(persistent=False)
    yield
    engine.clear_cache(persistent=False)


def _boom(*a, **k):
    raise RuntimeError("engine blew up")


def _break(kind, monkeypatch):
    if kind == "decides":
        monkeypatch.setattr(engine, "decides", _boom)
    else:  # the import itself fails
        monkeypatch.setitem(sys.modules, "llm_router.engine", None)


@pytest.mark.parametrize("mode", ["shadow", "decide"])
@pytest.mark.parametrize("kind", ["decides", "import"])
@pytest.mark.parametrize("name", SERVER_DOORS)
def test_server_door_returns_the_rules_result_and_records(name, kind, mode, monkeypatch, mcp_rules):
    fn, _door = DOORS[name]
    # "engine" makes the MCP door consult decides(); every other door ignores LLM_ROUTER_ENSEMBLE.
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "engine" if name == "mcp" else "off")
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "off")
    want = fn(PROMPT)
    assert want  # the check has something to check
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", mode)
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    _break(kind, monkeypatch)
    assert fn(PROMPT) == want
    assert "LR-FO-ENGINE-DOOR" in seen, (name, kind, mode, seen)


def test_decide_mode_engine_exception_falls_back_to_rules(monkeypatch, mcp_rules):
    """decides() is True but decide() raises: every server door still returns the rules result."""
    for name in SERVER_DOORS:
        fn, _door = DOORS[name]
        monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "engine" if name == "mcp" else "off")
        monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "off")
        want = fn(PROMPT)
        monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
        monkeypatch.setattr(engine, "decide", _boom)
        monkeypatch.setattr(engine, "decide_for_proxy", _boom)
        seen = []
        monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
        assert fn(PROMPT) == want, name
        assert "LR-FO-ENGINE-DOOR" in seen, (name, seen)


@pytest.mark.parametrize("kind", ["decides", "import"])
def test_delegate_bound_check_survives_an_engine_failure(kind, monkeypatch):
    """tools/agentic._delegate classifies through engine_door: failure -> rules complexity."""
    _break(kind, monkeypatch)
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    assert engine_door.decision("fix the bug", door="mcp") is None
    engine_door.shadow("fix the bug", door="mcp", rules_task=None, rules_complexity="simple")
    assert "LR-FO-ENGINE-DOOR" in seen


def _run_policy_decide():
    from llm_router.proxy import tiers

    async def run():
        policy = tiers.ClaudeTierPolicy.load(None)

        class Sticky:
            def get(self, *a, **k): return None
            def record(self, *a, **k): return None

        body = {"model": "claude-opus-4-6", "messages": [{"role": "user", "content": "explain async"}]}
        return await policy.decide(body, "sess-x", Sticky())

    return asyncio.run(run())


@pytest.mark.parametrize("mode", [None, "shadow", "off"])
def test_tiers_does_not_bind_a_session_unless_the_engine_decides(mode, monkeypatch):
    if mode:
        monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", mode)
    calls = []
    monkeypatch.setattr(engine, "bind_session", lambda sid: calls.append(("bind", sid)))
    monkeypatch.setattr(engine, "unbind_session", lambda tok: calls.append(("unbind", tok)))
    assert _run_policy_decide() is not None
    assert calls == []


def test_tiers_binds_in_decide_mode(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    calls = []
    orig_b, orig_u = engine.bind_session, engine.unbind_session
    monkeypatch.setattr(engine, "bind_session", lambda sid: calls.append("bind") or orig_b(sid))
    monkeypatch.setattr(engine, "unbind_session", lambda tok: calls.append("unbind") or orig_u(tok))
    _run_policy_decide()
    assert calls == ["bind", "unbind"]


@pytest.mark.parametrize("kind", ["decides", "import"])
def test_tiers_routing_survives_a_bind_failure(kind, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", "decide")
    want = _run_policy_decide()
    _break(kind, monkeypatch)
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    got = _run_policy_decide()
    assert (got.served_model, got.reason, got.tier) == (want.served_model, want.reason, want.tier)
    assert "LR-FO-ENGINE-DOOR" in seen
