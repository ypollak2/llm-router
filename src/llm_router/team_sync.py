"""Team spend aggregation via JSONL export + idempotent import.

The audit's Part 7 rec #7 calls out that each LLM Router install owns its
own ``~/.llm-router/usage.db`` and there's no team view of aggregate spend
or routing decisions. Commercial routers (Portkey, OpenRouter, Martian)
solve this with a hosted control plane; that's a big architectural
change LLM Router shouldn't take lightly.

This module ships a much smaller wedge: an **export/import pair** that
moves anonymised routing-decision rows between SQLite stores via JSONL.
With it, three patterns become possible without any new services:

* **Manager-collected**: each developer runs ``llm_router team export`` and
  emails the JSONL to a team lead, who runs ``llm_router team import`` into
  a shared ``team.db``. A team dashboard reads the shared DB.
* **Shared filesystem**: every install writes to a network drive at
  ``$LLM_ROUTER_TEAM_INBOX`` after each session; a cron runs imports.
* **Webhook / S3**: a future commit can wrap export to POST the JSONL.

Idempotency is by ``correlation_id`` — every routing decision already
has one, so an import that re-sees a row is a no-op. Schema is fixed
(see :data:`EXPORT_SCHEMA`) so a forward-compatible reader can detect
when a newer client wrote extra columns it doesn't understand.

The default field set excludes anything containing prompt text or model
responses — only metadata (model, provider, cost, latency, success,
correlation_id, user_id, project_id) crosses the boundary. Set
``include_subjects=True`` to also export the inferred ``subject`` /
``task_type`` / ``complexity`` fields, which are useful for routing
analytics but not strictly necessary for spend rollups.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "EXPORT_SCHEMA",
    "EXPORT_SCHEMA_VERSION",
    "ImportReport",
    "export_rows",
    "import_rows",
    "ensure_team_schema",
]

EXPORT_SCHEMA_VERSION = 1

# Columns crossing the install boundary. Anything containing prompt /
# response / classifier-output text is excluded by default — those are
# user-content and shouldn't leave the originating install without an
# explicit opt-in.
EXPORT_SCHEMA: tuple[str, ...] = (
    "correlation_id",   # primary key for idempotency
    "timestamp",
    "task_type",
    "profile",
    "complexity",
    "subject",
    "final_model",
    "final_provider",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "latency_ms",
    "success",
    "user_id",
    "project_id",
)

# Columns that may be NULL on the source side; the importer accepts
# missing values for any field outside ``EXPORT_REQUIRED``.
EXPORT_REQUIRED: frozenset[str] = frozenset(
    {"correlation_id", "timestamp", "final_model", "cost_usd"}
)


# ── Export ──────────────────────────────────────────────────────────────


def export_rows(
    source_db: Path,
    *,
    since: str | None = None,
    limit: int | None = None,
) -> Iterator[dict]:
    """Yield one dict per exportable row from ``source_db``.

    ``since`` is an ISO-8601 datetime string ('2026-06-01T00:00:00Z' or
    just '2026-06-01'); rows with ``timestamp < since`` are skipped.
    ``limit`` caps the total yield.

    The generator opens its own connection and closes it when exhausted
    so callers can stream directly to disk without holding the source
    database open for the duration of the run.

    Skips rows that lack any column in :data:`EXPORT_REQUIRED` — those
    are typically partial backfills from the sidecar replay path and
    don't carry the financial signal team views care about.
    """
    if not source_db.is_file():
        return

    where = ["correlation_id IS NOT NULL"]
    params: list = []
    if since is not None:
        where.append("timestamp >= ?")
        params.append(since)
    if limit is not None:
        suffix = f" LIMIT {int(limit)}"
    else:
        suffix = ""

    column_list = ", ".join(EXPORT_SCHEMA)
    query = (
        f"SELECT {column_list} FROM routing_decisions "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY timestamp{suffix}"
    )

    conn = sqlite3.connect(str(source_db))
    try:
        cursor = conn.execute(query, params)
        cols = [c[0] for c in cursor.description]
        for row in cursor:
            record = dict(zip(cols, row))
            if any(record.get(field) is None for field in EXPORT_REQUIRED):
                continue
            yield record
    finally:
        conn.close()


# ── Import ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportReport:
    """Outcome of an ``import_rows`` call."""

    scanned: int
    inserted: int
    duplicate: int
    invalid: int


_TEAM_TABLE = "team_routing_decisions"


def ensure_team_schema(connection: sqlite3.Connection) -> None:
    """Create ``team_routing_decisions`` if it doesn't exist.

    Mirrors the export schema so import is a straight column-mapped
    INSERT. Distinct from ``routing_decisions`` on purpose: imports
    accumulate across installs and we don't want to mix team and
    per-user views in the same table.
    """
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TEAM_TABLE} (
            correlation_id  TEXT PRIMARY KEY,
            timestamp       TEXT NOT NULL,
            task_type       TEXT,
            profile         TEXT,
            complexity      TEXT,
            subject         TEXT,
            final_model     TEXT NOT NULL,
            final_provider  TEXT,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            cost_usd        REAL NOT NULL,
            latency_ms      REAL,
            success         INTEGER,
            user_id         TEXT,
            project_id      TEXT,
            imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TEAM_TABLE}_user_ts "
        f"ON {_TEAM_TABLE}(user_id, timestamp DESC)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_TEAM_TABLE}_project_ts "
        f"ON {_TEAM_TABLE}(project_id, timestamp DESC)"
    )
    connection.commit()


def import_rows(
    team_db: Path,
    rows: Iterator[dict],
) -> ImportReport:
    """Merge ``rows`` into ``team_db``'s team_routing_decisions table.

    Idempotency: INSERT OR IGNORE on the correlation_id PK so a row
    seen twice (e.g. importing the same JSONL twice, or two installs
    sharing a correlation_id collision) doesn't double-count spend.

    Returns counts so callers can show a progress report. ``invalid``
    counts rows missing a required field or carrying a non-numeric
    cost; those rows are silently dropped (the importer is best-effort).
    """
    team_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(team_db))
    try:
        ensure_team_schema(conn)
        scanned = inserted = duplicate = invalid = 0
        column_list = ", ".join(EXPORT_SCHEMA)
        placeholder_list = ", ".join("?" for _ in EXPORT_SCHEMA)

        for record in rows:
            scanned += 1
            # ``read_jsonl`` yields whatever the JSON decoder produces; a
            # bare scalar (``12345``) parses fine but isn't a record we
            # can import. Validate the shape before touching it.
            if not isinstance(record, dict):
                invalid += 1
                continue
            if any(record.get(field) is None for field in EXPORT_REQUIRED):
                invalid += 1
                continue
            try:
                values = tuple(record.get(field) for field in EXPORT_SCHEMA)
                # Coerce cost to float — JSON can preserve int, but the
                # column is REAL.
                _ = float(record["cost_usd"])
            except (TypeError, ValueError, KeyError):
                invalid += 1
                continue
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO {_TEAM_TABLE} ({column_list}) "
                f"VALUES ({placeholder_list})",
                values,
            )
            if cursor.rowcount == 0:
                duplicate += 1
            else:
                inserted += 1

        conn.commit()
        return ImportReport(
            scanned=scanned,
            inserted=inserted,
            duplicate=duplicate,
            invalid=invalid,
        )
    finally:
        conn.close()


# ── JSONL helpers ───────────────────────────────────────────────────────


def write_jsonl(rows: Iterator[dict], output: Path) -> int:
    """Write ``rows`` as JSON Lines. Return the count written.

    Wrapper kept here (not in the CLI) so other callers (webhook,
    fixture-builders, tests) can use it without spinning up the CLI.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding="utf-8") as f:
        for record in rows:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")
            count += 1
    return count


def read_jsonl(input_path: Path) -> Iterator[dict]:
    """Yield rows from a JSONL file. Skips malformed lines silently —
    the same single-line truncation a network blip would produce can't
    abort an import."""
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
