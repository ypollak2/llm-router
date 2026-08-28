"""MCPServer server — MCP entry point for llm_router.

All 60 tools are registered by modules in llm_router/tools/:
- routing.py  — llm_classify, llm_track_usage, llm_route, llm_auto, llm_stream,
                llm_select_agent, llm_reroute
- text.py     — llm_query, llm_research, llm_generate, llm_analyze, llm_reason, llm_code, llm_edit
- media.py    — llm_image, llm_video, llm_audio
- pipeline.py — llm_orchestrate, llm_pipeline_templates
- admin.py    — llm_save_session, llm_set_profile, llm_usage, llm_cache_stats,
                llm_cache_clear, llm_quality_report, llm_health, llm_providers,
                llm_team_report, llm_team_push, llm_session_spend, llm_approve_route
- subscription.py — llm_check_usage, llm_update_usage, llm_refresh_claude_usage
- codex.py    — llm_codex
- gemini_cli.py — llm_gemini
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

from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from llm_router.config import get_config
from llm_router.health import get_tracker
from llm_router.logging import configure_logging, get_logger
from llm_router.state import _check_tier, get_active_profile  # noqa: F401  (backward compat)
from llm_router.tools import admin, agentic, agents, codex, consolidated, dashboard, fs, gemini_cli, media, pipeline, routing, setup, subscription, text

# agoragentic is an OPTIONAL tool group, and the import says so.
#
# It is off by default (SEC-003: `register()` no-ops without LLM_ROUTER_AGORAGENTIC=on)
# and it is excluded from redistributions that do not want marketplace/wallet
# tools. A hard module-level import contradicted both: it made a default-off,
# excludable tool group a load-bearing dependency of the MCP server, so a
# distribution that dropped it got a server that could not import at all —
# failing at startup over a feature nobody had enabled.
#
# Found by the downstream sync: the availability closure marked server.py
# unreachable for exactly this reason, which meant the sync could not carry the
# server module at all. The gate was already right; the import was not.
try:
    from llm_router.tools import agoragentic
except ImportError:  # pragma: no cover - only in builds that exclude it
    agoragentic = None
from llm_router.tools.admin import llm_health, llm_set_profile, llm_usage  # noqa: F401
from llm_router.tools.pipeline import llm_orchestrate  # noqa: F401
from llm_router.tools.routing import llm_route  # noqa: F401
from llm_router.tools.setup import _mask_key, llm_setup  # noqa: F401

configure_logging()
log = get_logger("llm_router.server")


@asynccontextmanager
async def _lifespan(_server):
    """RED1-8-05: drain tracked fire-and-forget tasks on server shutdown.

    _spawn_bg() records receipt/OKF/analytics writes in _BG_TASKS so they can be
    flushed rather than abandoned (leaking an aiosqlite connection) when the loop
    tears down. This runs on the SAME event loop as those tasks, so it can await
    them — an atexit handler cannot (the tasks belong to the already-closed loop).
    """
    try:
        yield
    finally:
        try:
            from llm_router.router import drain_bg_tasks
            await drain_bg_tasks(timeout_s=5.0)
        except Exception:
            pass  # best-effort flush; never block shutdown


mcp = MCPServer("llm_router", lifespan=_lifespan)

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

# Warm the local ensemble classifier so the first routed prompt pays warm latency
# (~2.5s) rather than an Ollama cold start (~56s). Fire-and-forget daemon thread;
# no-op when the ensemble is disabled or the primary is not local.
try:
    from llm_router.ensemble import warm_primary as _warm_primary
    _warm_primary()
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

# ── CHZ-SURF-01: startup self-check on the emittable tool surface ─────────────
# Every name a routing hook can put in a directive must resolve to a tool that is
# actually registered under the active tier. When it does not, the caller gets
# "No such tool available", silently falls back to the expensive model, and the
# savings dashboard shows exactly what it shows for "chose not to route" — the
# failure is invisible in every metric we have. So assert it out loud at boot.
try:
    from llm_router.tool_surface import unregistered as _unregistered_tools  # noqa: E402

    _bad_surface = _unregistered_tools(slim=_slim)
    if _bad_surface:
        log.error(
            "tool_surface_unroutable",
            slim_mode=_slim,
            tools=_bad_surface,
            detail=(
                "These tool names can be emitted by a routing hook but are NOT "
                "registered under this tier. Hints naming them fail with 'No such "
                "tool available' and silently cost full model price. Fix the door "
                "map or fallback chain in llm_router/tool_surface.py."
            ),
        )
except Exception as _surface_err:  # noqa: BLE001 — never block startup on the check
    log.warning("tool_surface_check_failed", error=str(_surface_err))

# ── Register all tool groups ──────────────────────────────────────────────────

routing.register(mcp, _gate)
text.register(mcp, _gate)
media.register(mcp, _gate)
pipeline.register(mcp, _gate)
admin.register(mcp, _gate)
subscription.register(mcp, _gate)
codex.register(mcp, _gate)
gemini_cli.register(mcp, _gate)
setup.register(mcp, _gate)
dashboard.register(mcp, _gate)
fs.register(mcp, _gate)
if agoragentic is not None:
    agoragentic.register(mcp)  # SEC-003: no-ops unless LLM_ROUTER_AGORAGENTIC=on
agents.register(mcp, _gate)  # v0.0.2 — agent-session tools (gated; consolidated keeps the rich two)
agentic.register(mcp, _gate)  # agentic router — llm_delegate (gated; consolidated hides it behind llm_act)
consolidated.register(mcp, _gate)  # North Star P4 — 1.0 front-door aliases (llm_act; non-breaking)

# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("llm_router://status")
def router_status() -> str:
    """MCP resource returning a plain-text snapshot of router state.

    SEC-004 closure (audit, lateral finding): under
    ``LLM_ROUTER_PROFILE=enterprise`` this resource gates by identity.
    Without a valid ``LLM_ROUTER_TOKEN`` (or, when called via SSE, a
    Bearer header) we return a minimal redacted shape that confirms
    the server is up but leaks NO provider configuration — the
    same posture the original audit's SEC-004 row asked for.

    Developer profile preserves the full surface so dev workstations
    and `llm_router doctor` keep working out-of-the-box.
    """
    return _render_router_status()


def _render_router_status(
    *, force_redacted: bool | None = None,
) -> str:
    """Implementation of ``llm_router://status``. Split out so tests
    don't need to spin up an MCP transport.

    ``force_redacted`` is a test affordance — when ``True`` the
    redacted shape is rendered regardless of identity; when
    ``False`` the full shape; when ``None`` the gate is applied per
    the SEC-004 contract (enterprise + no valid identity →
    redacted; everything else → full).

    Crucially the gate check happens BEFORE we touch ``get_config``
    so a Pydantic-rejected ``LLM_ROUTER_PROFILE`` value (llm_router's
    Config schema predates the enterprise profile axis introduced
    in slice 3) can't crash the redacted path."""
    if force_redacted is None:
        from llm_router.profile import is_enterprise
        from llm_router.identity import (
            EnterpriseIdentityRequired,
            _enterprise_identity,
        )
        if is_enterprise():
            try:
                _enterprise_identity()
                redact = False
            except EnterpriseIdentityRequired:
                redact = True
        else:
            redact = False
    else:
        redact = force_redacted

    if redact:
        # Minimal shape — confirms the server is up, leaks
        # nothing about configured providers / models / tiers.
        # We deliberately don't call ``get_config()`` on this path
        # so a non-Pydantic-aware enterprise profile value can't
        # crash the redacted shape.
        return "\n".join([
            "Profile: enterprise",
            "Status: ok",
            "Note: provider details redacted (SEC-004); "
            "authenticate with LLM_ROUTER_TOKEN for full status.",
        ])

    try:
        config = get_config()
    except Exception:
        # Routing config failed to validate. Most likely cause:
        # ``LLM_ROUTER_PROFILE`` is being used for the deployment-profile
        # axis (slice 3) but llm_router's routing ``Config`` expects one
        # of ``budget/balanced/premium/quota_balanced/subscription_local``
        # for the same env. Surface a useful message rather than
        # crashing the resource handler.
        return "\n".join([
            "Profile: enterprise",
            "Status: ok",
            "Note: routing config unavailable — the deployment-profile "
            "env may collide with the routing config's LLM_ROUTER_PROFILE "
            "expectations. Restart with a clean routing profile to see "
            "provider details.",
        ])
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


