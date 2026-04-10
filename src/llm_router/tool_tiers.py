"""Tool slim mode — tiered tool registration for token budget management.

Registering all 41 tools injects ~8,000 tokens into every Claude session,
degrading routing accuracy past 20–30K context tokens. Slim mode solves
this by registering only the tools appropriate for the active tier.

Three tiers (controlled via LLM_ROUTER_SLIM env var):
  off      — all tools registered (default, backward-compatible)
  routing  — 12 core routing + admin tools (recommended for most users)
  core     — 4 essential tools only (maximum token savings)

Usage in server.py:
    from llm_router.tool_tiers import make_should_register
    gate = make_should_register(get_config().llm_router_slim)
    routing.register(mcp, gate)
"""

from __future__ import annotations

from typing import Callable

CORE_TOOLS: frozenset[str] = frozenset({
    "llm_query",
    "llm_code",
    "llm_research",
    "llm_usage",
})
"""4-tool tier — essential tools only. Maximum token savings (~7,500 tokens saved)."""

ROUTING_TOOLS: frozenset[str] = CORE_TOOLS | frozenset({
    "llm_analyze",
    "llm_generate",
    "llm_classify",
    "llm_route",
    "llm_auto",
    "llm_check_usage",
    "llm_set_profile",
    "llm_health",
    "llm_session_spend",
    "llm_savings",
    "llm_reroute",
    "llm_select_agent",
})
"""12-tool tier — routing + core admin tools. Recommended for most users (~5,000 tokens saved)."""


def make_should_register(slim: str) -> Callable[[str], bool]:
    """Return a predicate that controls which tools are registered at startup.

    Args:
        slim: One of "off", "routing", or "core".
              Any other value defaults to "off" (all tools registered).

    Returns:
        Callable that takes a tool name and returns True if it should be registered.
    """
    slim = (slim or "off").strip().lower()

    if slim == "core":
        return lambda name: name in CORE_TOOLS
    if slim == "routing":
        return lambda name: name in ROUTING_TOOLS
    # "off" or any unknown value — register everything
    return lambda name: True


def tier_summary(slim: str) -> str:
    """Return a human-readable summary of the active slim tier."""
    slim = (slim or "off").strip().lower()
    if slim == "core":
        return f"core ({len(CORE_TOOLS)} tools — maximum token savings)"
    if slim == "routing":
        return f"routing ({len(ROUTING_TOOLS)} tools — recommended)"
    return "off (all 43 tools — maximum compatibility)"
