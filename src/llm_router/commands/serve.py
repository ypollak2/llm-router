"""``llm_router serve`` — run llm_router as a long-lived HTTP service (E3 / deployment).

Until now llm_router had no stable entrypoint for a container or systemd unit: the
default ``llm_router`` runs the stdio MCP server, and the secured SSE server
(``main_sse_secured``) had no CLI surface after ``llm_router-sse`` was removed for
SEC-001. This command is that entrypoint.

Modes:
    llm_router serve                       → secured SSE MCP server (Bearer/OIDC auth)
    llm_router serve --admin               → FastAPI admin control plane

Container/VM deployments bind ``0.0.0.0`` explicitly. The SSE server additionally
requires ``LLM_ROUTER_SSE_ALLOW_PUBLIC=on`` to bind ``0.0.0.0`` — a deliberate safety
gate inherited from ``main_sse_secured`` (SEC-001): a careless deployment cannot
silently expose the routing surface without auth.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

_DEFAULT_SSE_PORT = 17891
_DEFAULT_ADMIN_PORT = 8080


@dataclass(frozen=True)
class ServeOptions:
    host: str
    port: int
    admin: bool


def parse_serve_args(args: list[str]) -> ServeOptions:
    """Parse ``llm_router serve`` flags into :class:`ServeOptions` (pure, testable)."""
    parser = argparse.ArgumentParser(
        prog="llm_router serve",
        description="Run llm_router as a long-lived HTTP service.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address (use 0.0.0.0 in containers). Default localhost.",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="bind port. Default 17891 (sse) / 8080 (admin).",
    )
    parser.add_argument(
        "--admin", action="store_true",
        help="serve the admin control plane instead of the SSE MCP server.",
    )
    ns = parser.parse_args(args)
    port = ns.port if ns.port is not None else (
        _DEFAULT_ADMIN_PORT if ns.admin else _DEFAULT_SSE_PORT
    )
    return ServeOptions(host=ns.host, port=port, admin=ns.admin)


def cmd_serve(args: list[str]) -> int:
    """Entry point for ``llm_router serve``. Blocks serving until terminated."""
    opts = parse_serve_args(args)
    if opts.admin:
        import uvicorn

        from llm_router.admin_api import create_app
        uvicorn.run(create_app(), host=opts.host, port=opts.port, log_level="info")
        return 0

    from llm_router.server import main_sse_secured
    main_sse_secured(host=opts.host, port=opts.port)
    return 0
