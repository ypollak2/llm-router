#!/usr/bin/env python3
"""Repair the 2026-09-29 savings_stats double-count (host="agentic" x2).

BUG. Every ``llm_delegate`` call wrote its delegation's ``saved_usd`` into
``savings_stats`` TWICE:

  1. directly, via ``agentic.telemetry.record_delegation_savings``
     (``host="agentic"``, ``task_type="code"`` by default);
  2. as the SAME delegation's ``routing_quality.jsonl`` parent row
     (``route_kind`` / ``task_type`` = ``"delegate"`` or
     ``"bounded_operational"``), which ``cost.import_routing_quality_ledger``
     (PR #171) then imported a second time as its own ``savings_stats`` row
     (``host="routing_quality"``).

``src/llm_router/cost.py``'s ``import_routing_quality_ledger`` now excludes
those ``route_kind``s at the source (see ``DELEGATION_ROUTE_KINDS``), so this
double-write cannot happen again for calls made after the fix ships. This
script repairs the rows it ALREADY wrote, on 2026-09-28, before the fix
existed.

WHAT IT DOES. For every ``savings_stats`` row with
``host='routing_quality' AND task_type IN ('delegate', 'bounded_operational')``
that has not already been repaired, it zeroes
``estimated_claude_cost_saved`` (so every existing SUM(...) reader stops
double-counting it, with no code changes required anywhere else) and
records the original value plus a reason code in two new, nullable columns
(``corrected_from``, ``correction_reason``) so the operation is fully
auditable and REVERSIBLE — the row itself, and the evidence of what it used
to say, are never deleted.

SAFETY.
  * Dry-run by default. Nothing is written unless you pass ``--apply``.
  * Idempotent: a row already carrying ``correction_reason`` is skipped, so
    running this twice (or on an already-repaired DB) is a no-op the second
    time — verified by scripts/test_repair fixtures / manual runs, see the
    PR description for the real numbers.
  * Never touches rows outside the exact `(host, task_type)` pair this
    incident produced — a legitimate `routing_quality` row for `llm(task=...)`
    (task_type in {"query","code","analyze","research","generate"}) is
    untouched.

USAGE.
    python3 scripts/repair_savings_double_count.py                  # dry run, default DB
    python3 scripts/repair_savings_double_count.py --db-path P.db   # dry run, explicit DB
    python3 scripts/repair_savings_double_count.py --apply           # write, default DB
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DUPLICATE_TASK_TYPES = ("delegate", "bounded_operational")
DUPLICATE_HOST = "routing_quality"
CORRECTION_REASON = "double_count_agentic_routing_quality_delegate_2026_09_29"


def _default_db_path() -> Path:
    """Mirrors ``llm_router.paths.state_path("usage.db")`` without importing
    the package, so this script has no import-time dependency on it (and can
    run against a bare copy of the DB with no llm_router installed)."""
    import os

    override = (os.environ.get("LLM_ROUTER_DB_PATH") or "").strip()
    if override:
        return Path(override)
    home = (os.environ.get("LLM_ROUTER_HOME") or "").strip()
    base = Path(home) if home else Path.home() / ".llm-router"
    return base / "usage.db"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def find_affected_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows this incident produced that have not yet been repaired."""
    conn.row_factory = sqlite3.Row
    cols = _column_names(conn, "savings_stats")
    has_reason_col = "correction_reason" in cols
    reason_filter = (
        "AND (correction_reason IS NULL)" if has_reason_col else ""
    )
    placeholders = ",".join("?" for _ in DUPLICATE_TASK_TYPES)
    query = (
        "SELECT id, timestamp, session_id, task_type, model_used, host, "
        "estimated_claude_cost_saved, route_id "
        "FROM savings_stats "
        f"WHERE host = ? AND task_type IN ({placeholders}) "
        "AND estimated_claude_cost_saved != 0 "
        f"{reason_filter} "
        "ORDER BY timestamp"
    )
    return list(conn.execute(query, (DUPLICATE_HOST, *DUPLICATE_TASK_TYPES)))


def ensure_correction_columns(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE, same pattern as cost.py's other savings_stats
    migrations: nullable, no default, additive only."""
    cols = _column_names(conn, "savings_stats")
    if "corrected_from" not in cols:
        conn.execute("ALTER TABLE savings_stats ADD COLUMN corrected_from REAL")
    if "correction_reason" not in cols:
        conn.execute("ALTER TABLE savings_stats ADD COLUMN correction_reason TEXT")


def apply_repair(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    ensure_correction_columns(conn)
    fixed = 0
    for row in rows:
        cur = conn.execute(
            "UPDATE savings_stats "
            "SET corrected_from = estimated_claude_cost_saved, "
            "    estimated_claude_cost_saved = 0.0, "
            "    correction_reason = ? "
            "WHERE id = ? AND (correction_reason IS NULL)",
            (CORRECTION_REASON, row["id"]),
        )
        fixed += cur.rowcount
    conn.commit()
    return fixed


def _print_report(rows: list[sqlite3.Row], *, applying: bool) -> None:
    if not rows:
        print("No affected rows found. Nothing to do (already repaired, or "
              "the double-count never happened on this database).")
        return

    total = sum(r["estimated_claude_cost_saved"] for r in rows)
    verb = "REPAIRED" if applying else "WOULD REPAIR (dry run — pass --apply to write)"
    print(f"{verb}: {len(rows)} row(s), ${total:.2f} of duplicated savings\n")
    print(f"{'id':>6}  {'timestamp':<32}  {'task_type':<20}  {'saved_usd':>10}  route_id")
    for r in rows:
        rid = r["route_id"] or "-"
        print(f"{r['id']:>6}  {r['timestamp']:<32}  {r['task_type']:<20}  "
              f"{r['estimated_claude_cost_saved']:>10.4f}  {rid}")
    print(f"\nTotal duplicated: ${total:.2f}")
    if not applying:
        print("\nEach row's estimated_claude_cost_saved would be set to 0.0 "
              "(original value preserved in a new 'corrected_from' column, "
              "reason recorded in 'correction_reason'). No row is deleted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db-path", type=Path, default=None,
                         help="Path to usage.db (default: $LLM_ROUTER_DB_PATH, "
                              "$LLM_ROUTER_HOME/usage.db, or ~/.llm-router/usage.db)")
    parser.add_argument("--apply", action="store_true",
                         help="Actually write the repair. Without this flag, "
                              "the script only reports what it would do.")
    args = parser.parse_args(argv)

    db_path = args.db_path or _default_db_path()
    if not db_path.exists():
        print(f"No database at {db_path} — nothing to repair.", file=sys.stderr)
        return 1

    if args.apply:
        conn = sqlite3.connect(str(db_path))
    else:
        # mode=ro: never take a write lock, never risk mutating the operator's
        # live DB during a dry run, even if a bug crept into this script.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = find_affected_rows(conn)
        if args.apply:
            fixed = apply_repair(conn, rows)
            _print_report(rows, applying=True)
            if fixed != len(rows):
                print(f"\nNOTE: {len(rows)} row(s) matched but only {fixed} "
                      "were updated (a concurrent run likely repaired the "
                      "rest first — this is expected and safe).")
        else:
            _print_report(rows, applying=False)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