_STARTUP_VERIFY_SKIP_ENV = "LLM_ROUTER_SKIP_STARTUP_VERIFY"
_STARTUP_VERIFY_OFF_VALUES = {"on", "1", "true", "yes"}

# Loop-5 follow-up — critical-module list checked at BOTH developer
# and enterprise boot. Fires before _startup_verify_or_die so a
# stale-installed runtime (the G-034 / OP-1 failure mode that broke
# the MCP repeatedly during the audit + Loop-5 work) is caught at
# boot rather than mid-call. Keep this list minimal: only modules
# whose absence guarantees the server is broken. Adding low-value
# modules here turns startup into a slow probe.
_CRITICAL_MODULES: tuple[str, ...] = (
    "llm_router.cli",
    "llm_router.classification_allowlist",  # the canonical G-034 canary
    # removed by sync: llm_router.admin_api is not shipped downstream
    # removed by sync: llm_router.invoice_reconciliation is not shipped downstream
    "llm_router.agents.session",
)

# Enterprise-only critical modules. ``llm_router.enterprise/`` is intentionally
# EXCLUDED from the public wheel/sdist, so requiring it universally made the
# published MCP server refuse to boot ("No module named 'llm_router.enterprise'").
# These are only critical under the enterprise profile, where the package IS
# present; checked conditionally below.
_ENTERPRISE_CRITICAL_MODULES: tuple[str, ...] = (
    # removed by sync: llm_router.enterprise.identity is not shipped downstream
    # removed by sync: llm_router.enterprise.rbac is not shipped downstream
    # removed by sync: llm_router.enterprise.quotas is not shipped downstream
)

