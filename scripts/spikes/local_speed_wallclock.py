"""SPIKE (2026-09-28): reconstruct wall clock for the 6 golden tasks under a
tested lever, by replaying the 2026-09-28 per-call-proxy session timeline
and substituting this lever's measured latency for each of the 19 calls
that were originally served locally. Every Anthropic-forwarded call (first
call of each task + the 5 calls the classifier kept on Claude) keeps its
ORIGINAL recorded latency -- those are untouched by a local-speed lever.

This is a RECONSTRUCTION from real per-call timestamps, not a fresh live
run (see local_speed_replay.py's module docstring for why a live run is not
possible here). A hedged-to-fallback call is charged: time spent waiting
for the hedge deadline + the median observed latency of an Anthropic
continuation call (computed from this session's own 5 kept-on-Claude
calls), since a hedge miss falls back to Claude.

Run:
  python3 scripts/spikes/local_speed_wallclock.py --cases-log <f>/cases.jsonl \
      --ref-log <f>/routed.jsonl --lever-log <out>.jsonl
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-log", required=True)
    ap.add_argument("--ref-log", required=True)
    ap.add_argument("--lever-log", required=True)
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(args.cases_log)]
    windows = {}
    for c in cases:
        if c.get("tag") != "routed":
            continue
        cid = c["case"]
        if "start" in c:
            windows.setdefault(cid, {})["start"] = c["start"]
        if "end" in c:
            windows.setdefault(cid, {})["end"] = c["end"]

    ref = [json.loads(l) for l in open(args.ref_log) if json.loads(l).get("path") == "/v1/messages"]
    lever = {json.loads(l)["dump"]: json.loads(l) for l in open(args.lever_log)}  # keyed by basename

    anthropic_continuation_lat = [r["route"]["latency_s"] for r in ref
                                   if r.get("route") and r["route"]["outcome"] != "served"
                                   and "latency_s" in r["route"]]
    anthropic_fallback_lat = (statistics.median(anthropic_continuation_lat)
                               if anthropic_continuation_lat else 2.2)

    per_case = {}
    for r in ref:
        ts = r["ts"]
        cid = next((c for c, w in windows.items()
                    if w.get("start", 0) <= ts <= w.get("end", 1e18)), None)
        if cid is None:
            continue
        if r.get("route") and r["route"]["outcome"] == "served":
            lr = lever.get(Path(r.get("dump", "")).name)
            if lr is None:
                lat = r["route"]["latency_s"]  # no replay result: keep original (should not happen)
                note = "MISSING-REPLAY-fallback-to-original"
            elif lr.get("err") == "hedge_timeout":
                lat = lr["total_s"] + anthropic_fallback_lat
                note = "hedged->anthropic"
            elif not lr.get("valid", True):
                lat = lr["total_s"] + anthropic_fallback_lat
                note = "invalid->anthropic-fallback"
            else:
                lat = lr["total_s"]
                note = "local"
        else:
            lat = r.get("latency_s", r.get("route", {}).get("latency_s", 0))
            note = "anthropic-original"
        per_case.setdefault(cid, []).append((lat, note))

    total = 0.0
    for cid in sorted(per_case):
        calls = per_case[cid]
        s = sum(x[0] for x in calls)
        total += s
        print(f"{cid}: {s:.1f}s over {len(calls)} calls "
              f"({sum(1 for _,n in calls if n=='local')} local, "
              f"{sum(1 for _,n in calls if 'hedge' in n or 'fallback' in n)} hedged/invalid)")
    print(f"TOTAL wall-clock estimate (6 tasks): {total:.1f}s "
          f"(anthropic continuation-call median used for fallbacks: {anthropic_fallback_lat:.2f}s, n={len(anthropic_continuation_lat)})")


if __name__ == "__main__":
    main()
