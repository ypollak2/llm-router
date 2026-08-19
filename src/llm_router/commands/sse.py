"""Refinement #12 / SEC-001 closure — ``llm_router sse`` CLI command.

Re-introduces the SSE network transport behind the same Bearer
auth + RBAC stack the admin API uses. The original ``llm_router-sse``
console script was removed in the SEC-001 fix because it bound
``0.0.0.0`` with no auth; this command re-enables network mode
without that hole:

* Default bind: ``127.0.0.1`` (loopback only).
* ``--host 0.0.0.0`` refuses unless
  ``LLM_ROUTER_SSE_ALLOW_PUBLIC=on`` is set (operator confirmation).
* Every request must carry ``Authorization: Bearer <token>``;
  validates against ``IdentityStore.authenticate`` and requires
  ``Permission.ROUTE_PROMPT``.
* Startup verifier (refinement #11) fires under enterprise profile
  before the bind — misconfig refuses to start.

Usage::

    llm_router sse                       # 127.0.0.1:17891
    llm_router sse --port 8080
    llm_router sse --host 0.0.0.0 --port 8080   # requires LLM_ROUTER_SSE_ALLOW_PUBLIC=on
    llm_router sse --help
"""
from __future__ import annotations

import sys


_USAGE = (
    "llm_router sse — start the SSE transport with Bearer-token auth\n"
    "\n"
    "Options:\n"
    "  --host HOST   bind interface (default: 127.0.0.1)\n"
    "  --port PORT   bind port (default: 17891)\n"
    "  --help, -h    show this message and exit\n"
    "\n"
    "Public binds:\n"
    "  --host 0.0.0.0 is refused unless LLM_ROUTER_SSE_ALLOW_PUBLIC=on\n"
    "  is set explicitly. This is the SEC-001 closure — the removed\n"
    "  llm_router-sse entry point bound 0.0.0.0 with no auth.\n"
    "\n"
    "Auth:\n"
    "  Every request needs Authorization: Bearer <token>. Issue\n"
    "  tokens via POST /v1/admin/users/{user_id}/tokens; the bearer\n"
    "  identity must carry Permission.ROUTE_PROMPT.\n"
)


def cmd_sse(args: list[str]) -> int:
    """Execute: ``llm_router sse [--host HOST] [--port PORT]``."""
    host = "127.0.0.1"
    port = 17891

    i = 0
    while i < len(args):
        flag = args[i]
        if flag in ("--help", "-h"):
            print(_USAGE)
            return 0
        if flag == "--host":
            if i + 1 >= len(args):
                print("--host requires a value", file=sys.stderr)
                return 2
            host = args[i + 1]
            i += 2
            continue
        if flag == "--port":
            if i + 1 >= len(args):
                print("--port requires a value", file=sys.stderr)
                return 2
            try:
                port = int(args[i + 1])
            except ValueError:
                print(
                    f"Invalid port: {args[i + 1]!r}", file=sys.stderr
                )
                return 2
            i += 2
            continue
        print(f"Unknown flag: {flag!r}", file=sys.stderr)
        return 2

    print(
        f"llm_router sse starting on http://{host}:{port} "
        "(Bearer auth required, ROUTE_PROMPT permission gated)"
    )
    from llm_router.server import main_sse_secured

    main_sse_secured(host=host, port=port)
    return 0
