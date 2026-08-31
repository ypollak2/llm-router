"""``llm_router team-sync`` — JSONL export/import for cross-install spend rollups.

Three subcommands:

* ``llm_router team-sync export --output FILE [--since DATE] [--limit N]`` —
  dump routing decisions from the local ``usage.db`` to a JSONL file.
* ``llm_router team-sync import --input FILE --team-db PATH`` — merge a
  JSONL into a shared team database (idempotent via ``correlation_id``).
* ``llm_router team-sync summary --team-db PATH`` — quick aggregate of
  total spend + calls per user / project across the team database.

Designed so users can email JSONL files, sync via a shared filesystem,
or wrap export to POST to a webhook — none of those require LLM Router to
grow a hosted control plane. See :mod:`llm_router.team_sync` for the
underlying export/import primitives.

Distinct from :mod:`llm_router.commands.team`, which owns the existing
Slack-report + notification surface.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from llm_router.team_sync import (
    export_rows,
    import_rows,
    read_jsonl,
    write_jsonl,
)


def _default_source_db() -> Path:
    return Path.home() / ".llm-router" / "usage.db"


def _default_team_db() -> Path:
    return Path.home() / ".llm-router" / "team.db"


def _cmd_export(args: argparse.Namespace) -> int:
    rows = export_rows(args.source_db, since=args.since, limit=args.limit)
    count = write_jsonl(rows, args.output)
    print(f"Wrote {count} rows to {args.output}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    if not args.input_path.is_file():
        print(f"Input file not found: {args.input_path}", file=sys.stderr)
        return 1
    report = import_rows(args.team_db, read_jsonl(args.input_path))
    print(f"Scanned   {report.scanned}")
    print(f"Inserted  {report.inserted}")
    print(f"Duplicate {report.duplicate}")
    print(f"Invalid   {report.invalid}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    if not args.team_db.is_file():
        print(f"Team DB not found: {args.team_db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(args.team_db))
    try:
        totals = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), "
            "COUNT(DISTINCT user_id), COUNT(DISTINCT project_id) "
            "FROM team_routing_decisions"
        ).fetchone()
        calls, total_cost, users, projects = totals
        print(f"Team rollup ({args.team_db}):")
        print(f"  calls    {calls}")
        print(f"  spend    ${total_cost:.4f}")
        print(f"  users    {users}")
        print(f"  projects {projects}")
        print()
        print("By user:")
        for row in conn.execute(
            "SELECT user_id, COUNT(*), COALESCE(SUM(cost_usd), 0) "
            "FROM team_routing_decisions "
            "WHERE user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY 3 DESC LIMIT 20"
        ):
            user_id, n, spend = row
            print(f"  {user_id or '<none>':<20}  {n:>5} calls  ${spend:.4f}")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-router team-sync")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("export", help="Dump local routing decisions to JSONL.")
    sp.add_argument("--output", type=Path, required=True)
    sp.add_argument("--source-db", type=Path, default=_default_source_db())
    sp.add_argument("--since", default=None,
                    help="ISO date; rows older are skipped.")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=_cmd_export)

    sp = sub.add_parser("import", help="Merge a JSONL into the team database.")
    sp.add_argument("--input", dest="input_path", type=Path, required=True)
    sp.add_argument("--team-db", type=Path, default=_default_team_db())
    sp.set_defaults(func=_cmd_import)

    sp = sub.add_parser("summary", help="Show team-wide spend rollup.")
    sp.add_argument("--team-db", type=Path, default=_default_team_db())
    sp.set_defaults(func=_cmd_summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
