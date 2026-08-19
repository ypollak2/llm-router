"""Public-bind refusal — one gate, used by every component that serves.

RED6-04. `server.py`'s SSE entry point refused to bind a public interface without
an explicit env opt-in. Three other components that serve real, paid model calls
did not:

    gateway.py            FastAPI; a whole-file grep for ``Depends(`` returns
                          ZERO — no bearer token, no API key, no per-route auth.
                          Its only protection is a browser CSRF/DNS-rebinding
                          Host check whose own docstring states that non-browser
                          clients are unaffected, so by design it admits exactly
                          the traffic shape any curl, SDK, or hostile local
                          process produces.
    route_server.py       console script ``llm_router-route``; zero auth checks.
    commands/admin_api.py documents ``--host 0.0.0.0`` as a feature.

The audit's conclusion, and the reason this module exists rather than three more
copies of the check: *"the gate should be a shared utility, not something each
new server component has to remember to reimplement."* Three components forgot
and one remembered, which is the expected outcome of a convention. A shared
function turns "remember to add the gate" into "call the bind helper", and the
test suite asserts every serving module calls it — so the NEXT server component
fails a test rather than a production network.

Deliberately NOT authentication. This refuses an unattended public bind; it does
nothing about an attacker already on the loopback interface. Conflating the two
would be the sort of overclaim this audit exists to remove.
"""

from __future__ import annotations

import os
import sys

__all__ = [
    "ALLOW_PUBLIC_ENV",
    "LEGACY_SSE_ALLOW_PUBLIC_ENV",
    "PUBLIC_HOSTS",
    "allow_public_bind",
    "is_public_host",
    "refuse_public_bind_or_exit",
]

#: Explicit opt-in for binding a publicly-reachable interface.
ALLOW_PUBLIC_ENV = "LLM_ROUTER_ALLOW_PUBLIC_BIND"

#: server.py shipped this first. Still honoured: consolidating a gate must not
#: silently revoke an opt-in somebody is already relying on.
LEGACY_SSE_ALLOW_PUBLIC_ENV = "LLM_ROUTER_SSE_ALLOW_PUBLIC"

_TRUTHY = {"on", "1", "true", "yes"}

#: Hosts that accept connections from outside the machine.
PUBLIC_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})


def allow_public_bind() -> bool:
    """True only when an operator has explicitly opted in.

    Unrecognised values (including typos like "yess" or "enabled") read as
    FALSE. A gate that opens on anything non-empty is not a gate, and the
    failure direction matters: a mistyped variable should leave you on
    localhost, not on every interface.
    """
    for env in (ALLOW_PUBLIC_ENV, LEGACY_SSE_ALLOW_PUBLIC_ENV):
        if (os.environ.get(env) or "").strip().lower() in _TRUTHY:
            return True
    return False


def is_public_host(host: str) -> bool:
    """True if binding ``host`` exposes the service beyond this machine."""
    return (host or "").strip().lower() in PUBLIC_HOSTS


def refuse_public_bind_or_exit(host: str, *, component: str) -> None:
    """Exit(2) if ``host`` is public and the operator has not opted in.

    ``component`` is named in the message because an operator running several
    LLM Router servers needs to know WHICH one refused; "refusing to bind" alone
    sends them reading source. The remedy is stated for the same reason — a
    refusal nobody can act on becomes an env var set blindly to make it stop,
    which is worse than no gate at all.
    """
    if not is_public_host(host) or allow_public_bind():
        return
    sys.stderr.write(
        f"[llm_router {component}] refusing to bind {host!r}: this exposes "
        f"{component} on every reachable interface, and it has no request "
        f"authentication.\n"
        f"  To bind localhost only (recommended): --host 127.0.0.1\n"
        f"  To accept the exposure explicitly:    {ALLOW_PUBLIC_ENV}=on\n"
    )
    sys.exit(2)
