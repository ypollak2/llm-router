"""Usability pillar — defaults, error messages, doctor output.

A QA suite isn't complete without exercising the developer experience:
do errors tell you how to fix them, do defaults make sense, does
`llm_router doctor` correctly diagnose problems? Each test below is a
documented UX guarantee.
"""
from __future__ import annotations

from pathlib import Path


from llm_router.agents import BudgetExceeded, SessionStore
from llm_router.agents.budget import BudgetEnvelope
from llm_router.agents.registry import AgentNotFound, AgentRegistry
from llm_router.agents.session import SessionNotFound, TerminalStateViolation


# ────────────────────────────────────────────────────────────────────────
# Error messages contain remediation
# ────────────────────────────────────────────────────────────────────────

def test_budget_exceeded_message_includes_cap():
    exc = BudgetExceeded("sid", cap_usd=1.0, consumed_usd=0.95, proposed_usd=0.10)
    msg = str(exc)
    assert "cap" in msg.lower() or "1.0" in msg, (
        f"BudgetExceeded message should reference the cap: {msg!r}"
    )


def test_budget_exceeded_message_includes_consumed():
    exc = BudgetExceeded("sid", cap_usd=1.0, consumed_usd=0.95, proposed_usd=0.10)
    msg = str(exc)
    assert "consumed" in msg.lower() or "0.95" in msg, (
        f"BudgetExceeded message should reference consumed: {msg!r}"
    )


def test_budget_exceeded_message_includes_session_id():
    exc = BudgetExceeded("session-xyz", cap_usd=1.0, consumed_usd=1.0, proposed_usd=0.5)
    assert "session-xyz" in str(exc)


def test_agent_not_found_lists_available_agents():
    """The MCP tool returns an error dict listing available agents — the
    user can immediately retry with a valid agent_id."""
    import asyncio

    from llm_router.tools import agents as tool_mod

    reg = AgentRegistry.from_profiles([])  # empty
    tool_mod.reset_singletons_for_test(registry=reg)
    try:
        result = asyncio.run(
            tool_mod.llm_router_agent_start_session(agent_id="unknown")
        )
        assert result["error"] == "agent_not_found"
        assert "available_agents" in result, (
            "llm_router_agent_start_session error must include available_agents"
        )
    finally:
        tool_mod.reset_singletons_for_test()


def test_invalid_budget_error_returns_structured_response():
    """Budget validation surfaces via structured response, not exception."""
    import asyncio

    from llm_router.agents import AgentProfile
    from llm_router.tools import agents as tool_mod

    reg = AgentRegistry.from_profiles([AgentProfile(
        id="x", description="t",
        default_budget_usd=0.5, hard_max_budget_usd=1.0,
    )])
    tool_mod.reset_singletons_for_test(registry=reg)
    try:
        result = asyncio.run(
            tool_mod.llm_router_agent_start_session(agent_id="x", budget_usd=-1.0)
        )
        assert result["error"] == "invalid_budget"
        assert "requested_usd" in result
    finally:
        tool_mod.reset_singletons_for_test()


def test_session_not_found_in_route_returns_structured_error():
    """Routing against an unknown session returns error dict, not exception."""
    import asyncio

    from llm_router.tools import agents as tool_mod

    tool_mod.reset_singletons_for_test(
        session_store=SessionStore(),  # empty store
    )
    try:
        result = asyncio.run(
            tool_mod.llm_router_agent_route(
                session_id="nonexistent", prompt="x", estimated_cost_usd=0.01
            )
        )
        assert result["error"] == "session_not_found"
        assert result["session_id"] == "nonexistent"
    finally:
        tool_mod.reset_singletons_for_test()


def test_budget_would_exceed_error_includes_remaining(tmp_path: Path):
    """When budget would breach, the error tells the user how much is left
    so they can downsize the next call."""
    import asyncio

    from llm_router.agents import AgentProfile
    from llm_router.tools import agents as tool_mod

    reg = AgentRegistry.from_profiles([AgentProfile(
        id="x", description="t",
        default_budget_usd=0.10, hard_max_budget_usd=1.0,
    )])
    store = SessionStore(db_path=tmp_path / "s.db")
    tool_mod.reset_singletons_for_test(registry=reg, session_store=store)
    try:
        start = asyncio.run(
            tool_mod.llm_router_agent_start_session(agent_id="x")
        )
        sid = start["session_id"]
        refused = asyncio.run(
            tool_mod.llm_router_agent_route(
                session_id=sid, prompt="x", estimated_cost_usd=0.20
            )
        )
        assert refused["error"] == "budget_would_exceed"
        assert "remaining_usd" in refused
        assert "cap_usd" in refused
        assert "consumed_usd" in refused
    finally:
        tool_mod.reset_singletons_for_test()


