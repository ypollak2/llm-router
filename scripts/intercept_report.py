#!/usr/bin/env python3
"""What did local interception actually save — from observations, not projections.

Reads ~/.llm-router/intercepts.jsonl, one record per intercepted call, written
at the moment of interception with what the call would have cost and what it did.

This exists because the previous compression figure (9.6%, 23,999 tokens) was a
PROJECTION: the compressor's output was measured offline and never reached the
model, so nothing was saved at all. A number computed from a replay is a claim
about a mechanism; a number computed from this log is a claim about what
happened.

    python3 scripts/intercept_report.py [days]
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time
from pathlib import Path


def log_path() -> Path:
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "intercepts.jsonl"


def main() -> int:
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    path = log_path()
    if not path.exists():
        print(f"no interceptions recorded yet ({path})")
        print("enable with image_intercept/bash_intercept in ~/.llm-router/routing.yaml")
        return 1

    cutoff = time.time() - days * 86400
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue          # a torn final line is normal for an append log
        if float(record.get("at", 0)) >= cutoff:
            rows.append(record)

    if not rows:
        print(f"nothing in the last {days:g} day(s)")
        return 0

    by_kind = collections.defaultdict(lambda: [0, 0, 0])
    for r in rows:
        slot = by_kind[r.get("kind", "?")]
        slot[0] += 1
        slot[1] += int(r.get("before_tokens", 0))
        slot[2] += int(r.get("after_tokens", 0))

    print(f"{len(rows)} interceptions in the last {days:g} day(s)\n")
    print(f"{'kind':8s}{'calls':>7s}{'would have cost':>18s}{'did cost':>11s}{'saved':>11s}{'cut':>7s}")
    total_before = total_after = 0
    for kind, (count, before, after) in sorted(by_kind.items()):
        total_before += before
        total_after += after
        cut = (1 - after / before) if before else 0.0
        print(f"{kind:8s}{count:>7d}{before:>18,}{after:>11,}{before - after:>11,}{cut:>7.0%}")
    saved = total_before - total_after
    cut = (1 - total_after / total_before) if total_before else 0.0
    print(f"{'TOTAL':8s}{len(rows):>7d}{total_before:>18,}{total_after:>11,}{saved:>11,}{cut:>7.0%}")

    # Per-day, so a single unusual session cannot look like a trend.
    per_day = collections.defaultdict(int)
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(float(r.get("at", 0))))
        per_day[day] += int(r.get("saved_tokens", 0))
    if len(per_day) > 1:
        print("\nsaved per day:")
        for day, n in sorted(per_day.items()):
            print(f"   {day}  {n:>10,}")

    top = collections.Counter()
    for r in rows:
        top[r.get("detail", "?")[:52]] += int(r.get("saved_tokens", 0))
    print("\nbiggest savers:")
    for detail, n in top.most_common(8):
        print(f"   {n:>9,}  {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
