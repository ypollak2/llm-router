"""Runtime effective-policy holder — the seam between the control-plane sidecar
and the router's enforcement.

By default ``get_effective_org_policy()`` returns the local file policy
(``load_org_policy()``), so behaviour is unchanged for single-instance
deployments. When a control-plane sidecar verifies and installs a policy via
``install_effective_org_policy()``, the router reads THAT instead — atomically,
under a lock, so a concurrent read never sees a half-swapped policy.
"""
from __future__ import annotations

import threading
from typing import Any

from llm_router.policy import OrgPolicy, load_org_policy

_lock = threading.Lock()
_effective: OrgPolicy | None = None
_metadata: dict[str, Any] = {"source": "local", "version": None, "digest": None}


def get_effective_org_policy() -> OrgPolicy:
    """Return the policy the router should enforce.

    An installed (control-plane) policy takes precedence; otherwise fall back to
    the local file policy (default permissive ``OrgPolicy`` when no file).
    """
    with _lock:
        if _effective is not None:
            return _effective
    return load_org_policy() or OrgPolicy()


def install_effective_org_policy(
    policy: OrgPolicy, *, source: str, version: Any = None, digest: Any = None
) -> None:
    """Atomically install a verified policy as the effective one."""
    global _effective, _metadata
    with _lock:
        _effective = policy
        _metadata = {"source": source, "version": version, "digest": digest}


def effective_policy_metadata() -> dict[str, Any]:
    with _lock:
        return dict(_metadata)


def reset_effective_policy_for_tests() -> None:
    global _effective, _metadata
    with _lock:
        _effective = None
        _metadata = {"source": "local", "version": None, "digest": None}


__all__ = [
    "get_effective_org_policy",
    "install_effective_org_policy",
    "effective_policy_metadata",
    "reset_effective_policy_for_tests",
]
