"""``llm_router migrate`` — inspect and apply versioned schema migrations.

Three subcommands:

* ``llm_router migrate status`` — show applied / pending / drifted migrations
  for the database at ``--db-path`` (defaults to ``~/.llm-router/usage.db``).
* ``llm_router migrate up [--target VERSION]`` — apply pending migrations.
* ``llm_router migrate down [--steps N | --target VERSION]`` — reverse the
  most recent N migrations or all migrations strictly newer than
  ``--target``.

The CLI is intentionally thin: every action delegates to
:mod:`llm_router.migrations`. Adding a new subcommand should not require
touching the migrations runner, and vice-versa.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from llm_router.migrations import apply, rollback, status


def _default_db_path() -> Path:
    """Production DB path; mirrors ``llm_router.config.get_config()``."""
    return Path.home() / ".llm-router" / "usage.db"


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path))


def _cmd_status(args: argparse.Namespace) -> int:
    conn = _open_db(args.db_path)
    try:
        report = status(conn)
    finally:
        conn.close()

    print(f"Database: {args.db_path}")
    print()
    print(f"Applied   ({len(report.applied)}):")
    for record in report.applied:
        marker = "  "
        if record.version in report.drifted:
            marker = "⚠ "
        elif record.version in report.missing_down:
            marker = "✗ "
        print(f"  {marker}{record.version}  {record.name:<32}"
              f"  applied {record.applied_at}  ({record.duration_ms}ms)")
    print()
    print(f"Pending   ({len(report.pending)}):")
    for version in report.pending:
        print(f"     {version}")
    if report.missing_down:
        print()
        print("Missing down() (rollback will fail for these):")
        for version in report.missing_down:
            print(f"     {version}")
    if report.drifted:
        print()
        print("⚠ Drifted (source changed since apply):")
        for version in report.drifted:
            print(f"     {version}")
    return 0


def _cmd_up(args: argparse.Namespace) -> int:
    conn = _open_db(args.db_path)
    try:
        applied = apply(conn, target=args.target)
    finally:
        conn.close()
    if not applied:
        print("Nothing to do — all migrations already applied.")
        return 0
    for record in applied:
        print(f"  ✓ {record.version}  {record.name}  ({record.duration_ms}ms)")
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    if args.target is None and args.steps is None:
        print("Refusing to roll back without --steps or --target.", file=sys.stderr)
        return 2

    conn = _open_db(args.db_path)
    try:
        reversed_versions = rollback(
            conn,
            target=args.target,
            steps=args.steps,
        )
    finally:
        conn.close()
    if not reversed_versions:
        print("Nothing to roll back.")
        return 0
    for version in reversed_versions:
        print(f"  ↓ {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm_router migrate")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_default_db_path(),
        help="SQLite file to operate on (default ~/.llm-router/usage.db).",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="Show applied vs pending migrations.")
    sp.set_defaults(func=_cmd_status)

    sp = sub.add_parser("up", help="Apply pending migrations.")
    sp.add_argument("--target", default=None, help="Stop after this version.")
    sp.set_defaults(func=_cmd_up)

    sp = sub.add_parser("down", help="Reverse one or more migrations.")
    sp.add_argument("--steps", type=int, default=None,
                    help="Number of migrations to roll back (default 1 if no --target).")
    sp.add_argument("--target", default=None,
                    help="Roll back everything strictly newer than this version.")
    sp.set_defaults(func=_cmd_down)

    args = parser.parse_args(argv)
    if args.cmd == "down" and args.target is None and args.steps is None:
        # Default to 1 step when neither flag is given.
        args.steps = 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
