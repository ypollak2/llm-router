"""The control plane's OWN tamper-evident audit log (#42).

Records control-plane mutations — policy version creation/activation, bundle
serves, instance heartbeats, signature failures, reconciliation runs — in a
SEPARATE hash-chained SQLite DB (``LLM_ROUTER_CP_AUDIT_PATH``), distinct from any
llm_router instance's local runtime audit. Reuses the tamper-evident
``llm_router.enterprise.audit`` machinery. Writes are best-effort (never break a
control-plane action); ``verify_cp_audit_chain`` surfaces tampering.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import llm_router.logging
from llm_router.enterprise.audit import AuditEvent, AuditLog, TamperDetected

ACTION_POLICY_VERSION_CREATED = "cp.policy.version_created"
ACTION_POLICY_ACTIVATED = "cp.policy.activated"
ACTION_BUNDLE_SERVED = "cp.policy.bundle_served"
ACTION_HEARTBEAT = "cp.instance.heartbeat"
ACTION_POLICY_EFFECTIVE = "cp.instance.policy_effective"
ACTION_SIGNATURE_FAILED = "cp.policy.signature_failed"
ACTION_RECONCILIATION = "cp.audit.reconciliation"
ACTION_BUDGET_LINEAGE_RECON = "cp.budget.lineage_reconciliation"

_CP_AUDIT_TYPE = "control_plane.event"

_log: AuditLog | None = None
_log_lock = threading.Lock()


def _cp_audit_path() -> Path:
    return Path(os.environ.get("LLM_ROUTER_CP_AUDIT_PATH", Path.home() / ".llm-router" / "cp_audit.db"))


def get_cp_audit_log() -> AuditLog:
    global _log
    with _log_lock:
        if _log is None:
            # check_same_thread=False: a threaded FastAPI server serves requests
            # (and thus writes CP audit rows) from a worker-thread pool, so the
            # shared audit connection must not be pinned to its creating thread.
            _log = AuditLog(db_path=_cp_audit_path(), check_same_thread=False)
        return _log


def reset_cp_audit_log_for_tests() -> None:
    global _log
    with _log_lock:
        _log = None


def append_cp_event(
    *,
    action: str,
    resource: str,
    detail: dict,
    actor_id: str = "system",
    actor_email: str = "",
    org_id: str = "",
    severity: str = "info",
) -> None:
    try:
        event = AuditEvent(
            type=_CP_AUDIT_TYPE,
            actor_id=actor_id,
            actor_email=actor_email,
            org_id=org_id,
            resource=resource,
            action=action,
            detail=detail,
            severity=severity,
        )
        get_cp_audit_log().append(event)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the caller
        llm_router.logging.get_logger("llm_router.control_plane.audit").warning(
            "cp_audit_write_failed", error=str(exc)
        )


def verify_cp_audit_chain() -> None:
    get_cp_audit_log().verify_chain()


def audit_policy_created(*, tenant_id, version, digest, actor_id: str = "system") -> None:
    append_cp_event(
        action=ACTION_POLICY_VERSION_CREATED,
        resource=f"tenant:{tenant_id}",
        detail={"version": version, "digest": digest},
        actor_id=actor_id,
    )


def audit_policy_activated(*, tenant_id, version, digest, actor_id: str = "system") -> None:
    append_cp_event(
        action=ACTION_POLICY_ACTIVATED,
        resource=f"tenant:{tenant_id}",
        detail={"version": version, "digest": digest},
        actor_id=actor_id,
    )


def audit_bundle_served(*, tenant_id, version, digest) -> None:
    append_cp_event(
        action=ACTION_BUNDLE_SERVED,
        resource=f"tenant:{tenant_id}",
        detail={"version": version, "digest": digest},
    )


def audit_heartbeat(*, tenant_id, instance_id, effective_version, effective_digest, source) -> None:
    append_cp_event(
        action=ACTION_HEARTBEAT,
        resource=f"instance:{instance_id}",
        detail={
            "tenant_id": tenant_id,
            "instance_id": instance_id,
            "effective_version": effective_version,
            "effective_digest": effective_digest,
            "source": source,
        },
    )


def audit_signature_failed(*, tenant_id, instance_id, detail=None) -> None:
    append_cp_event(
        action=ACTION_SIGNATURE_FAILED,
        resource=f"instance:{instance_id}",
        detail=detail or {"tenant_id": tenant_id},
        severity="critical",
    )


def audit_reconciliation(*, tenant_id, summary: dict) -> None:
    append_cp_event(
        action=ACTION_RECONCILIATION,
        resource=f"tenant:{tenant_id}",
        detail=summary,
    )


def audit_budget_lineage_reconciliation(*, scope: str, summary: dict) -> None:
    """Record a budget-lineage reconciliation result (#70) as a tamper-evident
    control-plane audit row, so the reconciliation itself is attestable and its
    integrity verifiable via ``verify_cp_audit_chain`` — the same treatment
    policy reconciliation (#60) gets."""
    append_cp_event(
        action=ACTION_BUDGET_LINEAGE_RECON,
        resource=f"budget:{scope}",
        detail=summary,
    )


__all__ = [
    "ACTION_POLICY_VERSION_CREATED",
    "ACTION_POLICY_ACTIVATED",
    "ACTION_BUNDLE_SERVED",
    "ACTION_HEARTBEAT",
    "ACTION_POLICY_EFFECTIVE",
    "ACTION_SIGNATURE_FAILED",
    "ACTION_RECONCILIATION",
    "ACTION_BUDGET_LINEAGE_RECON",
    "TamperDetected",
    "get_cp_audit_log",
    "reset_cp_audit_log_for_tests",
    "append_cp_event",
    "verify_cp_audit_chain",
    "audit_policy_created",
    "audit_policy_activated",
    "audit_bundle_served",
    "audit_heartbeat",
    "audit_signature_failed",
    "audit_reconciliation",
    "audit_budget_lineage_reconciliation",
]