_CRITICAL_MODULE_SKIP_ENV = "LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK"


def _critical_modules_or_die() -> None:
    """Loop-5 follow-up — verify every critical module is importable
    at server startup. Refuses to boot when any fails.

    Why this is separate from ``_startup_verify_or_die``:

    * ``_startup_verify_or_die`` only fires under enterprise profile;
      this check fires in developer profile too. Developer installs
      drift just as easily as enterprise ones — Loop-5 itself was
      blocked twice by ``No module named 'llm_router.classification_allowlist'``.
    * ``verify_enterprise`` covers RBAC / audit / redaction / DB
      invariants that only matter at the enterprise tier. The
      missing-module surface is universal.

    The G-034 install-smoke gate (``scripts/ci_install_smoke_test.sh``)
    prevents a broken sdist from publishing to PyPI in the first
    place. This boot-time check is the second layer — it catches
    a runtime that drifted AFTER install (a `pip install -U` against
    an old wheel, an editable install that lost a `.pth` entry, a
    user who skipped the smoke gate locally).

    Bypass via ``LLM_ROUTER_SKIP_CRITICAL_MODULE_CHECK=on`` for emergency
    debug; the bypass logs a warning to stderr so it can never be
    silent.
    """
    import importlib
    import os
    import sys

    skip = (
        (os.environ.get(_CRITICAL_MODULE_SKIP_ENV) or "")
        .strip().lower() in _STARTUP_VERIFY_OFF_VALUES
    )
    if skip:
        sys.stderr.write(
            f"[llm_router server] {_CRITICAL_MODULE_SKIP_ENV}=on — "
            "skipping critical-module check. The server may boot "
            "with a broken module surface; routed calls will fail "
            "with inscrutable transport errors.\n"
        )
        return

    # Enterprise modules are only critical under the enterprise profile — the
    # public distribution ships without them on purpose.
    modules = _CRITICAL_MODULES
    try:
        from llm_router.profile import is_enterprise
        if is_enterprise():
            modules = _CRITICAL_MODULES + _ENTERPRISE_CRITICAL_MODULES
    except Exception:  # noqa: BLE001 — profile resolution must never block boot
        pass

    failures: list[tuple[str, str]] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            # Any import-time failure counts — ImportError on missing
            # files, SyntaxError on corrupt files, ModuleNotFoundError
            # on a partial install. We don't try to classify because
            # the remediation is the same for all of them.
            failures.append((name, f"{type(exc).__name__}: {exc}"))

    if not failures:
        return

    sys.stderr.write(
        "[llm_router server] critical-module check FAILED — installed "
        "runtime is missing modules that exist in source. This is "
        "the G-034 / OP-1 stale-install failure mode.\n"
    )
    for name, detail in failures:
        sys.stderr.write(f"  ✗ {name}\n    → {detail}\n")
    sys.stderr.write(
        "\nRemediation:\n"
        "  1. uv tool install --reinstall --editable ~/projects/llm_router\n"
        "     (or `pip install -e .` from the source checkout)\n"
        "  2. Restart the MCP server process — in-memory module table "
        "is cached from the previous install.\n"
        "  3. Verify with `llm_router doctor` (the OP-4 transport probe).\n"
        f"  4. Bypass for emergency debug only: "
        f"{_CRITICAL_MODULE_SKIP_ENV}=on (NOT for production).\n"
    )
    sys.exit(1)


