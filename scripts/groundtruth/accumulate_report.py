#!/usr/bin/env python3
"""Accumulation coverage: what is in the pool, and why things are not.

    python3 scripts/groundtruth/accumulate_report.py

A CLI, not a dashboard. It answers the questions that decide whether to keep
accumulating or to change what is captured:

  * how many candidates, and how far along the lifecycle?
  * why are tasks being rejected?
  * which task types are underrepresented?
  * how many would survive deduplication and human review?
  * is the pool representative of real traffic?

The last one matters most and is the easiest to get wrong. Reaching 500
candidates that are all the same easy task type is worse than 100 mixed ones,
because the first looks like progress.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth import pool as poolmod  # noqa: E402
from groundtruth import sampling as sp  # noqa: E402

# Below this, a per-category number is noise. Matches the project's existing
# "too few to tell" threshold for routing rates.
MIN_PER_CATEGORY = 10
# A rough target for Ground Truth v1; used only to show distance, never to
# justify freezing early.
DEFAULT_TARGET = 400


def _runtime_outcomes() -> dict:
    """Tallies from the accumulation outcome log written on the routing path."""
    try:
        from llm_router.prompt_capture import outcome_log_path
    except Exception:  # noqa: BLE001
        return {}
    path = outcome_log_path()
    if not path.exists():
        return {}
    counts: Counter[str] = Counter()
    try:
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            counts[json.loads(line).get("outcome", "unknown")] += 1
    except Exception:  # noqa: BLE001
        return {}
    d = dict(counts)
    d["total"] = sum(counts.values())
    return d


def _bar(n: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    filled = int(round(width * n / total))
    return "█" * filled + "·" * (width - filled)


def representativeness(stats: dict, traffic: dict[str, int] | None) -> list[str]:
    """Compare the pool's task mix against real traffic, if known.

    Without a traffic reference this says so rather than inventing a baseline.
    """
    pool_mix = stats.get("by_task_type", {})
    if not traffic:
        return ["  (no traffic reference supplied — pass --traffic to compare)"]
    total_pool = sum(pool_mix.values()) or 1
    total_traffic = sum(traffic.values()) or 1
    lines = [f"  {'task type':14s} {'pool':>6s} {'pool%':>7s} {'traffic%':>9s} {'ratio':>7s}"]
    for t in sorted(set(pool_mix) | set(traffic)):
        p = pool_mix.get(t, 0) / total_pool
        w = traffic.get(t, 0) / total_traffic
        ratio = (p / w) if w else None
        flag = ""
        if ratio is not None and (ratio < 0.5 or ratio > 2.0):
            flag = "  <-- skewed"
        lines.append(f"  {t:14s} {pool_mix.get(t,0):6d} {p:6.1%} {w:8.1%} "
                     f"{(f'{ratio:.2f}' if ratio is not None else '  n/a'):>7s}{flag}")
    return lines


def render(p: poolmod.Pool, *, traffic: dict[str, int] | None = None,
           target: int = DEFAULT_TARGET) -> str:
    s = p.stats()
    out: list[str] = []
    out.append("Ground Truth accumulation")
    out.append("=" * 58)
    out.append(f"  pool file   {p.path}")
    out.append(f"  funnel      {p.funnel_path}")
    out.append("")
    # The runtime funnel, in the order a task moves through it. `Errors` comes
    # from the accumulation outcome log rather than the pool: an accumulation
    # that threw never reached the pool, so the pool cannot count it, and that
    # is exactly the failure worth seeing.
    rt = _runtime_outcomes()
    out.append(f"Captured:        {rt.get('total', s['captured'])}")
    out.append(f"Eligible:        {s['eligible']}")
    out.append(f"Persisted:       {rt.get('persisted', s['eligible'])}")
    out.append(f"Rejected:        {rt.get('rejected', sum(s['rejected'].values()))}")
    out.append(f"Deduplicated:    {rt.get('deduplicated', 0)}")
    out.append(f"Errors:          {rt.get('error', 0)}"
               + ("   <-- investigate" if rt.get("error") else ""))
    out.append(f"Verifier-ready:  {s['high_confidence']}")
    out.append("")
    out.append(f"  Replay-ready:            {s['replay_ready']}")
    out.append(f"  Verifier candidate:      {s['verifier_candidate']}")
    out.append(f"  Needs human review:      {s['needs_human_review']}")
    out.append("")

    if s["by_state"]:
        out.append("Lifecycle:")
        total = s["captured"]
        for st, n in sorted(s["by_state"].items(), key=lambda kv: -kv[1]):
            out.append(f"  {st:26s} {n:5d}  {_bar(n, total)}")
        out.append("")

    if s["rejected"]:
        out.append("Rejected:")
        for r, n in sorted(s["rejected"].items(), key=lambda kv: -kv[1]):
            out.append(f"  {r:38s} {n:5d}")
        out.append("")

    if s["by_task_type"]:
        out.append("Task-type distribution (non-rejected):")
        thin = []
        for t, n in sorted(s["by_task_type"].items(), key=lambda kv: -kv[1]):
            mark = "" if n >= MIN_PER_CATEGORY else f"  <-- under {MIN_PER_CATEGORY}"
            if n < MIN_PER_CATEGORY:
                thin.append(t)
            out.append(f"  {t:14s} {n:5d}{mark}")
        if thin:
            out.append(f"  underrepresented: {', '.join(thin)}")
        out.append("")

    if s["by_verifier_class"]:
        out.append("Credible verifier path:")
        for v, n in sorted(s["by_verifier_class"].items(), key=lambda kv: -kv[1]):
            out.append(f"  {v:22s} {n:5d}")
        out.append("")

    out.append("Representativeness:")
    out.extend(representativeness(s, traffic))
    out.append("")

    hi = s["high_confidence"]
    out.append("Readiness for Ground Truth v1:")
    out.append(f"  high-confidence candidates {hi} / {target} target  {_bar(hi, target)}")
    if hi < MIN_PER_CATEGORY:
        out.append("  VERDICT: far too few to freeze. Keep accumulating.")
    elif hi < target:
        out.append(f"  VERDICT: not yet. {target - hi} more high-confidence candidates, "
                   "and check the type mix above before freezing.")
    else:
        out.append("  VERDICT: target reached by COUNT. Before freezing, confirm the "
                   "type mix is not dominated by one easy category.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=Path, default=None)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET)
    ap.add_argument("--traffic", type=Path, default=None,
                    help="JSON {task_type: count} of real traffic, to compare against")
    ap.add_argument("--sampling", action="store_true",
                    help="also show a stratified sample plan over the pool")
    args = ap.parse_args()

    p = poolmod.Pool(args.pool)
    traffic = json.loads(args.traffic.read_text()) if args.traffic else None
    print(render(p, traffic=traffic, target=args.target))

    if args.sampling:
        ready = p.in_state(poolmod.READY_FOR_REPLAY, poolmod.VERIFIED)
        if not ready:
            print("\n(no replay-ready candidates to plan a sample over)")
            return 0

        class _U:
            def __init__(self, c):
                self.task_type = c.task_type
                self.complexity = c.complexity
                self.route_kind = "completion"
                self.tool_execution_attempted = False

        plan = sp.plan_sample([_U(c) for c in ready],
                              target_n=min(args.target, len(ready)))
        print("\n" + sp.describe(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
