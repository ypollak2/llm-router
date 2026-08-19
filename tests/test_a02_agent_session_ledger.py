"""CHZ-AUD-A-02: agent_session_id must reach the execution-ledger session_id so
get_session_accounting(agent_session_id) returns the agent's real activity.

Data-based (no mocks): rows are written to a real temp ledger DB and read back
via the public get_session_accounting API — the exact boundary the audit probed.
"""
import os
from types import SimpleNamespace

import pytest

import llm_router.router as router
from llm_router.router import TaskType, RoutingProfile, _LEDGER_SESSION_OVERRIDE
from llm_router import execution_ledger


@pytest.fixture()
def temp_ledger(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    monkeypatch.delenv("LLM_ROUTER_SESSION_ID", raising=False)
    return db


def _resp():
    return SimpleNamespace(provider="ollama", model="ollama/x",
                           input_tokens=10, output_tokens=5, cost_usd=0.0)


def test_override_attributes_attempt_to_agent_session(temp_ledger):
    tok = _LEDGER_SESSION_OVERRIDE.set("agent-A02")
    try:
        router._emit_ledger_attempt(
            _resp(), "ollama/x", TaskType.QUERY, RoutingProfile.BUDGET,
            event_type="attempt_completed", accepted=True, correlation_id="corr-1",
        )
    finally:
        _LEDGER_SESSION_OVERRIDE.reset(tok)

    acc = execution_ledger.get_session_accounting("agent-A02")
    assert acc.attempt_count >= 1, "agent session saw no attempts — A-02 regression"
    # The row must NOT be attributed to the bare correlation_id.
    assert execution_ledger.get_session_accounting("corr-1").attempt_count == 0


def test_bypass_terminal_attributes_to_agent_session(temp_ledger):
    router._emit_ledger_terminal(
        "corr-2", "bypassed", route_succeeded=True, agent_session_id="agent-B02",
    )
    acc = execution_ledger.get_session_accounting("agent-B02")
    assert acc.terminal_states.get("bypassed", 0) >= 1


def test_override_resets_and_does_not_leak(temp_ledger, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", "env-session")
    # No override set: emit falls through to LLM_ROUTER_SESSION_ID.
    router._emit_ledger_terminal("corr-3", "accepted", route_succeeded=True)
    assert execution_ledger.get_session_accounting("env-session").terminal_states.get("accepted", 0) >= 1
    # Nothing leaked to a stale agent id.
    assert execution_ledger.get_session_accounting("agent-A02").attempt_count == 0


def test_precedence_order():
    """override > LLM_ROUTER_SESSION_ID > correlation_id."""
    assert router._ledger_session_id("corr", "agentX") == "agentX"
    tok = _LEDGER_SESSION_OVERRIDE.set("cv-agent")
    try:
        assert router._ledger_session_id("corr") == "cv-agent"
    finally:
        _LEDGER_SESSION_OVERRIDE.reset(tok)
    os.environ.pop("LLM_ROUTER_SESSION_ID", None)
    assert router._ledger_session_id("corr") == "corr"