def _startup_verify_or_die() -> None:
    """Refinement #11 — run the enterprise verifier at boot and
    refuse to start if any check fails.

    Only fires under ``LLM_ROUTER_PROFILE=enterprise`` so existing
    developer / single-user installs see zero behaviour change.
    Operators can bypass for emergency debug via
    ``LLM_ROUTER_SKIP_STARTUP_VERIFY=on`` — the bypass logs a warning so
    it can never be silent.

    The verifier itself is a sub-100ms pure check list; failing fast
    on a misconfigured enterprise deployment is the whole point.
    Anything that needs the MCP transport (auto-route hook, llm_*
    tools) is dead in the water until the operator fixes the
    config, and silent boot would mean every routed call producing
    inscrutable errors at the transport layer (the OP-1 / OP-4
    failure mode this session showed in spades).
    """
    import os
    import sys

    from llm_router.profile import is_enterprise

    if not is_enterprise():
        return

    skip = (
        (os.environ.get(_STARTUP_VERIFY_SKIP_ENV) or "")
        .strip().lower() in _STARTUP_VERIFY_OFF_VALUES
    )
    if skip:
        sys.stderr.write(
            f"[llm_router server] {_STARTUP_VERIFY_SKIP_ENV}=on — "
            "skipping enterprise verifier on startup. The MCP server "
            "may boot in a degraded state; routed calls can fail "
            "with inscrutable transport errors.\n"
        )
        return

    from llm_router.commands.verify_enterprise import run_verifier

    report = run_verifier(enterprise=True)
    if report.all_passed:
        return

    sys.stderr.write(
        "[llm_router server] enterprise startup verification FAILED:\n"
    )
    for r in report.results:
        if not r.passed:
            sys.stderr.write(f"  ✗ {r.name}: {r.status}\n")
            if r.remediation:
                sys.stderr.write(f"    → {r.remediation}\n")
    sys.stderr.write(
        "Set LLM_ROUTER_SKIP_STARTUP_VERIFY=on to bypass (NOT for production).\n"
    )
    sys.exit(1)


def main():
    """Start the MCP server (stdio transport by default)."""
    # Critical-module check runs FIRST — the enterprise verifier
    # below imports modules that may themselves be missing in a
    # stale install, which would surface as a confusing
    # ``ModuleNotFoundError`` inside the verifier rather than a
    # clean remediation message.
    _critical_modules_or_die()
    _startup_verify_or_die()
    # Auto-detect local LLM platforms and print a summary on first run.
    # Best-effort — never blocks or crashes startup on failure.
    try:
        from llm_router.local_platforms import print_startup_summary
        print_startup_summary()
    except Exception:
        pass
    mcp.run()


