"""Tests for llm_router.agents — session lifecycle, budget envelope, rollups."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from llm_router.agents import (
    AgentProfile,
    AgentRegistry,
    BudgetEnvelope,
    BudgetExceeded,
    SessionState,
    SessionStore,
)
from llm_router.agents.registry import AgentNotFound
from llm_router.agents.session import SessionNotFound, TerminalStateViolation


# ─────────────────────────────────────────────────────────────────────────
# BudgetEnvelope
# ─────────────────────────────────────────────────────────────────────────

def test_envelope_remaining_starts_full():
    env = BudgetEnvelope(cap_usd=1.0)
    assert env.remaining_usd == 1.0
    assert not env.exhausted


def test_envelope_consume_returns_new_instance():
    env = BudgetEnvelope(cap_usd=1.0)
    env2 = env.consume(0.3)
    assert env.consumed_usd == 0.0  # original unchanged
    assert env2.consumed_usd == 0.3
    assert env2.remaining_usd == 0.7


def test_envelope_would_exceed_safe_call():
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.5)
    assert not env.would_exceed(0.4)


def test_envelope_would_exceed_breaching_call():
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.5)
    assert env.would_exceed(0.51)


def test_envelope_consume_negative_raises():
    env = BudgetEnvelope(cap_usd=1.0)
    with pytest.raises(ValueError):
        env.consume(-0.10)


def test_envelope_raise_if_would_exceed():
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.95)
    with pytest.raises(BudgetExceeded) as ctx:
        env.raise_if_would_exceed(0.10, session_id="abc")
    err = ctx.value
    assert err.session_id == "abc"
    assert err.cap_usd == 1.0
    assert err.consumed_usd == 0.95
    assert err.proposed_usd == 0.10


def test_envelope_raise_if_would_not_exceed_silent():
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.5)
    env.raise_if_would_exceed(0.3, session_id="abc")  # no raise


def test_envelope_exhausted_property():
    env = BudgetEnvelope(cap_usd=1.0, consumed_usd=1.0)
    assert env.exhausted
    env2 = BudgetEnvelope(cap_usd=1.0, consumed_usd=0.99)
    assert not env2.exhausted


# ─────────────────────────────────────────────────────────────────────────
# AgentRegistry
# ─────────────────────────────────────────────────────────────────────────

def _profile(id_="agent-1", **kw) -> AgentProfile:
    return AgentProfile(
        id=id_,
        description=kw.get("description", "test"),
        tier_preference=kw.get("tier_preference", ()),
        signal_boosts=kw.get("signal_boosts", {}),
        preferred_chain=kw.get("preferred_chain", ""),
        default_budget_usd=kw.get("default_budget_usd", 0.50),
        hard_max_budget_usd=kw.get("hard_max_budget_usd", 2.00),
    )


def test_registry_get_returns_profile():
    reg = AgentRegistry.from_profiles([_profile("code-reviewer")])
    assert reg.get("code-reviewer").id == "code-reviewer"


def test_registry_unknown_raises():
    reg = AgentRegistry.from_profiles([_profile("code-reviewer")])
    with pytest.raises(AgentNotFound):
        reg.get("nonexistent")


def test_registry_duplicate_id_rejected():
    with pytest.raises(ValueError):
        AgentRegistry.from_profiles([_profile("dup"), _profile("dup")])


def test_registry_list_ids_sorted():
    reg = AgentRegistry.from_profiles(
        [_profile("zebra"), _profile("alpha"), _profile("middle")]
    )
    assert reg.list_ids() == ["alpha", "middle", "zebra"]


def test_registry_contains():
    reg = AgentRegistry.from_profiles([_profile("x")])
    assert "x" in reg
    assert "y" not in reg


def test_registry_from_yaml_parses_default_template(tmp_path):
    """Verify the shipped config/agents.yaml template parses correctly."""
    project_root = Path(__file__).resolve().parent.parent
    template = project_root / "config" / "agents.yaml"
    reg = AgentRegistry.from_yaml(template)
    assert "code-reviewer" in reg
    assert "trend-researcher" in reg
    assert "tdd-guide" in reg
    cr = reg.get("code-reviewer")
    assert cr.preferred_chain == "code_chain"
    assert cr.signal_boosts.get("code_keywords") == 1.5
    assert cr.default_budget_usd == 0.50
    assert cr.hard_max_budget_usd == 2.00


def test_registry_from_yaml_rejects_default_above_hard_max(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "agents:\n"
        "  - id: oops\n"
        "    description: too greedy\n"
        "    budget:\n"
        "      default_usd: 10.0\n"
        "      hard_max_usd: 5.0\n"
    )
    with pytest.raises(ValueError, match="default_usd"):
        AgentRegistry.from_yaml(bad_yaml)


# ─────────────────────────────────────────────────────────────────────────
# SessionStore lifecycle
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "sessions.db")


def test_create_session_returns_active(store: SessionStore):
    session = store.create(agent_id="reviewer", budget_usd=0.50)
    assert session.state == SessionState.ACTIVE
    assert session.budget_cap_usd == 0.50
    assert session.consumed_usd == 0.0
    assert session.step_count == 0
    assert session.session_id  # uuid present


def test_create_rejects_non_positive_budget(store: SessionStore):
    with pytest.raises(ValueError):
        store.create(agent_id="x", budget_usd=0.0)
    with pytest.raises(ValueError):
        store.create(agent_id="x", budget_usd=-1.0)


def test_get_unknown_session_raises(store: SessionStore):
    with pytest.raises(SessionNotFound):
        store.get("nonexistent-uuid")


def test_record_step_increments_consumed(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    updated = store.record_step(s.session_id, cost_usd=0.10)
    assert updated.consumed_usd == 0.10
    assert updated.step_count == 1
    assert updated.state == SessionState.ACTIVE


def test_record_step_multiple_accumulates(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    store.record_step(s.session_id, cost_usd=0.10)
    store.record_step(s.session_id, cost_usd=0.20)
    final = store.get(s.session_id)
    assert final.consumed_usd == pytest.approx(0.30)
    assert final.step_count == 2


def test_record_step_breaches_budget_raises_and_terminates(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=0.20)
    store.record_step(s.session_id, cost_usd=0.10)  # ok
    with pytest.raises(BudgetExceeded):
        store.record_step(s.session_id, cost_usd=0.50)  # breaches
    final = store.get(s.session_id)
    assert final.state == SessionState.BUDGET_EXCEEDED
    assert final.completed_at is not None


def test_record_step_on_terminal_session_rejected(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    store.complete(s.session_id)
    with pytest.raises(TerminalStateViolation):
        store.record_step(s.session_id, cost_usd=0.10)


def test_complete_transitions_to_completed(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    completed = store.complete(s.session_id)
    assert completed.state == SessionState.COMPLETED
    assert completed.completed_at is not None


def test_complete_idempotent(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    first = store.complete(s.session_id)
    second = store.complete(s.session_id)
    assert first.session_id == second.session_id
    assert first.completed_at == second.completed_at


def test_complete_rejects_errored_session(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    store.error(s.session_id)
    with pytest.raises(TerminalStateViolation):
        store.complete(s.session_id)


def test_check_budget_returns_safe(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    assert store.check_budget(s.session_id, prospective_cost_usd=0.5) is True


def test_check_budget_returns_unsafe(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    store.record_step(s.session_id, cost_usd=0.8)
    assert store.check_budget(s.session_id, prospective_cost_usd=0.5) is False


# ─────────────────────────────────────────────────────────────────────────
# Nested sessions + rollup
# ─────────────────────────────────────────────────────────────────────────

def test_children_returns_spawned_sessions(store: SessionStore):
    parent = store.create(agent_id="orchestrator", budget_usd=5.0)
    child_a = store.create(
        agent_id="reviewer", budget_usd=1.0, parent_session_id=parent.session_id
    )
    child_b = store.create(
        agent_id="researcher", budget_usd=1.0, parent_session_id=parent.session_id
    )
    children = store.children(parent.session_id)
    assert {c.session_id for c in children} == {child_a.session_id, child_b.session_id}


def test_rollup_includes_descendants(store: SessionStore):
    parent = store.create(agent_id="orchestrator", budget_usd=5.0)
    child = store.create(
        agent_id="reviewer", budget_usd=1.0, parent_session_id=parent.session_id
    )
    grandchild = store.create(
        agent_id="tdd", budget_usd=1.0, parent_session_id=child.session_id
    )
    store.record_step(parent.session_id, cost_usd=0.10)
    store.record_step(child.session_id, cost_usd=0.20)
    store.record_step(grandchild.session_id, cost_usd=0.05)

    rollup = store.rollup(parent.session_id)
    assert rollup["total_cost_usd"] == pytest.approx(0.35)
    assert rollup["total_steps"] == 3
    assert rollup["descendant_session_count"] == 2


def test_rollup_no_descendants(store: SessionStore):
    s = store.create(agent_id="solo", budget_usd=1.0)
    store.record_step(s.session_id, cost_usd=0.10)
    rollup = store.rollup(s.session_id)
    assert rollup["total_cost_usd"] == 0.10
    assert rollup["descendant_session_count"] == 0


def test_by_agent_returns_sessions_for_one_agent(store: SessionStore):
    s1 = store.create(agent_id="reviewer", budget_usd=1.0)
    s2 = store.create(agent_id="reviewer", budget_usd=1.0)
    _other = store.create(agent_id="researcher", budget_usd=1.0)
    sessions = store.by_agent("reviewer")
    assert {s.session_id for s in sessions} == {s1.session_id, s2.session_id}


# ─────────────────────────────────────────────────────────────────────────
# Framework attribution
# ─────────────────────────────────────────────────────────────────────────

def test_framework_persists_through_lifecycle(store: SessionStore):
    s = store.create(agent_id="reviewer", budget_usd=1.0, framework="agno")
    assert s.framework == "agno"
    store.record_step(s.session_id, cost_usd=0.1)
    assert store.get(s.session_id).framework == "agno"


# ─────────────────────────────────────────────────────────────────────────
# MCP tool surface
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_tools(tmp_path, monkeypatch):
    """Wire the tools module to tmp_path SQLite + a minimal registry."""
    from llm_router.lineage import LineageStore
    from llm_router.tools import agents as tool_mod

    reg = AgentRegistry.from_profiles(
        [
            _profile(
                id_="code-reviewer",
                default_budget_usd=0.50,
                hard_max_budget_usd=2.00,
                tier_preference=("mid", "premium"),
                preferred_chain="code_chain",
                signal_boosts={"code_keywords": 1.5},
            ),
        ]
    )
    session_store = SessionStore(db_path=tmp_path / "sessions.db")
    lineage_store = LineageStore(db_path=tmp_path / "lineage.db")
    tool_mod.reset_singletons_for_test(
        registry=reg, session_store=session_store, lineage_store=lineage_store
    )
    yield tool_mod
    tool_mod.reset_singletons_for_test()


def test_tool_agent_list(isolated_tools):
    result = asyncio.run(isolated_tools.llm_router_agent_list())
    assert len(result["agents"]) == 1
    cr = result["agents"][0]
    assert cr["id"] == "code-reviewer"
    assert cr["default_budget_usd"] == 0.50


def test_tool_start_session_with_default_budget(isolated_tools):
    result = asyncio.run(
        isolated_tools.llm_router_agent_start_session(agent_id="code-reviewer")
    )
    assert "session_id" in result
    assert result["budget_cap_usd"] == 0.50


def test_tool_start_session_unknown_agent(isolated_tools):
    result = asyncio.run(
        isolated_tools.llm_router_agent_start_session(agent_id="unknown")
    )
    assert result["error"] == "agent_not_found"
    assert "code-reviewer" in result["available_agents"]


def test_tool_start_session_clamps_to_hard_max(isolated_tools):
    result = asyncio.run(
        isolated_tools.llm_router_agent_start_session(
            agent_id="code-reviewer", budget_usd=100.0
        )
    )
    assert result["budget_cap_usd"] == 2.00  # clamped


def test_tool_route_refuses_when_budget_breached(isolated_tools):
    start = asyncio.run(
        isolated_tools.llm_router_agent_start_session(
            agent_id="code-reviewer", budget_usd=0.10
        )
    )
    sid = start["session_id"]
    refused = asyncio.run(
        isolated_tools.llm_router_agent_route(
            session_id=sid, prompt="x", estimated_cost_usd=0.20
        )
    )
    assert refused["error"] == "budget_would_exceed"


def test_tool_route_allows_within_budget(isolated_tools):
    start = asyncio.run(
        isolated_tools.llm_router_agent_start_session(agent_id="code-reviewer")
    )
    sid = start["session_id"]
    ok = asyncio.run(
        isolated_tools.llm_router_agent_route(
            session_id=sid, prompt="review src/auth.py", estimated_cost_usd=0.05
        )
    )
    assert ok["would_route"] is True
    assert ok["agent_id"] == "code-reviewer"
    assert "prompt_fingerprint" in ok


def test_tool_complete_returns_rollup(isolated_tools):
    start = asyncio.run(
        isolated_tools.llm_router_agent_start_session(agent_id="code-reviewer")
    )
    sid = start["session_id"]
    done = asyncio.run(isolated_tools.llm_router_agent_complete_session(session_id=sid))
    assert done["state"] == "completed"
    assert "rollup" in done


def test_tool_check_budget_returns_state(isolated_tools):
    start = asyncio.run(
        isolated_tools.llm_router_agent_start_session(agent_id="code-reviewer")
    )
    sid = start["session_id"]
    status = asyncio.run(isolated_tools.llm_router_agent_check_budget(session_id=sid))
    assert status["cap_usd"] == 0.50
    assert status["consumed_usd"] == 0.0
    assert status["remaining_usd"] == 0.50
    assert status["state"] == "active"
