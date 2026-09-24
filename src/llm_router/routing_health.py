"""`llm-router routing-health` — is local routing doing any work? One screen, with n.

Written 2026-09-24 because the existing report answers a different question:
`routing-report` counts drafts PRODUCED ("DIRECT SUCCESS 63%") and an
estimated saving, while 0 of 1,191 drafts had ever been used. The numbers that
matter, per day, over prompts a person typed (routing_log.is_human):

  reach    a local draft was produced
  context  that draft read repo files first (read-only loop, files_read > 0)
  USED     Claude relayed the draft (hooks/draft_usage.py verdicts) — the one
           that pays; everything else is a cost
  latency  median time a draft added before Claude saw the prompt

Below MIN_SAMPLE prompts a rate prints as "too few" — days of 21-64 prompts
have been misread as collapses before.
"""
from __future__ import annotations

import collections
import datetime as _dt
import json
import re
import statistics
import sys
from pathlib import Path

from llm_router.routing_log import MIN_SAMPLE, default_log, is_human, is_real, load

_LATENCY = re.compile(r"latency=(\d+)ms")
_FILES = re.compile(r"files_read=(\d+)")


def summarize(log: Path | None = None, days: int = 7,
              today: _dt.date | None = None) -> dict[str, dict]:
    """Per-day counters over the last *days* days (inclusive of today)."""
    path = log or default_log()
    if not path.exists():
        return {}
    since = ((today or _dt.date.today()) - _dt.timedelta(days=days - 1)).isoformat()
    out: dict[str, dict] = collections.defaultdict(lambda: {
        "prompts": 0, "drafted": 0, "read_files": 0, "used": 0, "judged": 0,
        "latencies": []})
    for rec in load(path).values():
        day = rec["day"]
        if day < since:
            continue
        msgs = rec["msgs"]
        # Verdicts are logged one invocation late, on the next prompt of the same
        # session — count them wherever they land, from any real session.
        if is_real(rec):
            for m in msgs:
                if m.startswith("DRAFT USED:"):
                    out[day]["used"] += 1
                    out[day]["judged"] += 1
                elif m.startswith("DRAFT UNUSED:"):
                    out[day]["judged"] += 1
        if not is_human(rec):
            continue
        row = out[day]
        row["prompts"] += 1
        success = next((m for m in msgs if m.startswith("DIRECT SUCCESS:")), None)
        if success:
            row["drafted"] += 1
            fr = _FILES.search(success)
            if fr and int(fr.group(1)) > 0:
                row["read_files"] += 1
            lat = _LATENCY.search(success)
            if lat:
                row["latencies"].append(int(lat.group(1)))
    return dict(out)


def _rate(k: int, n: int, min_n: int) -> str:
    if n == 0:
        return "—"
    return f"{k}/{n}" + (" (too few)" if n < min_n else f" {100 * k / n:.0f}%")


def _streak() -> str:
    try:
        from llm_router.hooks import draft_usage
        n = draft_usage.unused_streak()
        cut = draft_usage.drafting_reverted()
        return f"{n} unused in a row" + (" — DRAFTING AUTO-REVERTED" if cut else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    days = 7
    if "--days" in argv:
        try:
            days = max(1, int(argv[argv.index("--days") + 1]))
        except (IndexError, ValueError):
            print("usage: llm-router routing-health [--days N] [--json]")
            return 2
    rows = summarize(days=days)
    if "--json" in argv:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("no routing log yet — nothing to report")
        return 0
    print(f"routing health — last {days} day(s), prompts you typed (log: {default_log()})\n")
    print(f"{'day':10s} {'prompts':>7s}  {'reach (drafted)':>17s}  {'context (read files)':>21s}"
          f"  {'USED by Claude':>16s}  {'draft p50':>9s}")
    tot = collections.Counter()
    lats: list[int] = []
    for day in sorted(rows):
        r = rows[day]
        p50 = f"{statistics.median(r['latencies']) / 1000:.1f}s" if r["latencies"] else "—"
        print(f"{day:10s} {r['prompts']:7d}  {_rate(r['drafted'], r['prompts'], MIN_SAMPLE):>17s}  "
              f"{_rate(r['read_files'], r['drafted'], 1):>21s}  "
              f"{_rate(r['used'], r['judged'], 1):>16s}  {p50:>9s}")
        for k in ("prompts", "drafted", "read_files", "used", "judged"):
            tot[k] += r[k]
        lats += r["latencies"]
    p50 = f"{statistics.median(lats) / 1000:.1f}s" if lats else "—"
    print(f"{'total':10s} {tot['prompts']:7d}  {_rate(tot['drafted'], tot['prompts'], MIN_SAMPLE):>17s}  "
          f"{_rate(tot['read_files'], tot['drafted'], 1):>21s}  "
          f"{_rate(tot['used'], tot['judged'], 1):>16s}  {p50:>9s}")
    print(f"\nauto-revert: {_streak()}")
    print("USED is what saves Claude quota; reach without use is only added latency.")
    return 0