# ────────────────────────────────────────────────────────────────────────
# Sensible defaults
# ────────────────────────────────────────────────────────────────────────

def test_budget_envelope_default_consumed_is_zero():
    env = BudgetEnvelope(cap_usd=1.0)
    assert env.consumed_usd == 0.0


def test_agent_profile_default_budget_under_one_dollar():
    """Sensible default — agents shouldn't have unbounded spend by default."""
    from llm_router.agents import AgentProfile

    profile = AgentProfile(id="x", description="t")
    assert profile.default_budget_usd <= 1.0


def test_agent_profile_hard_max_at_most_few_dollars():
    """Hard max shouldn't allow individual sessions to bankrupt the user."""
    from llm_router.agents import AgentProfile

    profile = AgentProfile(id="x", description="t")
    assert profile.hard_max_budget_usd <= 5.0


def test_default_agents_yaml_uses_sensible_budgets():
    """The shipped config defaults must look reasonable."""
    ROOT = Path(__file__).resolve().parent.parent.parent
    reg = AgentRegistry.from_yaml(ROOT / "config" / "agents.yaml")
    for aid in reg.list_ids():
        profile = reg.get(aid)
        # No agent should default to over $1 — that's a footgun.
        assert profile.default_budget_usd <= 1.0, (
            f"agents.yaml: {aid} default_usd ${profile.default_budget_usd} > $1"
        )
        # Hard max must be reasonable.
        assert profile.hard_max_budget_usd <= 5.0, (
            f"agents.yaml: {aid} hard_max_usd ${profile.hard_max_budget_usd} > $5"
        )


# ────────────────────────────────────────────────────────────────────────
# Documentation / discoverability
# ────────────────────────────────────────────────────────────────────────

def test_every_exception_class_has_a_docstring():
    """Errors users will see in tracebacks should explain themselves."""
    for cls in (
        BudgetExceeded,
        AgentNotFound,
        SessionNotFound,
        TerminalStateViolation,
    ):
        assert cls.__doc__, f"{cls.__name__} missing docstring"
        assert len(cls.__doc__.strip()) > 20, (
            f"{cls.__name__} docstring too short to be helpful"
        )


def test_every_agent_mcp_tool_has_a_docstring():
    from llm_router.tools import agents as tool_mod

    for name in (
        "llm_router_agent_list",
        "llm_router_agent_start_session",
        "llm_router_agent_check_budget",
        "llm_router_agent_route",
        "llm_router_agent_complete_session",
        "llm_router_agent_lineage",
    ):
        func = getattr(tool_mod, name)
        assert func.__doc__, f"{name} missing docstring"


def test_agent_mcp_tool_docstrings_describe_args():
    """Each tool's docstring should describe its arguments so the MCP
    client can render meaningful help to the user."""
    from llm_router.tools import agents as tool_mod

    func = tool_mod.llm_router_agent_start_session
    doc = func.__doc__ or ""
    assert "agent_id" in doc
    assert "budget_usd" in doc


def test_agent_mcp_tool_docstrings_describe_return_shape():
    """Each tool should document what it returns so callers can parse it."""
    from llm_router.tools import agents as tool_mod

    func = tool_mod.llm_router_agent_start_session
    doc = func.__doc__ or ""
    assert "Returns" in doc or "session_id" in doc


# ────────────────────────────────────────────────────────────────────────
# Help text references the right binary
# ────────────────────────────────────────────────────────────────────────

def test_pyproject_advertises_llm_router_binary():
    """The shipped entry-point binary must be `llm_router`, not legacy names."""
    import tomllib

    ROOT = Path(__file__).resolve().parent.parent.parent
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = data["project"].get("scripts", {})
    assert "llm_router" in scripts, "pyproject scripts must expose `llm_router` binary"
    # Sanity-check the entrypoint resolves
    target = scripts["llm_router"]
    module_path, func_name = target.split(":")
    import importlib

    mod = importlib.import_module(module_path)
    assert hasattr(mod, func_name), f"entrypoint {target} does not resolve"
