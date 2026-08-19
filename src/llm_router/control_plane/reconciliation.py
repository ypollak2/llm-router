"""Cross-instance policy reconciliation (#60).

Answers, centrally and auditably: which policy version is each instance
actually enforcing, is it the active one, and is the control-plane audit chain
intact? Every reconciliation run appends a tamper-evident audit row so the
result itself is part of the auditable history.
"""
from __future__ import annotations

import time

from llm_router.control_plane import audit as cpa
from llm_router.control_plane.policy_bundle import make_payload, policy_digest
from llm_router.control_plane.store import ControlPlaneStore

# Per-instance reconciliation statuses.
STATUS_UP_TO_DATE = "up_to_date"
STATUS_BEHIND = "behind"
STATUS_LAST_KNOWN_GOOD = "last_known_good"
STATUS_STALE = "stale"

_DEFAULT_STALE_AFTER_S = 120.0


def _active_digest(tenant_id: str, active) -> str | None:
    if active is None:
        return None
    return policy_digest(make_payload(
        tenant_id=tenant_id, version=active.version,
        issued_at=active.created_at, yaml_text=active.yaml_text,
    ))


def reconcile_tenant_effective_policy(
    store: ControlPlaneStore,
    tenant_id: str,
    *,
    stale_after_s: float = _DEFAULT_STALE_AFTER_S,
    now: float | None = None,
    audit: bool = True,
) -> dict:
    now = time.time() if now is None else now
    active = store.get_current_policy(tenant_id)
    active_version = active.version if active is not None else None
    active_digest = _active_digest(tenant_id, active)

    instances = []
    for inst in store.list_instances(tenant_id):
        age = now - inst.last_seen_at
        if age > stale_after_s:
            status = STATUS_STALE
        elif inst.source == "last_known_good":
            status = STATUS_LAST_KNOWN_GOOD
        elif inst.effective_version == active_version:
            status = STATUS_UP_TO_DATE
        else:
            status = STATUS_BEHIND
        instances.append({
            "instance_id": inst.instance_id,
            "effective_version": inst.effective_version,
            "effective_digest": inst.effective_digest,
            "source": inst.source,
            "last_seen_at": inst.last_seen_at,
            "age_s": round(age, 3),
            "status": status,
        })

    counts: dict[str, int] = {}
    for i in instances:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    all_converged = bool(instances) and all(i["status"] == STATUS_UP_TO_DATE for i in instances)

    # Audit-chain integrity of the control-plane's own log.
    try:
        cpa.verify_cp_audit_chain()
        audit_chain_status = "ok"
    except cpa.TamperDetected:
        audit_chain_status = "tampered"

    summary = {
        "tenant_id": tenant_id,
        "active_version": active_version,
        "active_digest": active_digest,
        "instance_count": len(instances),
        "counts": counts,
        "all_converged": all_converged,
        "audit_chain_status": audit_chain_status,
        "instances": instances,
    }

    if audit:
        # Record the reconciliation result itself (without the per-instance
        # detail blob) as a tamper-evident row.
        cpa.audit_reconciliation(tenant_id=tenant_id, summary={
            "active_version": active_version,
            "instance_count": len(instances),
            "counts": counts,
            "all_converged": all_converged,
            "audit_chain_status": audit_chain_status,
        })

    return summary


__all__ = [
    "reconcile_tenant_effective_policy",
    "STATUS_UP_TO_DATE",
    "STATUS_BEHIND",
    "STATUS_LAST_KNOWN_GOOD",
    "STATUS_STALE",
]
