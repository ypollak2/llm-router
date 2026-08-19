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

# CHZ-SURF-01: the tier membership sets moved to `llm_router.tool_surface`, which is
# stdlib-only and therefore loadable BY PATH from a routing hook running under an
# interpreter that has no `llm_router` on sys.path. The hooks must be able to answer
# "is this tool registered?" — when they could not, auto-route.py emitted legacy
# tool names under the consolidated default and every hint 404'd.
#
# This module remains the documented home for the *gate*; the sets are re-exported
# so existing imports (`from llm_router.tool_tiers import CORE_TOOLS`) keep working.
# Do NOT redefine them here — one definition, one direction of dependency.
from llm_router.tool_surface import (  # noqa: F401  (re-export)
    CONSOLIDATED_TOOLS,
    CORE_TOOLS,
    ROUTING_TOOLS,
)


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
    if slim == "consolidated":
        return lambda name: name in CONSOLIDATED_TOOLS
    # "off" or any unknown value — register everything
    return lambda name: True


def tier_summary(slim: str) -> str:
    """Return a human-readable summary of the active slim tier."""
    slim = (slim or "off").strip().lower()
    if slim == "core":
        return f"core ({len(CORE_TOOLS)} tools — maximum token savings)"
    if slim == "routing":
        return f"routing ({len(ROUTING_TOOLS)} tools — recommended)"
    if slim == "consolidated":
        return f"consolidated ({len(CONSOLIDATED_TOOLS)} front-door tools — North Star 1.0 surface)"
    return "off (all tools — maximum compatibility)"
