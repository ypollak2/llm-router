"""MCP tools for agent sessions — 6 endpoints.

Surface area:
    llm_router_agent_list             — discovery
    llm_router_agent_start_session    — open a session
    llm_router_agent_route            — route within session (or refuse if budget breached)
    llm_router_agent_check_budget     — non-destructive budget query
    llm_router_agent_complete_session — finalize + return summary
    llm_router_agent_lineage          — full step-by-step trace

Module-level singletons (registry, session_store, lineage_store) are
constructed lazily so importing this module is cheap and doesn't open
SQLite handles until a tool actually fires.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from llm_router.agents.registry import AgentNotFound, AgentRegistry
from llm_router.agents.session import (
    SessionNotFound,
    SessionStore,
    TerminalStateViolation,
)
from llm_router.lineage import LineageStore


# ── Lazy singletons ──────────────────────────────────────────────────────

_registry: AgentRegistry | None = None
_session_store: SessionStore | None = None
_lineage_store: LineageStore | None = None


def _default_config_path() -> Path:
    """Where to look for agents.yaml. Env override > project > package."""
    env_path = os.environ.get("LLM_ROUTER_AGENTS_CONFIG")
    if env_path:
        return Path(env_path)
    # Walk up from cwd to find a project-level config/agents.yaml
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "config" / "agents.yaml"
        if candidate.exists():
            return candidate
    # Fall back to the bundled template (next to this package)
    pkg_dir = Path(__file__).resolve().parent.parent.parent.parent
    return pkg_dir / "config" / "agents.yaml"


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        path = _default_config_path()
        if path.exists():
            _registry = AgentRegistry.from_yaml(path)
        else:
            # Empty registry — every llm_router_agent_* call returns "no agents
            # defined" gracefully rather than crashing on a missing file.
            _registry = AgentRegistry.from_profiles([])
    return _registry


def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


def get_lineage_store() -> LineageStore:
    global _lineage_store
    if _lineage_store is None:
        _lineage_store = LineageStore()
    return _lineage_store


def reset_singletons_for_test(
    registry: AgentRegistry | None = None,
    session_store: SessionStore | None = None,
    lineage_store: LineageStore | None = None,
) -> None:
    """Tests use this to inject isolated stores backed by tmp_path."""
    global _registry, _session_store, _lineage_store
    _registry = registry
    _session_store = session_store
    _lineage_store = lineage_store


# ── Tool implementations (plain async functions; register() wires to MCP) ─

async def llm_router_agent_list() -> dict:
    """List registered agent profiles.

    Returns:
        {"agents": [{"id": ..., "description": ..., "default_budget_usd": ...,
                     "hard_max_budget_usd": ..., "tier_preference": [...],
                     "preferred_chain": ...}, ...]}
    """
    reg = get_registry()
    return {
        "agents": [
            {
                "id": p.id,
                "description": p.description,
                "default_budget_usd": p.default_budget_usd,
                "hard_max_budget_usd": p.hard_max_budget_usd,
                "tier_preference": list(p.tier_preference),
                "preferred_chain": p.preferred_chain,
                "signal_boosts": dict(p.signal_boosts),
            }
            for p in (reg.get(aid) for aid in reg.list_ids())
        ]
    }


async def llm_router_agent_start_session(
    agent_id: str,
    budget_usd: float | None = None,
    parent_session_id: str | None = None,
    framework: str | None = None,
) -> dict:
    """Open a new agent session.

    Args:
        agent_id: must exist in the registry.
        budget_usd: optional override; clamped to [0, profile.hard_max_usd].
            When None, uses profile.default_budget_usd.
        parent_session_id: when this session is spawned by another agent,
            chains for cost rollup.
        framework: which adapter started this session (agno / hermes /
            langgraph / crewai / claude-agent-sdk / pydantic-ai). Stored
            in lineage rows for per-framework reporting.

    Returns:
        {"session_id": ..., "agent_id": ..., "budget_cap_usd": ...}
        or {"error": "agent_not_found", "available_agents": [...]}
    """
    reg = get_registry()
    try:
        profile = reg.get(agent_id)
    except AgentNotFound:
        return {
            "error": "agent_not_found",
            "agent_id": agent_id,
            "available_agents": reg.list_ids(),
        }

    requested = budget_usd if budget_usd is not None else profile.default_budget_usd
    if requested > profile.hard_max_budget_usd:
        requested = profile.hard_max_budget_usd
    if requested <= 0:
        return {"error": "invalid_budget", "requested_usd": requested}

    store = get_session_store()
    session = store.create(
        agent_id=agent_id,
        budget_usd=requested,
        parent_session_id=parent_session_id,
        framework=framework,
    )
    return {
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "budget_cap_usd": session.budget_cap_usd,
        "parent_session_id": session.parent_session_id,
        "framework": session.framework,
    }


async def llm_router_agent_check_budget(session_id: str) -> dict:
    """Non-destructive: get current budget consumption + remaining."""
    try:
        store = get_session_store()
        session = store.get(session_id)
    except SessionNotFound:
        return {"error": "session_not_found", "session_id": session_id}
    return {
        "session_id": session_id,
        "state": session.state.value,
        "cap_usd": session.budget_cap_usd,
        "consumed_usd": session.consumed_usd,
        "remaining_usd": session.remaining_usd,
        "step_count": session.step_count,
    }


async def llm_router_agent_route(
    session_id: str,
    prompt: str,
    task_type: str = "query",
    estimated_cost_usd: float = 0.0,
) -> dict:
    """Route within a session.

    Pre-check: refuse if estimated_cost would breach budget. The router
    integration that calls this should pass a conservative cost estimate
    so we never spend money on a call we'd refuse afterwards.

    v0.0.2 PERFORMS THE BUDGET CHECK ONLY. Wiring this to actually call
    llm_router.router.route_and_call requires touching router.py — that
    lands in v0.0.3 once we agree on the cost-estimation function. For
    now, this tool returns "would_route" + the chosen action so callers
    can do their own dispatch.

    Returns:
        {"would_route": true, "session_id": ..., "step_index": ...}
        or {"error": "budget_would_exceed", ...}
        or {"error": "session_not_found"} / "session_terminal"
    """
    try:
        store = get_session_store()
        session = store.get(session_id)
    except SessionNotFound:
        return {"error": "session_not_found", "session_id": session_id}

    if session.state.is_terminal:
        return {
            "error": "session_terminal",
            "session_id": session_id,
            "state": session.state.value,
        }

    env = store.envelope(session_id)
    if env.would_exceed(estimated_cost_usd):
        return {
            "error": "budget_would_exceed",
            "session_id": session_id,
            "cap_usd": env.cap_usd,
            "consumed_usd": env.consumed_usd,
            "proposed_usd": estimated_cost_usd,
            "remaining_usd": env.remaining_usd,
        }

    return {
        "would_route": True,
        "session_id": session_id,
        "agent_id": session.agent_id,
        "step_index": session.step_count,  # next step index
        "remaining_usd": env.remaining_usd,
        "prompt_fingerprint": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
    }


async def llm_router_agent_complete_session(session_id: str) -> dict:
    """Mark the session COMPLETED and return a summary.

    Returns:
        {"session_id": ..., "state": "completed", "total_cost_usd": ...,
         "step_count": ..., "rollup": {...full descendants}}
    """
    try:
        store = get_session_store()
        session = store.complete(session_id)
    except SessionNotFound:
        return {"error": "session_not_found", "session_id": session_id}
    except TerminalStateViolation as err:
        return {"error": "session_terminal", "detail": str(err)}

    rollup = store.rollup(session_id)
    return {
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "state": session.state.value,
        "consumed_usd": session.consumed_usd,
        "step_count": session.step_count,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
        "rollup": rollup,
    }


async def llm_router_agent_lineage(session_id: str, limit: int = 200) -> dict:
    """Return the full step-by-step trace for a session.

    Joins the agent session row with all lineage rows tagged with the
    session_id, ordered by step_index.
    """
    try:
        store = get_session_store()
        session = store.get(session_id)
    except SessionNotFound:
        return {"error": "session_not_found", "session_id": session_id}

    lineage = get_lineage_store()
    steps = lineage.by_session(session_id)[:limit]
    return {
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "state": session.state.value,
        "step_count": session.step_count,
        "steps": steps,
    }


# ── MCP registration ─────────────────────────────────────────────────────

def register(mcp, should_register=None) -> None:
    """Register the agent tools with the MCP server, honouring the slim gate.

    mcp is the MCPServer instance from llm_router.server. Each tool is exposed
    under the canonical name; descriptions are pulled from the docstrings.
    Under the consolidated default only the rich session tools
    (llm_router_agent_start_session / llm_router_agent_route) register — the simple
    lifecycle actions collapse into the llm_router_session door.
    """
    def _ok(name: str) -> bool:
        return should_register is None or should_register(name)

    for _name, _fn in (
        ("llm_router_agent_list", llm_router_agent_list),
        ("llm_router_agent_start_session", llm_router_agent_start_session),
        ("llm_router_agent_check_budget", llm_router_agent_check_budget),
        ("llm_router_agent_route", llm_router_agent_route),
        ("llm_router_agent_complete_session", llm_router_agent_complete_session),
        ("llm_router_agent_lineage", llm_router_agent_lineage),
    ):
        if _ok(_name):
            mcp.tool()(_fn)
