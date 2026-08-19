"""Refinement #7 — admin-actions retention CLI.

Usage::

    llm_router admin-actions prune --older-than 90d
    llm_router admin-actions prune --older-than 12h --dry-run
    llm_router admin-actions count

The prune helper itself lives in ``llm_router.admin_actions.AdminActionLog.prune``;
this module is a thin CLI wrapper that parses the duration string,
emits a self-referential admin-action row BEFORE the delete (so the
audit trail records who pruned and when), then runs the prune.

The self-referential row uses an ``actor_user_id`` of ``"cli"`` /
``actor_email`` of ``"cli@localhost"`` because the CLI runs without
an authenticated identity. Operators can attribute the action via
the timestamp + shell history. A future slice can promote this to
require ``LLM_ROUTER_TOKEN`` under enterprise profile (G-002 follow-up).
"""
from __future__ import annotations

import sys


_USAGE = (
    "llm_router admin-actions — admin-action audit retention\n"
    "\n"
    "Subcommands:\n"
    "  count                            print total row count\n"
    "  prune --older-than DURATION      drop rows older than threshold\n"
    "  prune --older-than DURATION --dry-run\n"
    "                                   preview the count, no delete\n"
    "\n"
    "DURATION formats:\n"
    "  Nd      N days  (e.g. 90d)\n"
    "  Nh      N hours (e.g. 12h)\n"
    "  Nm      N minutes\n"
    "  Ns      N seconds\n"
    "  N       N seconds (bare integer)\n"
    "\n"
    "Examples:\n"
    "  llm_router admin-actions count\n"
    "  llm_router admin-actions prune --older-than 90d --dry-run\n"
    "  llm_router admin-actions prune --older-than 30d\n"
)


# Suffix → seconds-per-unit. Anything else parses as bare integer
# seconds.
_DURATION_UNITS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def _parse_duration(raw: str) -> float:
    """Parse a duration like ``"90d"`` / ``"12h"`` / ``"600"`` into
    seconds. Raises ``ValueError`` on malformed input."""
    raw = raw.strip()
    if not raw:
        raise ValueError("duration cannot be empty")
    last = raw[-1].lower()
    if last in _DURATION_UNITS:
        body = raw[:-1].strip()
        try:
            value = float(body)
        except ValueError as exc:
            raise ValueError(
                f"invalid duration {raw!r}: {body!r} is not a number"
            ) from exc
        return value * _DURATION_UNITS[last]
    # Bare integer / float → seconds.
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid duration {raw!r}: expected NUMBER[s|m|h|d]"
        ) from exc


def cmd_admin_actions(args: list[str]) -> int:
    """Execute: ``llm_router admin-actions <subcommand> [flags]``."""
    if not args or args[0] in ("--help", "-h"):
        print(_USAGE)
        return 0

    sub = args[0]
    rest = args[1:]

    from llm_router.admin_actions import AdminActionLog

    if sub == "count":
        log = AdminActionLog()
        print(log.count())
        return 0

    if sub == "prune":
        older_than: str | None = None
        dry_run = False
        i = 0
        while i < len(rest):
            flag = rest[i]
            if flag in ("--help", "-h"):
                print(_USAGE)
                return 0
            if flag == "--older-than":
                if i + 1 >= len(rest):
                    print(
                        "--older-than requires a duration value",
                        file=sys.stderr,
                    )
                    return 1
                older_than = rest[i + 1]
                i += 2
                continue
            if flag == "--dry-run":
                dry_run = True
                i += 1
                continue
            print(f"Unknown flag: {flag!r}", file=sys.stderr)
            return 1

        if older_than is None:
            print(
                "prune requires --older-than DURATION", file=sys.stderr
            )
            return 1

        try:
            seconds = _parse_duration(older_than)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if seconds <= 0:
            print(
                "--older-than must be positive — refusing to prune "
                "the entire table",
                file=sys.stderr,
            )
            return 1

        log = AdminActionLog()
        # Emit the self-referential audit row BEFORE the delete so
        # the row about to be pruned cannot include itself. Under
        # dry-run mode we still emit so operators can see attempted
        # prunes in the log; the delete is skipped per the kwarg.
        log.append(
            actor_user_id="cli",
            actor_email="cli@localhost",
            action=(
                "admin_actions:prune_dry_run" if dry_run
                else "admin_actions:prune"
            ),
            resource_id=str(log.db_path),
            detail={
                "older_than_seconds": seconds,
                "older_than_input": older_than,
            },
        )
        result = log.prune(
            older_than_seconds=seconds, dry_run=dry_run,
        )
        verb = "would delete" if dry_run else "deleted"
        print(
            f"{verb} {result['would_delete']} row(s) older than "
            f"{older_than} (cutoff_ts={result['cutoff_ts']:.0f}, "
            f"db={log.db_path})"
        )
        return 0

    print(f"Unknown subcommand: {sub!r}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 1
