"""Budget-lineage reconciliation — subtree spend conservation (#70, core).

Task #70 is "reconcile budget lineage against the control-plane audit log."
This module builds the **verifiable core**: the ledger-internal invariant that
guards the exact G-009 failure mode (a child charge that fails to debit its
ancestor). It deliberately does **not** yet cross-check individual decrements
against control-plane audit rows — the control-plane audit log records *policy*
events, not budget spend, so that half requires budget-spend events to be
audited first (a live-routing-path prerequisite, gated with the rest of G-009's
live-path work against the real identity model, #21/#22). See
the internal budget-lineage design doc §4.

## The invariant

Every child charge atomically debits its whole ancestor chain (see
`SqliteBudgetBackend._try_reserve_sync`). Therefore, for any ancestor:

    consumed(parent) >= Σ consumed(direct children)

A parent may *exceed* the sum (it can also be charged directly), but it must
never fall **below** it. `consumed(parent) < Σ consumed(children)` means a child
spent without its parent being debited — a lineage leak, i.e. the parent cap was
not actually protecting the subtree. That is the precise thing G-009 warned
about, made checkable.

A child that names a parent which is not itself registered is also flagged: the
chain cannot be reconciled if a link is missing.
"""
from __future__ import annotations

from typing import Iterable

# Statuses for a flagged parent.
KIND_UNDERDEBITED = "underdebited_parent"
KIND_UNREGISTERED = "unregistered_parent"

# Row shape: (key_blob, consumed_usd, parent_key_blobs).
LineageRow = tuple[str, float, tuple[str, ...]]


def reconcile_budget_lineage(
    rows: Iterable[LineageRow],
    *,
    tol: float = 1e-9,
) -> dict:
    """Check subtree spend conservation over a set of lineage rows.

    ``rows`` is an iterable of ``(key_blob, consumed_usd, parent_key_blobs)`` —
    exactly what ``SqliteBudgetBackend.iter_lineage_consumed()`` yields.

    Returns a structured summary (stable schema, mirroring the control-plane
    reconciliation surface): the reconciliation *reports* violations rather than
    raising, so monitoring can alert on ``converged is False`` and read the
    offending parents.
    """
    rows = list(rows)
    consumed: dict[str, float] = {kb: c for kb, c, _ in rows}

    child_sum: dict[str, float] = {}
    for _kb, c, parents in rows:
        for parent_blob in parents:
            child_sum[parent_blob] = child_sum.get(parent_blob, 0.0) + c

    violations: list[dict] = []
    for parent_blob, csum in sorted(child_sum.items()):
        parent_consumed = consumed.get(parent_blob)
        if parent_consumed is None:
            violations.append({
                "parent": parent_blob,
                "kind": KIND_UNREGISTERED,
                "child_consumed_sum": round(csum, 12),
            })
        elif parent_consumed + tol < csum:
            violations.append({
                "parent": parent_blob,
                "kind": KIND_UNDERDEBITED,
                "parent_consumed": round(parent_consumed, 12),
                "child_consumed_sum": round(csum, 12),
                "shortfall": round(csum - parent_consumed, 12),
            })

    return {
        "envelope_count": len(consumed),
        "parent_count": len(child_sum),
        "violation_count": len(violations),
        "converged": not violations,
        "violations": violations,
    }


def reconcile_budget_lineage_audited(
    backend,
    *,
    scope: str = "global",
    audit: bool = True,
) -> dict:
    """Reconcile a backend's budget lineage **against the control-plane audit
    log** (#70): run the subtree-conservation check over
    ``backend.iter_lineage_consumed()``, record the result as a tamper-evident
    row in the control-plane audit log, and report that log's chain integrity —
    the same treatment policy reconciliation (#60) gets.

    Returns the reconciliation summary plus ``audit_chain_status``
    (``"ok"`` / ``"tampered"``). Reports rather than raises, so monitoring can
    alert on either ``converged is False`` (a lineage leak) or
    ``audit_chain_status == "tampered"`` (the attestation log was altered).

    ``backend`` must expose ``iter_lineage_consumed()`` (SqliteBudgetBackend
    does). ``audit=False`` skips the append — used by tamper tests that must not
    extend a chain they have deliberately broken.
    """
    from llm_router.control_plane import audit as cpa

    summary = reconcile_budget_lineage(backend.iter_lineage_consumed())

    try:
        cpa.verify_cp_audit_chain()
        audit_chain_status = "ok"
    except cpa.TamperDetected:
        audit_chain_status = "tampered"

    result = {**summary, "scope": scope, "audit_chain_status": audit_chain_status}

    if audit:
        cpa.audit_budget_lineage_reconciliation(
            scope=scope,
            summary={
                "envelope_count": summary["envelope_count"],
                "parent_count": summary["parent_count"],
                "violation_count": summary["violation_count"],
                "converged": summary["converged"],
                "audit_chain_status": audit_chain_status,
            },
        )

    return result


__all__ = [
    "reconcile_budget_lineage",
    "reconcile_budget_lineage_audited",
    "KIND_UNDERDEBITED",
    "KIND_UNREGISTERED",
    "LineageRow",
]
