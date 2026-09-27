#!/usr/bin/env python3
"""NS3 experiment: what fraction of validated llm_edit output survives without a redo?

NORTH_STAR's primary metric needs "routed AND used as-is (no Claude redo)". `llm_edit`
(src/llm_router/edit.py, src/llm_router/edit_ledger.py) writes one row per file it
validated to ``~/.llm-router/edit_outcomes.jsonl`` — but that ledger only knows
``applied`` (did the returned pairs pass exact-once + syntax validation at call
time?), never ``survived`` (did the edit actually stay, or did Claude quietly redo
it?). This script answers the second question after the fact, by reading git history
for each ledger row's file.

METHOD (read this before trusting the number)
----------------------------------------------
For every ledger row with ``applied: true``:

  1. If the file is not inside a git work tree, or `git log` can't find it, the row
     is UNKNOWN — excluded from both numerator and denominator (n only counts rows
     we could actually check).
  2. Otherwise: count commits that touched the file with an author date AFTER the
     row's ``ts``. Zero such commits → SURVIVED (nothing has overwritten the file
     since llm_edit handed back validated pairs). One or more → NOT SURVIVED.

This is a CONSERVATIVE proxy, not a diff check: a later commit that touches the same
file for an unrelated reason still counts as "not survived", because the ledger does
not store old_string/new_string (only file/model/applied), so there is no way from
here to tell "Claude redid this edit" apart from "someone touched an unrelated line
in the same file later". That means this number can only UNDERSTATE the survival
rate, never overstate it — the honest direction to be wrong in for a North Star
metric. A future ledger version that also stores a content hash of the post-edit
file could resolve this exactly (compare current file content at that hash's
position); until then, treat this as a lower bound.

Rows outside a git repo, or whose file has since been deleted, count as NOT
SURVIVED (an edit that no longer exists anywhere clearly didn't survive).

USAGE
-----
    python3 scripts/northstar/edit_survival.py                     # ~/.llm-router ledger
    python3 scripts/northstar/edit_survival.py --ledger PATH       # explicit ledger file
    python3 scripts/northstar/edit_survival.py --since 2026-09-20  # only rows after this date
    python3 scripts/northstar/edit_survival.py --json              # machine-readable output

Exit code: 0 always (this is a report, not a gate) unless the ledger cannot be read
at all (exit 1) — matches the "confirm the check found something" rule: a ledger
with zero applied=true rows is reported as n=0, not silently treated as 100%.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _default_ledger_path() -> Path:
    home = os.environ.get("LLM_ROUTER_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".llm-router"
    return base / "edit_outcomes.jsonl"


@dataclass
class RowVerdict:
    file: str
    model: str
    ts: float
    verdict: str  # "survived" | "redone" | "unknown"
    detail: str


def _git_root_for(path: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def _commits_touching_since(repo_root: Path, file_path: Path, since_epoch: float) -> int:
    """Count commits touching *file_path* with an author date after *since_epoch*."""
    try:
        rel = file_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return -1  # file isn't inside this repo at all
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_epoch))
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"--since={since_iso}",
             "--pretty=format:%H", "--", str(rel)],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if out.returncode != 0:
        return -1
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    return len(lines)


def judge_row(row: dict) -> RowVerdict:
    file_str = row.get("file", "")
    model = row.get("model", "unknown")
    ts = float(row.get("ts", 0.0))
    path = Path(file_str)

    if not path.exists():
        return RowVerdict(file_str, model, ts, "redone", "file no longer exists")

    root = _git_root_for(path)
    if root is None:
        return RowVerdict(file_str, model, ts, "unknown", "not inside a git work tree")

    n_commits = _commits_touching_since(root, path, ts)
    if n_commits < 0:
        return RowVerdict(file_str, model, ts, "unknown", "git log failed or file outside repo root")
    if n_commits == 0:
        return RowVerdict(file_str, model, ts, "survived", "no commit has touched the file since")
    return RowVerdict(file_str, model, ts, "redone",
                       f"{n_commits} commit(s) touched the file after the edit")


def load_rows(ledger_path: Path, since_epoch: float | None) -> list[dict]:
    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not row.get("applied"):
            continue
        if since_epoch is not None and float(row.get("ts", 0.0)) < since_epoch:
            continue
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, default=None, help="Path to edit_outcomes.jsonl")
    ap.add_argument("--since", type=str, default=None, help="Only rows at/after this date (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    args = ap.parse_args()

    ledger_path = args.ledger or _default_ledger_path()
    since_epoch = None
    if args.since:
        since_epoch = time.mktime(time.strptime(args.since, "%Y-%m-%d"))

    if not ledger_path.exists():
        print(f"No ledger at {ledger_path} — nothing to report (llm_edit hasn't run yet).",
              file=sys.stderr)
        return 1

    applied_rows = load_rows(ledger_path, since_epoch)
    verdicts = [judge_row(r) for r in applied_rows]

    checked = [v for v in verdicts if v.verdict != "unknown"]
    survived = [v for v in checked if v.verdict == "survived"]
    redone = [v for v in checked if v.verdict == "redone"]
    unknown = [v for v in verdicts if v.verdict == "unknown"]

    n = len(checked)
    rate = (len(survived) / n) if n else None

    if args.json:
        print(json.dumps({
            "ledger": str(ledger_path),
            "applied_rows_total": len(applied_rows),
            "checked_n": n,
            "survived": len(survived),
            "redone": len(redone),
            "unknown_excluded": len(unknown),
            "kept_without_redo_rate": rate,
        }, indent=2))
        return 0

    print(f"Ledger: {ledger_path}")
    print(f"applied=true rows: {len(applied_rows)}  (unknown/excluded: {len(unknown)})")
    if n == 0:
        print("n=0 checkable rows — no rate to report (this is not the same as 100%).")
        return 0
    print(f"kept-without-redo rate: {len(survived)}/{n} = {rate:.0%}  (n={n})")
    print()
    print("By model:")
    by_model: dict[str, list[RowVerdict]] = {}
    for v in checked:
        by_model.setdefault(v.model, []).append(v)
    for model, vs in sorted(by_model.items()):
        s = sum(1 for v in vs if v.verdict == "survived")
        print(f"  {model:30s} {s}/{len(vs)} = {s/len(vs):.0%}")
    if redone:
        print()
        print("Redone/lost (first 10):")
        for v in redone[:10]:
            print(f"  {v.file}  [{v.detail}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
