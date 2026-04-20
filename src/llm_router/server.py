"""FastMCP server — MCP entry point for llm-router.

All 50 tools are registered by modules in llm_router/tools/:
- routing.py  — llm_classify, llm_track_usage, llm_route, llm_auto, llm_stream,
                llm_select_agent, llm_reroute
- text.py     — llm_query, llm_research, llm_generate, llm_analyze, llm_code, llm_edit
- media.py    — llm_image, llm_video, llm_audio
- pipeline.py — llm_orchestrate, llm_pipeline_templates
- admin.py    — llm_save_session, llm_set_profile, llm_usage, llm_cache_stats,
                llm_cache_clear, llm_quality_report, llm_health, llm_providers,
                llm_team_report, llm_team_push, llm_session_spend, llm_approve_route
- subscription.py — llm_check_usage, llm_update_usage, llm_refresh_claude_usage
- codex.py    — llm_codex
- setup.py    — llm_setup, llm_rate
- fs.py       — llm_fs_find, llm_fs_rename, llm_fs_edit_many, llm_fs_analyze_context
- agoragentic.py — agoragentic_task, agoragentic_browse, agoragentic_wallet,
                   agoragentic_status

Tool slim mode (LLM_ROUTER_SLIM=routing|core) reduces registered tools to save
context tokens — see llm_router/tool_tiers.py for tier definitions.

All tools return formatted strings (not structured data) because MCP tool
responses are displayed directly to the user in the Claude Code UI.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from llm_router.config import get_config
from llm_router.health import get_tracker
from llm_router.logging import configure_logging, get_logger
from llm_router.state import _check_tier, get_active_profile  # noqa: F401  (backward compat)
from llm_router.tools import admin, agoragentic, codex, fs, media, pipeline, routing, setup, subscription, text
from llm_router.tools.admin import llm_health, llm_set_profile, llm_usage  # noqa: F401
from llm_router.tools.pipeline import llm_orchestrate  # noqa: F401
from llm_router.tools.routing import llm_route  # noqa: F401
from llm_router.tools.setup import _mask_key, llm_setup  # noqa: F401

configure_logging()
log = get_logger("llm_router.server")

mcp = FastMCP("llm-router")

# Auto-update routing rules and hooks on startup if a newer version was installed via pip
try:
    from llm_router.install_hooks import check_and_update_hooks as _update_hooks
    from llm_router.install_hooks import check_and_update_rules as _update_rules
    _msg = _update_rules()
    if _msg:
        log.info("routing_rules_updated", update_message=_msg)
    for _hmsg in _update_hooks():
        log.info("hook_updated", update_message=_hmsg)
except Exception:
    pass

# Auto-update benchmark data on startup
try:
    from llm_router.benchmarks import check_and_update_benchmarks as _update_benchmarks
    _bmsg = _update_benchmarks()
    if _bmsg:
        log.info("benchmarks_updated", update_message=_bmsg)
except Exception:
    pass

# Reset stale circuit breakers on startup (clears failures older than 30 min)
try:
    import os as _os
    from llm_router.health import get_tracker as _get_tracker
    _reset_tracker = _get_tracker()
    _reset = _reset_tracker.reset_stale(max_age_seconds=1800.0)
    if _reset:
        log.info("circuit_breakers_reset", reset_count=_reset)
    try:
        _os.unlink(_os.path.expanduser("~/.llm-router/reset_stale.flag"))
    except OSError:
        pass
except Exception:
    pass

# ── Initialize dynamic routing tables on startup ────────────────────────────────
# Build custom routing tables based on discovered available providers.
# This happens once at session start, so all routing decisions use optimized
# chains that reflect what's actually configured.
try:
    from llm_router.dynamic_routing import initialize_dynamic_routing
    initialize_dynamic_routing()
except Exception as _dynroute_err:
    log.warning("Failed to initialize dynamic routing, will fall back to static tables: %s", _dynroute_err)

# ── Tool slim mode (v4.0) ─────────────────────────────────────────────────────
# Gating happens at registration time so unused tools never appear in Claude's
# tool list at all — saving tokens before any request is made.

from llm_router.tool_tiers import make_should_register, tier_summary as _tier_summary  # noqa: E402

_slim = get_config().llm_router_slim
_gate = make_should_register(_slim)
if _slim != "off":
    log.info("tool_slim_mode", slim_mode=_slim, summary=_tier_summary(_slim))

# ── Register all tool groups ──────────────────────────────────────────────────

routing.register(mcp, _gate)
text.register(mcp, _gate)
media.register(mcp, _gate)
pipeline.register(mcp, _gate)
admin.register(mcp, _gate)
subscription.register(mcp, _gate)
codex.register(mcp, _gate)
setup.register(mcp, _gate)
fs.register(mcp, _gate)
agoragentic.register(mcp)

# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("llm-router://status")
def router_status() -> str:
    """MCP resource returning a plain-text snapshot of the router's current state.

    Includes the active profile, subscription tier, configured provider
    counts (text and media), optional monthly budget, and per-provider
    circuit-breaker health status.

    Returns:
        A newline-delimited plain-text summary (not markdown).
    """
    config = get_config()
    tracker = get_tracker()
    report = tracker.status_report()
    lines = [
        f"Profile: {config.llm_router_profile.value}",
        f"Tier: {config.llm_router_tier.value}",
        f"Providers: {len(config.available_providers)} configured",
        f"Text: {', '.join(sorted(config.text_providers))}",
        f"Media: {', '.join(sorted(config.media_providers))}",
    ]
    if config.llm_router_monthly_budget > 0:
        lines.append(f"Budget: ${config.llm_router_monthly_budget:.2f}/mo")
    for provider, status in report.items():
        lines.append(f"  {provider}: {status}")
    return "\n".join(lines)


# ── Backward compat re-exports are at the top of this module ─────────────────


def main():
    """Start the MCP server (stdio transport by default)."""
    mcp.run()


def main_sse(port: int | None = None) -> None:
    """Start the MCP server with SSE transport for remote/hosted access.

    Reads PORT and HOST from environment so it works on Railway, Render,
    Fly.io and other PaaS platforms that inject these at runtime.

    Args:
        port: TCP port to listen on. Falls back to $PORT env var, then
              argv[1], then 17891.
    """
    import os
    import sys
    import anyio
    import uvicorn

    if port is None:
        env_port = os.environ.get("PORT")
        port = int(env_port) if env_port else (
            int(sys.argv[1]) if len(sys.argv) > 1 else 17891
        )
    host = os.environ.get("HOST", "0.0.0.0")

    starlette_app = mcp.sse_app()
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if __name__ == "__main__":
    main()
