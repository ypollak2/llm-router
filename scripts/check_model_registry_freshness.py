#!/usr/bin/env python3
"""WP-12 / RED8-08 — fail the build when the model snapshot goes stale.

NORTH_STAR.md described the capability ranking as "live" and
"continuously-updated". It is a hand-curated YAML file, and nothing fetches a
ranking at runtime. WP-12's locked decision was Option B: keep the static
ladder, stop calling it live, and *enforce* a refresh cadence — because a
manually-refreshed snapshot with no enforcement is just a stale snapshot that
nobody has noticed yet.

The evidence that this needs a machine rather than a convention: the header of
`config/models.yaml` instructed readers to "refresh periodically via
scripts/refresh-model-registry.py", and that script does not exist and never
did. The documented refresh path pointed at a file that was never written, so
"periodically" had nothing behind it at all.

This is a cadence check, not a correctness check. It cannot tell whether the
numbers are right — only whether anybody has looked recently. That is a real
limitation and it is the honest amount of assurance a curated snapshot can carry.

Exit 0 fresh, 1 stale, 2 malformed.

    python scripts/check_model_registry_freshness.py [--max-age-days N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "config" / "models.yaml"

#: One quarter. Long enough not to nag, short enough that a model generation
#: does not pass unnoticed — the frontier moves on a scale of weeks, so a
#: snapshot older than this is describing a different market.
MAX_SNAPSHOT_AGE_DAYS = 90

_SNAPSHOT_RE = re.compile(r"^snapshot_date:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def read_snapshot_date(path: Path) -> dt.date | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _SNAPSHOT_RE.search(text)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-days", type=int, default=MAX_SNAPSHOT_AGE_DAYS)
    ap.add_argument(
        "--today",
        default=None,
        help="ISO date to evaluate against; for tests, so no assertion depends "
        "on the wall clock",
    )
    args = ap.parse_args(argv[1:])

    if not REGISTRY.exists():
        print(f"WP-12: {REGISTRY} not found", file=sys.stderr)
        return 2

    snapshot = read_snapshot_date(REGISTRY)
    if snapshot is None:
        print(
            "WP-12: config/models.yaml has no parseable `snapshot_date:` line.\n"
            "An undated snapshot cannot be checked for staleness, and an\n"
            "unenforceable cadence is the thing this check exists to replace.",
            file=sys.stderr,
        )
        return 2

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    age = (today - snapshot).days

    if age > args.max_age_days:
        print(
            f"WP-12: the model registry snapshot is {age} days old "
            f"(limit {args.max_age_days}, dated {snapshot}).\n\n"
            "This ranking decides which model sits at the top of the escalation\n"
            "ladder, so a stale one silently pins routing to a previous model\n"
            "generation — the 'fixed vendor ranking' the North Star names as a\n"
            "violation, arrived at by neglect rather than by decision.\n\n"
            "Refresh the entries in config/models.yaml from\n"
            "https://artificialanalysis.ai/leaderboards/models by hand, then bump\n"
            "`snapshot_date`. Do NOT bump the date alone: that silences the\n"
            "check without doing the work it is asking for.",
            file=sys.stderr,
        )
        return 1

    print(f"WP-12: model registry snapshot is {age} days old (limit {args.max_age_days}) — fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
