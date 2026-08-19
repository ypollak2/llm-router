"""Canonical routing attribution — one definition, consumed by every surface.

WHY THIS MODULE EXISTS
----------------------
Two product surfaces reported contradictory answers for the same session because they
read different tables and applied different rules:

    30-day dashboard   -> routing_decisions   (34 cols, HAS classifier_type)
    MODELS this session-> usage               (22 cols, NO classifier_type)

The attribution rule introduced by `0aab32f` was expressed as a filter on a column the
other table does not have, so the second surface could not apply it even in principle.
Every consumer re-deciding "does this row count?" is the defect; this module decides once.

THE TWO DIMENSIONS, WHICH ARE NOT THE SAME QUESTION
---------------------------------------------------
Measured over the live database, 30-day window:

    classifier_type='unknown'  AND provenance='unattributed'   28683   <- ALL of them
    classifier_type='heuristic'    provenance=NULL              3631
    other real classifiers        provenance=NULL                 38

28536 of those 28683 rows are `openai/gpt-4o-mini` — the test pollution that Finding #30
identified and `0aab32f` removed by excluding `classifier_type='unknown'`.

That exclusion produced the right numbers for the wrong reason. The two facts are
independent:

    provenance      = WHERE THE ROW CAME FROM   (real traffic vs test/synthetic)
    classifier_type = HOW THE DECISION WAS MADE (heuristic, fallback, unrecorded)

They are perfectly correlated in today's data only because the synthetic rows happened to
carry no classifier. Filtering attribution on `classifier_type` therefore works today and
would silently misclassify the first REAL routing decision whose classifier was not
recorded — counting genuine traffic as non-routing.

CANONICAL RULE
--------------
    ATTRIBUTED   = the row describes real routing traffic  (provenance IS NULL)
    UNATTRIBUTED = the row is test/synthetic in origin     (provenance = 'unattributed')

`classifier_type` is reported as a property OF an attributed decision, never as the test
for whether it counts. An attributed decision whose classifier is `unknown` is a routing
decision with an unrecorded classifier — which is what it says.

On today's data this yields numbers IDENTICAL to the current dashboard, because of the
correlation above. It is not a no-op: it is the same answer derived from the fact that
actually determines it, and it stays correct when the correlation breaks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AttributionStatus(str, Enum):
    """Explicit, per the directive's §8 — never inferred from a missing value.

    THREE states, not two. The first version of this module had only ATTRIBUTED and
    UNATTRIBUTED and treated `provenance IS NULL` as attributed. That was wrong, and the
    reason is worth keeping:

    **Nothing in the codebase ever wrote `provenance`.** The column defaults to NULL, and
    `log_routing_decision`'s INSERT did not include it. `0aab32f` then marked one known
    synthetic population `unattributed` retroactively — so NULL never meant "real
    traffic", it meant "not yet cleaned up". A second synthetic population was sitting
    inside that NULL set and was being counted as routing.

    Rows written from now on carry their origin (see `cost._write_provenance`). Rows
    written before that change cannot be classified either way, and saying so is the only
    honest option: guessing in either direction manufactures a fact the writer never
    recorded.
    """

    ATTRIBUTED = "attributed"        # provenance says: real runtime traffic
    UNATTRIBUTED = "unattributed"    # provenance says: test / synthetic
    UNKNOWN = "unknown"              # no provenance recorded — predates writer-side marking


#: Provenance values that mean "this row is real user traffic".
ATTRIBUTED_PROVENANCE: frozenset[str] = frozenset({"runtime"})

#: Provenance values that mean "this row is not user traffic". A set rather than a
#: string comparison so a future marker ("synthetic", "replay", "benchmark") lands in
#: ONE place instead of being re-decided per consumer.
UNATTRIBUTED_PROVENANCE: frozenset[str] = frozenset({"unattributed", "test"})


@dataclass(frozen=True)
class ModelShare:
    model: str
    decisions: int
    share: float          # 0.0–1.0 of attributed decisions


@dataclass(frozen=True)
class AttributionResult:
    """What every surface consumes. Counts first, percentages derived — never the
    reverse, so a caller cannot round a percentage back into a count."""

    attributed_decisions: int = 0
    unattributed_decisions: int = 0
    unknown_decisions: int = 0
    by_model: tuple[ModelShare, ...] = ()
    unattributed_by_model: tuple[ModelShare, ...] = ()
    unknown_by_model: tuple[ModelShare, ...] = ()
    classifier_breakdown: dict[str, int] = field(default_factory=dict)
    source_table: str = "routing_decisions"
    window_description: str = ""

    @property
    def eligible_decisions(self) -> int:
        return (self.attributed_decisions + self.unattributed_decisions
                + self.unknown_decisions)

    @property
    def is_reportable(self) -> bool:
        """False when unknown-provenance rows could change the answer.

        A share computed over 3,669 attributed rows means nothing if 28,000 rows of
        unknown origin sit beside it — the true denominator is unknowable. Surfaces
        should refuse to render a percentage rather than render a confident wrong one.
        """
        return self.unknown_decisions == 0

    def check_invariants(self) -> None:
        """§14. Raises rather than returning a bool: a violated invariant is a defect
        in this module, not a condition for a caller to branch on."""
        if min(self.attributed_decisions, self.unattributed_decisions,
               self.unknown_decisions) < 0:
            raise ValueError("negative decision count")

        counted = sum(m.decisions for m in self.by_model)
        if counted != self.attributed_decisions:
            raise ValueError(
                f"by_model sums to {counted}, attributed_decisions is "
                f"{self.attributed_decisions}"
            )
        if self.attributed_decisions and abs(sum(m.share for m in self.by_model) - 1.0) > 1e-9:
            raise ValueError("attributed model shares do not sum to 1.0")

        # classifier_breakdown describes ATTRIBUTED decisions only.
        cls_total = sum(self.classifier_breakdown.values())
        if cls_total != self.attributed_decisions:
            raise ValueError(
                f"classifier_breakdown sums to {cls_total}, attributed_decisions is "
                f"{self.attributed_decisions}"
            )


def _shares(counts: dict[str, int]) -> tuple[ModelShare, ...]:
    total = sum(counts.values())
    if not total:
        return ()
    return tuple(
        ModelShare(model=m, decisions=n, share=n / total)
        for m, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def attribution_from_rows(rows, *, window_description: str = "") -> AttributionResult:
    """Build the canonical result from already-fetched rows.

    Separated from the query so tests can drive it with a controlled event set and so
    any future source (a different table, an export, a replay) reaches the SAME rule.
    Each row needs `final_model`, `provenance`, `classifier_type`.
    """
    attributed: dict[str, int] = {}
    unattributed: dict[str, int] = {}
    unknown: dict[str, int] = {}
    classifiers: dict[str, int] = {}

    for r in rows:
        model = (r["final_model"] or "").strip() or "unknown"
        provenance = (r["provenance"] or "").strip()

        if provenance in UNATTRIBUTED_PROVENANCE:
            unattributed[model] = unattributed.get(model, 0) + 1
        elif provenance in ATTRIBUTED_PROVENANCE:
            attributed[model] = attributed.get(model, 0) + 1
            # An attributed decision with no recorded classifier is still a decision.
            ct = (r["classifier_type"] or "").strip() or "unrecorded"
            classifiers[ct] = classifiers.get(ct, 0) + 1
        else:
            # No provenance, or a value this version does not know. Never silently
            # promoted into either bucket — an unrecognised marker is a reason to stop,
            # not to guess.
            unknown[model] = unknown.get(model, 0) + 1

    result = AttributionResult(
        attributed_decisions=sum(attributed.values()),
        unattributed_decisions=sum(unattributed.values()),
        unknown_decisions=sum(unknown.values()),
        by_model=_shares(attributed),
        unattributed_by_model=_shares(unattributed),
        unknown_by_model=_shares(unknown),
        classifier_breakdown=classifiers,
        window_description=window_description,
    )
    result.check_invariants()
    return result


def routing_attribution(
    db_path: Path | str,
    *,
    since_sql: str = "datetime('now','-30 days')",
    window_description: str = "last 30 days",
) -> AttributionResult:
    """Canonical attribution over `routing_decisions`.

    `since_sql` is a SQL expression rather than a bound value because the existing
    surfaces express their windows that way; it is NOT caller-supplied user input.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(routing_decisions)")}
        prov = "provenance" if "provenance" in cols else "NULL AS provenance"
        ctyp = "classifier_type" if "classifier_type" in cols else "NULL AS classifier_type"
        rows = conn.execute(
            f"SELECT final_model, {prov}, {ctyp} FROM routing_decisions "
            f"WHERE timestamp >= {since_sql}"
        ).fetchall()
    finally:
        conn.close()
    return attribution_from_rows(rows, window_description=window_description)
