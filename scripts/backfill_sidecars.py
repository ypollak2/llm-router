"""Backfill routing_decisions from ~/.llm-router/last_route_*.json sidecars.

Background
----------
The auto-route hook writes a per-decision JSON sidecar
(``~/.llm-router/last_route_<session_id>.json``) every time it classifies a
prompt. When Claude subsequently bypasses the route (CONTINUATION bypass,
DIRECT SKIP, etc.), no row is written into ``routing_decisions`` because
the corresponding MCP tool never runs. Over time this leaves the
decision-history table starved while the sidecar pile grows on disk.

This one-shot script replays the sidecars into ``routing_decisions`` so
the quality-report dashboard and any downstream analytics see a complete
record of *what the router decided*, even when the host bypassed it.

What does NOT get backfilled
----------------------------
* ``usage`` table rows — sidecars carry no token/cost outcome data, so
  the savings dashboard remains driven by real routed calls only.
* ``lineage.db:lineage`` rows — same reason; the lineage store records
  outcomes, not intents.

Idempotency
-----------
Each sidecar gets a stable ``correlation_id`` derived from the filename
(``sidecar:<session_id>:<saved_at>``). A pre-insert SELECT skips rows
that already carry that correlation_id, so re-running is safe.

Usage
-----
::

    .venv/bin/python scripts/backfill_sidecars.py
    .venv/bin/python scripts/backfill_sidecars.py --dry-run
    .venv/bin/python scripts/backfill_sidecars.py --sidecar-dir /tmp/.llm_router
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Allow direct invocation (`python scripts/backfill_sidecars.py`) without
# needing to be a console-script entry point. Editable installs already
# resolve `llm_router` via site-packages, so this only matters when the
# venv isn't activated.
if str(Path(__file__).resolve().parent.parent / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llm_router.cost import _get_db


@dataclass(frozen=True)
class SidecarRecord:
    """One parsed sidecar — minimal intent record from the auto-route hook."""

    session_id: str
    task_type: str
    complexity: str
    tool: str
    saved_at: float
    correlation_id: str
    iso_timestamp: str

    @classmethod
    def from_path(cls, path: Path) -> "SidecarRecord | None":
        """Parse a sidecar file. Return None for malformed/unreadable files."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        task_type = data.get("task_type")
        complexity = data.get("complexity")
        tool = data.get("tool")
        saved_at = data.get("saved_at")

        if not (task_type and complexity and tool and saved_at):
            return None

        # Filename pattern: last_route_<session_id>.json
        session_id = path.stem.removeprefix("last_route_")
        correlation_id = f"sidecar:{session_id}:{saved_at}"
        iso = datetime.fromtimestamp(
            float(saved_at), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")

        return cls(
            session_id=session_id,
            task_type=str(task_type),
            complexity=str(complexity),
            tool=str(tool),
            saved_at=float(saved_at),
            correlation_id=correlation_id,
            iso_timestamp=iso,
        )


@dataclass(frozen=True)
class BackfillReport:
    scanned: int
    inserted: int
    skipped_duplicate: int
    skipped_malformed: int


async def backfill(
    sidecar_dir: Path,
    *,
    dry_run: bool = False,
) -> BackfillReport:
    """Replay sidecars from ``sidecar_dir`` into routing_decisions.

    Skips files that don't match the sidecar pattern, are malformed, or
    have already been backfilled (identified via correlation_id).
    """
    sidecar_paths = sorted(sidecar_dir.glob("last_route_*.json"))

    inserted = skipped_duplicate = skipped_malformed = 0

    db = await _get_db()
    try:
        for path in sidecar_paths:
            record = SidecarRecord.from_path(path)
            if record is None:
                skipped_malformed += 1
                continue

            cursor = await db.execute(
                "SELECT 1 FROM routing_decisions WHERE correlation_id = ? LIMIT 1",
                (record.correlation_id,),
            )
            already = await cursor.fetchone()
            if already:
                skipped_duplicate += 1
                continue

            if dry_run:
                inserted += 1
                continue

            await db.execute(
                """
                INSERT INTO routing_decisions (
                    timestamp, task_type, complexity, success,
                    reason_code, correlation_id
                )
                VALUES (?, ?, ?, 0, 'sidecar_backfill', ?)
                """,
                (
                    record.iso_timestamp,
                    record.task_type,
                    record.complexity,
                    record.correlation_id,
                ),
            )
            inserted += 1

        if not dry_run:
            await db.commit()
    finally:
        await db.close()

    return BackfillReport(
        scanned=len(sidecar_paths),
        inserted=inserted,
        skipped_duplicate=skipped_duplicate,
        skipped_malformed=skipped_malformed,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--sidecar-dir",
        type=Path,
        default=Path.home() / ".llm-router",
        help="Directory containing last_route_*.json files (default ~/.llm-router)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be inserted; do not write to the DB.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.sidecar_dir.is_dir():
        print(
            f"sidecar dir not found: {args.sidecar_dir}", file=sys.stderr
        )
        return 1
    report = asyncio.run(backfill(args.sidecar_dir, dry_run=args.dry_run))
    verb = "would insert" if args.dry_run else "inserted"
    print(
        f"Scanned {report.scanned} sidecar(s)  ·  "
        f"{verb} {report.inserted}  ·  "
        f"skipped {report.skipped_duplicate} duplicate, "
        f"{report.skipped_malformed} malformed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
