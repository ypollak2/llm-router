"""Tier-1 audit-on-every-turn helper.

Bridges the routing path to the existing
``llm_router.enterprise.audit.AuditLog`` (which was shipped as enterprise
scaffolding in earlier releases but had zero call sites from
``router.route_and_call`` until Tier 1). One row per successful routed
turn, attributed to ``TurnIdentity`` resolved from the operator's env.

Design choices:

* **Lazy module-level singleton.** ``AuditLog()`` opens a SQLite
  connection in its constructor; opening on every call would dominate
  the routing-path latency. The first call to
  :func:`audit_routing_turn` creates the log and caches it for the
  lifetime of the process.
* **Best-effort, fail-open.** A failure here must never break the
  routed turn — the user is owed an answer even if
  ``~/.llm-router/audit.db`` is unwritable. Exceptions are logged and
  swallowed. Tier 3 will introduce a fail-closed mode behind a config
  flag for regulated deployments.
* **Disable via env.** ``LLM_ROUTER_AUDIT_DISABLED=1`` (or any affirmative
  value) skips the audit append entirely. Useful for tests that don't
  exercise auditing and for users who explicitly opt out of local
  audit-DB writes.

The shape of the event is canonical (``AuditEventType.ROUTING_DECISION``)
so the existing CEF/JSON/CSV exporters Just Work.
"""
from __future__ import annotations

import os
import threading
from typing import Any

try:
    from llm_router.enterprise.audit import AuditEvent, AuditEventType, AuditLog
except ImportError:  # enterprise/ is excluded from public distributions (gated by is_enterprise())
    AuditEvent = AuditEventType = AuditLog = None  # type: ignore
from llm_router.identity import TurnIdentity, current_identity
from llm_router.logging import get_logger
from llm_router.profile import is_enterprise

log = get_logger("llm_router.audit_routing")


# Tests / opt-out env. Affirmative values match the convention used by
# ``LLM_ROUTER_FS_TOOLS`` (SEC-002) and ``LLM_ROUTER_AGORAGENTIC`` (SEC-003).
_AUDIT_DISABLED_ENV = "LLM_ROUTER_AUDIT_DISABLED"
_AFFIRMATIVE = {"1", "on", "true", "yes"}


# Module-level singleton + a lock to make first-call construction
# thread-safe. ``threading.Lock`` is the right tool here even though
# routing is asyncio-driven: the singleton is shared across whatever
# concurrent tasks happen to call ``audit_routing_turn`` first.
_audit_log: AuditLog | None = None
_audit_log_lock = threading.Lock()


def _audit_disabled() -> bool:
    # 🥷 Backslash-security: Log all security-relevant events.
    # G-003: under the enterprise profile the audit trail is mandatory —
    # ``LLM_ROUTER_AUDIT_DISABLED`` is refused regardless of its value so an
    # env tweak can't silently turn off the tamper-evident log. Developer
    # profile preserves the env-driven opt-out for local/test use.
    if is_enterprise():
        return False
    return (os.environ.get(_AUDIT_DISABLED_ENV) or "").strip().lower() in _AFFIRMATIVE


def _get_audit_log() -> AuditLog:
    """Return the process-wide :class:`AuditLog`, constructing on first call."""
    global _audit_log
    if _audit_log is None:
        with _audit_log_lock:
            if _audit_log is None:  # re-check under lock
                _audit_log = AuditLog()
    return _audit_log


def reset_audit_log_for_tests() -> None:
    """Clear the module-level :class:`AuditLog` singleton.

    Tests use this in their fixtures to force the next call to construct
    a fresh log pointed at the test's ``tmp_path`` ``LLM_ROUTER_AUDIT_PATH``.
    Production code never calls this.
    """
    global _audit_log
    with _audit_log_lock:
        _audit_log = None


def audit_routing_turn(
    *,
    identity: TurnIdentity | None,
    task_type: str,
    complexity: str | None,
    model: str,
    provider: str,
    cost_usd: float,
    cached: bool = False,
    detail_extras: dict[str, Any] | None = None,
) -> None:
    """Append one ``routing.decision`` audit row.

    Called from ``router.route_and_call`` just before every successful
    return path (cached hit + cold-fetched). Identity is resolved from
    env via :func:`llm_router.identity.current_identity` when ``None``.

    Failure modes are swallowed and logged at WARNING — callers must not
    catch exceptions from this function because there should not be
    any. See module docstring for the rationale.
    """
    if _audit_disabled():
        return

    try:
        actor = identity if identity is not None else current_identity()

        detail = {
            "task_type": task_type,
            "complexity": complexity or "unknown",
            "model": model,
            "provider": provider,
            "cost_usd": float(cost_usd or 0.0),
        }
        # Tier 2: surface agent_id in the audit row when this turn is
        # part of an agent run. Omitted when None so non-agent turns
        # don't carry a meaningless null field.
        if actor.agent_id:
            detail["agent_id"] = actor.agent_id
        # T1-M1 (Q-P-2 Phase 3a): always surface tenant_id when present.
        # In Phase 3a it usually equals org_id (single-org-per-instance);
        # in Phase 3b it differentiates per-tenant sidecars within one
        # org. Carrying it from day 1 makes audit-row schemas
        # forward-compat without a future migration.
        if actor.tenant_id:
            detail["tenant_id"] = actor.tenant_id
        if detail_extras:
            detail.update(detail_extras)

        event = AuditEvent(
            type=AuditEventType.ROUTING_DECISION,
            actor_id=actor.user_id,
            actor_email=actor.user_email,
            org_id=actor.org_id,
            resource=f"model:{model}",
            action="cached" if cached else "routed",
            detail=detail,
            severity="info",
        )
        _get_audit_log().append(event)
    except Exception as audit_err:  # noqa: BLE001 — see module docstring
        # Best-effort. Never propagate; the routed turn already happened.
        log.warning("audit_write_failed", error=str(audit_err))


__all__ = [
    "audit_routing_turn",
    "reset_audit_log_for_tests",
]
