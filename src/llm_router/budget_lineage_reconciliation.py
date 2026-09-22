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


# `reconcile_budget_lineage_audited` was removed here (T-11, audit 2026-09-22).
#
# It did `from llm_router.control_plane import audit` inside its body, and
# `control_plane/audit.py` has never existed in this repository. So it shipped
# in the wheel, was importable, and raised `ImportError` on every call any
# installed user could make — for every version.
#
# Nothing in `src/` called it. The only callers were four tests, and those were
# converted from FAIL to SKIP by the root `conftest.py` excluded-module hook,
# which is why a function that could not run looked healthy for months.
#
# Removed rather than stubbed, following the M-10 precedent recorded in
# pyproject.toml: a stub would silently disable audit logging in a control
# plane, which is the opposite of what an audit module is for. Writing the
# missing module is feature work, not remediation. `reconcile_budget_lineage`
# (unaudited) is unaffected and is what every real caller uses.


__all__ = [
    "reconcile_budget_lineage",
    "KIND_UNDERDEBITED",
    "KIND_UNREGISTERED",
    "LineageRow",
]