def main_sse(port: int | None = None) -> None:
    """Start the MCP server with SSE transport for remote/hosted access.

    ⚠️  SECURITY NOTICE (SEC-001, audit 2026-06):
    This function is INTENTIONALLY not exposed as a console script.
    The prior `llm_router-sse` entry point bound 0.0.0.0 with no auth and
    exposed the full 60-tool MCP surface — including filesystem tools
    and wallet — to anyone reachable on the network. It was removed in
    the same release that introduced this notice.

    Do NOT re-add `llm_router-sse` to `[project.scripts]` in pyproject.toml
    until ALL of the following are true:
      1. Bearer-token (or stronger) auth middleware wraps `mcp.sse_app()`
      2. INV-010 has landed (identity → routing → audit chain)
      3. Default host is `127.0.0.1`; `0.0.0.0` requires explicit env opt-in

    See Docs/audit/HIGH_PRIORITY_WORK_PLAN.md F-SEC-001.

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
    # SEC-1: default to localhost and refuse a public bind unless the shared
    # gate says so. This function previously defaulted to 0.0.0.0 and never
    # consulted _allow_public_bind() — the gate defined a few lines below it —
    # so the (wrongly re-added) console script published the whole MCP tool
    # surface, unauthenticated, to every interface.
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not _allow_public_bind():
        raise SystemExit(
            f"refusing to bind {host}: main_sse has no authentication.\n"
            f"Set {_SSE_ALLOW_PUBLIC_ENV}=1 to override, or use main_sse_secured(), "
            f"which requires a bearer token and defaults to localhost."
        )

    starlette_app = mcp.sse_app()
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)


_SSE_ALLOW_PUBLIC_ENV = "LLM_ROUTER_SSE_ALLOW_PUBLIC"
_SSE_ALLOW_PUBLIC_VALUES = {"on", "1", "true", "yes"}


def _allow_public_bind() -> bool:
    """Delegates to the shared gate (RED6-04).

    This module had the ONLY correct implementation while gateway.py,
    route_server.py and commands/admin_api.py each shipped without one. Keeping a
    private copy here would preserve exactly the arrangement that produced three
    misses out of four. LLM_ROUTER_SSE_ALLOW_PUBLIC is still honoured by the shared
    gate, so an existing opt-in keeps working.
    """
    from llm_router.net_bind import allow_public_bind as _shared
    return _shared()


def main_sse_secured(
    *, host: str = "127.0.0.1", port: int = 17891,
) -> None:
    """Refinement #12 / SEC-001 closure.

    Start the SSE transport behind a Bearer-token auth middleware
    that delegates to ``IdentityStore.authenticate`` + requires
    ``Permission.ROUTE_PROMPT``. Closes the literal first audit
    finding (SEC-001) that removed the original ``llm_router-sse``
    entry point because it bound 0.0.0.0 with no auth.

    Three concrete defences vs the pre-removal entry point:

    * **Auth is mandatory.** Every request must carry
      ``Authorization: Bearer <token>``; the token validates against
      the identity store and must carry ``Permission.ROUTE_PROMPT``.
      Tools without auth see ``401 Unauthorized``.
    * **Default bind is localhost.** ``host`` defaults to
      ``127.0.0.1``; ``0.0.0.0`` requires explicit
      ``LLM_ROUTER_SSE_ALLOW_PUBLIC=on`` so a careless deployment can't
      silently expose the surface.
    * **Startup verifier fires under enterprise profile.** Misconfig
      is refused before binding (same contract as ``main()``).

    The original ``main_sse`` is retained above with its SEC-001
    notice so a reader auditing for the regression can still see
    the unsecured shape — but ``main_sse_secured`` is what the CLI
    actually exposes.
    """
    import os
    import sys

    import anyio
    import uvicorn
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse

    if host == "0.0.0.0" and not _allow_public_bind():
        sys.stderr.write(
            "[llm_router sse] refusing to bind 0.0.0.0 without "
            f"{_SSE_ALLOW_PUBLIC_ENV}=on. Set the env explicitly or "
            "pass --host 127.0.0.1 (the default) for localhost only.\n"
        )
        sys.exit(2)

    _critical_modules_or_die()
    _startup_verify_or_die()

    import types

    from llm_router.enterprise.identity import (
        IdentityStore,
        InvalidToken,
    )
    from llm_router.enterprise.oidc import OidcConfig, OidcError, OidcValidator
    from llm_router.enterprise.rbac import Permission, permissions_for_role

    # Store opens once at process start; the SSE handler dispatches
    # under uvicorn workers so we need cross-thread safety.
    identity_store = IdentityStore(check_same_thread=False)

    # Optional OIDC federation: validate IdP JWTs (non-'tsr_' tokens) and
    # just-in-time provision the user. None when LLM_ROUTER_OIDC_ISSUER is unset.
    oidc_config = OidcConfig.from_env()
    oidc_validator = OidcValidator(oidc_config) if oidc_config else None
    oidc_default_org = (os.environ.get("LLM_ROUTER_OIDC_DEFAULT_ORG") or "default").strip() or "default"
    oidc_default_team = (os.environ.get("LLM_ROUTER_OIDC_DEFAULT_TEAM") or "default").strip() or "default"

    async def _authenticate_oidc(token: str):
        """Validate an IdP JWT → JIT-provisioned identity, or None on reject.

        🥷 Backslash-security: Enforce auth/authz to prevent unauthorized access.
        """
        try:
            claims = await oidc_validator.validate(token)
        except OidcError:
            return None
        role = oidc_validator.map_role(claims.groups)
        org = identity_store.get_or_create_org(oidc_default_org)
        team = identity_store.get_or_create_team(org.id, oidc_default_team)
        user = identity_store.get_or_create_by_external_id(
            external_id=claims.subject, email=claims.email,
            display_name=claims.email, role=role,
            org_id=org.id, team_id=team.id,
        )
        if not user.active:
            return None
        perms = frozenset(permissions_for_role(user.role))
        # Lightweight principal carrying just what downstream RBAC reads.
        return types.SimpleNamespace(user=user, token=None, permissions=perms)

    class _BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            authz = request.headers.get("Authorization", "")
            parts = authz.strip().split(None, 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return PlainTextResponse(
                    "Unauthorized — Bearer token required",
                    status_code=401,
                )
            token = parts[1].strip()
            if not token:
                return PlainTextResponse(
                    "Unauthorized — empty bearer token",
                    status_code=401,
                )
            # Federated (OIDC JWT) tokens lack the llm_router 'tsr_' prefix.
            if not token.startswith("tsr_") and oidc_validator is not None:
                identity = await _authenticate_oidc(token)
                if identity is None:
                    return PlainTextResponse(
                        "Unauthorized — OIDC token rejected", status_code=401,
                    )
            else:
                try:
                    identity = identity_store.authenticate(token)
                except InvalidToken as exc:
                    return PlainTextResponse(
                        f"Unauthorized — {exc}", status_code=401,
                    )
            if Permission.ROUTE_PROMPT not in identity.permissions:
                return PlainTextResponse(
                    "Forbidden — identity lacks ROUTE_PROMPT",
                    status_code=403,
                )
            # Pass through with identity attached so downstream
            # tools can attribute the routed turn (future
            # extension; the middleware contract is set).
            request.state.identity = identity
            return await call_next(request)

    starlette_app = mcp.sse_app()
    starlette_app.user_middleware.insert(
        0, Middleware(_BearerAuthMiddleware)
    )
    starlette_app.middleware_stack = starlette_app.build_middleware_stack()

    config = uvicorn.Config(
        starlette_app, host=host, port=port, log_level="info",
    )
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if __name__ == "__main__":
    main()
