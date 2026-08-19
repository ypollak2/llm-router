"""Consolidated tool surface — North Star P4 / 1.0 direction (Docs/archive/TOOL_SURFACE_PROPOSAL.md).

These are the front-door tool names from the 73→11 proposal, registered ALONGSIDE
the existing tools (nothing is removed — this is a NON-BREAKING alias layer that
sets up the 1.0 cutover). `llm_act` is the agentic *execution* door: the tool
enforcement steers operational work to, and — like every ``llm_*`` tool — calling
it clears an enforcement lock, so a task that needs to *do* things always has an
unblocked path (no wrong-tool dead-end).
"""
from __future__ import annotations

from mcp.server.mcpserver import Context

from llm_router.tools.admin import (
    llm_budget,
    llm_cache_clear,
    llm_gain,
    llm_health,
    llm_import_profile,
    llm_policy,
    llm_providers,
    llm_savings,
    llm_session_savings,
    llm_session_spend,
    llm_set_profile,
    llm_usage,
)
from llm_router.tools.agents import (
    llm_router_agent_check_budget,
    llm_router_agent_complete_session,
    llm_router_agent_lineage,
    llm_router_agent_list,
)
from llm_router.tool_surface import DEPRECATED_TOOLS as _DEPRECATED_TOOLS
from llm_router.tools.agentic import llm_delegate
from llm_router.tools.text import (
    llm_analyze,
    llm_code,
    llm_generate,
    llm_query,
    llm_research,
)

# tier → the completion tools' complexity vocabulary
_TIER_TO_COMPLEXITY = {"fast": "simple", "balanced": "moderate", "best": "complex"}

# 1.0 cutover step 3: the legacy-tool → front-door migration map. The DATA now lives
# in `llm_router.tool_surface` (stdlib-only) so the routing HOOKS can consume the same map
# without importing mcp/litellm. A private copy here is exactly what let auto-route.py
# drift and emit unregistered tool names (CHZ-SURF-01). Re-exported for backward
# compatibility — do not redefine it here.
DEPRECATED_TOOLS: dict[str, str] = _DEPRECATED_TOOLS


def door_for_tool(name: str) -> str:
    """Return the consolidated front door for a legacy tool, or the name unchanged
    if it has no door (e.g. it's already a door, or stays as-is toward 1.0).

    NOTE: this collapses llm_query/llm_code/… to a bare ``llm`` and so DROPS the
    specialization. Anything that TELLS A CALLER what to invoke must instead use
    :func:`llm_router.tool_surface.resolve`, which preserves it as ``llm(task="code")``.
    """
    return DEPRECATED_TOOLS.get(name, name)


async def llm_act(task: str, budget_usd: float = 1.0, context: str = "") -> str:
    """Agentic execution — do a real task end-to-end: decompose into milestones,
    run them on the cheapest capable tier *with tools* (files/commands/verify),
    escalate on failure without redoing done work, and return an honest JSON
    result (outcome, per-milestone status, events, savings). This is the 1.0 name
    for agentic delegation; currently a thin alias of ``llm_delegate``.

    *context* is optional conversation context handed to the delegated agents."""
    return await llm_delegate(task, budget_usd=budget_usd, context=context)


async def llm(
    prompt: str,
    ctx: Context,
    task: str = "auto",
    tier: str = "balanced",
    context: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """Unified COMPLETION door — one text-in→text-out entry that routes to the
    right cost tier internally, so callers don't pre-pick a tool. *task* selects
    the specialization (auto/query, analyze, code, research, generate); *tier*
    (fast/balanced/best) maps to the model complexity. The 1.0 name that collapses
    llm_query/analyze/code/research/generate; those remain as aliases underneath."""
    complexity = _TIER_TO_COMPLEXITY.get((tier or "").lower(), "moderate")
    t = (task or "auto").lower()
    if t == "research":
        return await llm_research(prompt, ctx, system_prompt=system_prompt, context=context)
    if t == "analyze":
        return await llm_analyze(prompt, ctx, complexity=complexity,
                                 system_prompt=system_prompt, context=context)
    if t == "code":
        return await llm_code(prompt, ctx, complexity=complexity,
                              system_prompt=system_prompt, context=context)
    if t == "generate":
        return await llm_generate(prompt, ctx, complexity=complexity,
                                  system_prompt=system_prompt, context=context)
    # auto / query — the general default
    return await llm_query(prompt, ctx, complexity=complexity,
                           system_prompt=system_prompt, context=context)


async def llm_router_status(view: str = "summary", period: str = "today") -> str:
    """Read-only status/observability door — collapses the many llm_* reporting
    tools into one *view* selector: summary/savings · session_savings · spend ·
    usage · health · providers · gain. The old tools remain as aliases underneath."""
    v = (view or "summary").lower()
    if v in ("savings", "summary"):
        return await llm_savings()
    if v in ("session_savings", "session-savings"):
        return await llm_session_savings()
    if v in ("spend", "session_spend"):
        return await llm_session_spend()
    if v == "usage":
        return await llm_usage(period=period)
    if v == "health":
        return await llm_health()
    if v == "providers":
        return await llm_providers()
    if v == "gain":
        return await llm_gain(period=period)
    return await llm_savings()


async def llm_router_admin(action: str, value: str = "") -> str:
    """Config/admin door — collapses the mutating/config llm_* tools into one
    *action* selector: set_profile (value=profile) · import_profile (value=url) ·
    clear_cache · policy · budget. Old tools stay as aliases underneath."""
    a = (action or "").lower()
    if a == "set_profile":
        return await llm_set_profile(value)
    if a == "import_profile":
        return await llm_import_profile(url=value)
    if a == "clear_cache":
        return await llm_cache_clear()
    if a == "policy":
        return await llm_policy()
    if a == "budget":
        return await llm_budget()
    return f"unknown admin action: {action!r} (try set_profile/import_profile/clear_cache/policy/budget)"


async def llm_router_session(action: str, session_id: str = "", limit: int = 200) -> dict:
    """Agent-session door — collapses the simple llm_router_agent_* lifecycle tools
    into one *action* selector: list · check_budget · complete · lineage (all take
    a session_id, or none). start/route carry richer params — call those tools
    directly. Old tools stay registered underneath."""
    a = (action or "").lower()
    if a == "list":
        return await llm_router_agent_list()
    if a == "check_budget":
        return await llm_router_agent_check_budget(session_id)
    if a == "complete":
        return await llm_router_agent_complete_session(session_id)
    if a == "lineage":
        return await llm_router_agent_lineage(session_id, limit=limit)
    return {"error": f"unknown/rich session action: {action!r}; "
                     "use list/check_budget/complete/lineage, or start/route directly"}


def register(mcp, should_register=None) -> None:
    """Register the consolidated front-door tools (aliases; old tools stay)."""
    if should_register is None or should_register("llm_act"):
        mcp.tool()(llm_act)
    if should_register is None or should_register("llm"):
        mcp.tool()(llm)
    if should_register is None or should_register("llm_router_status"):
        mcp.tool()(llm_router_status)
    if should_register is None or should_register("llm_router_admin"):
        mcp.tool()(llm_router_admin)
    if should_register is None or should_register("llm_router_session"):
        mcp.tool()(llm_router_session)
