#!/usr/bin/env python3
"""The routing rate, computed one way, with its denominator shown.

Written after an investigation produced two confidently wrong answers about
"routing stopped working". Neither was a reasoning error; both were denominator
errors:

  * Test-suite invocations write to the production log with
    `session_id=unknown`. On 2026-08-31 that was 1,037 of 1,938 entries — 54% of
    the file. A rate over the raw log is wrong by roughly a factor of two, in
    the direction that looks like a regression.
  * Days with 21-64 prompts produced rates of 2.5%, 1.6%, 0% and 4.3%. All four
    were noise and all four were briefly reported as a collapse.

So this prints n beside every rate, refuses to headline a small sample, and
breaks the result down by skip reason — because the third finding was that the
rate is a property of the WORKLOAD, not of the router. Release/CI days route at
~62% and ordinary code days at 78-96%, with no code change between them.

    python3 scripts/routing_rate.py                # per day
    python3 scripts/routing_rate.py --since 2026-09-01
    python3 scripts/routing_rate.py --file fixture.log
"""
from __future__ import annotations

import argparse
import collections
import os
import re
from pathlib import Path

MIN_SAMPLE = 50   # below this, report "too few to tell" rather than a number

_START = re.compile(r"\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION START\] ID=([\d.]+)")
_LINE = re.compile(r"\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION ([\d.]+)\] (.*)")


def default_log() -> Path:
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "auto-route-debug.log"


def load(path: Path) -> dict[str, dict]:
    inv: dict[str, dict] = {}
    for line in path.read_text(errors="replace").splitlines():
        m = _LINE.match(line)
        if not m:
            m2 = _START.match(line)
            if m2:
                inv.setdefault(m2.group(2), {"day": m2.group(1), "msgs": []})
            continue
        day, iid, rest = m.groups()
        rec = inv.setdefault(iid, {"day": day, "msgs": []})
        rec["msgs"].append(rest)
        sid = re.search(r"session_id=(\S+)", rest)
        if sid:
            rec["sid"] = sid.group(1)
    return inv


def is_real(rec: dict) -> bool:
    """A prompt from a real session — the only thing a rate may be computed over.

    `session_id=unknown` is the test suite. Counting it is the single mistake
    this script exists to prevent.
    """
    return (any(m.startswith("prompt_len=") for m in rec["msgs"])
            and rec.get("sid") not in (None, "unknown"))


def outcome(rec: dict) -> str:
    for m in rec["msgs"]:
        if m.startswith("DIRECT:"):
            return "reached"
        if "DIRECT SKIP:" in m:
            return "skip: " + m.split("DIRECT SKIP:")[1].strip()[:34]
        if "BYPASS" in m:
            return "bypass"
        if "CONTINUATION" in m:
            return "continuation"
    return "UNLOGGED — a prompt whose fate was never recorded"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--since")
    ap.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    args = ap.parse_args()

    path = Path(args.file).expanduser() if args.file else default_log()
    if not path.exists():
        print(f"no log at {path}")
        return 1

    inv = load(path)
    real = [r for r in inv.values() if is_real(r)]
    skipped_tests = sum(1 for r in inv.values()
                        if any(m.startswith("prompt_len=") for m in r["msgs"])) - len(real)

    by_day: dict[str, list] = collections.defaultdict(list)
    for r in real:
        if args.since and r["day"] < args.since:
            continue
        by_day[r["day"]].append(r)

    print(f"log: {path}")
    print(f"excluded {skipped_tests:,} test-suite prompts (session_id absent or 'unknown')\n")
    print(f"{'day':12s} {'real prompts':>13s} {'succeeded':>10s} {'rate':>18s}")
    print("-" * 58)
    for day in sorted(by_day):
        rows = by_day[day]
        n = len(rows)
        ok = sum(1 for r in rows if any("DIRECT SUCCESS" in m for m in r["msgs"]))
        if n < args.min_sample:
            print(f"{day:12s} {n:13d} {ok:10d} {'too few to tell':>18s}")
        else:
            print(f"{day:12s} {n:13d} {ok:10d} {100 * ok / n:17.1f}%")

    tot = [r for rows in by_day.values() for r in rows]
    if not tot:
        print("\nno real-session prompts in range")
        return 0
    ok = sum(1 for r in tot if any("DIRECT SUCCESS" in m for m in r["msgs"]))
    print(f"\noverall: {ok}/{len(tot)} = {100 * ok / len(tot):.1f}%")

    print("\nwhy the rest did not route — a workload shift shows up HERE,")
    print("and a workload shift is not a regression:")
    reasons = collections.Counter(outcome(r) for r in tot if
                                  not any("DIRECT SUCCESS" in m for m in r["msgs"]))
    for reason, count in reasons.most_common(8):
        print(f"   {100 * count / len(tot):5.1f}%  {reason}")

    unlogged = sum(v for k, v in reasons.items() if k.startswith("UNLOGGED"))
    if unlogged:
        print(f"\n⚠  {unlogged} prompt(s) had NO recorded outcome. That is a bug in the")
        print("   hook, not a routing decision — see tests/test_routing_outcome_logged.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
