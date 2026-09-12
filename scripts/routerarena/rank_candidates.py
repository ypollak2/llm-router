#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""G1.4 + G1.5 — filter the screened models for cost-predictability, then rank them.

Our Arena score *is* the best constant. Constants are also the only thing that transfers
between our corpus and RouterArena (Spearman +0.800, against +0.561 for policies), so a better
constant is the one improvement that can be trusted to survive the trip.

Three things this reports that a naive ranking would not:

* **Completion.** A model that errored on a third of its calls has not been measured, it has
  been sampled. Scoring it low would confuse a provider outage with model weakness, which is
  exactly the mistake C7 caught (`gpt-oss` 0.23 -> 0.69 once broken calls stopped counting as
  wrong answers). Below the completion floor a model is reported **unmeasured**, not ranked.
* **Cost predictability.** E2.1's criterion: a model that ran over its output cap, or finished
  exactly on it, gave us a lower bound rather than a measurement, and `accuracy - lambda*cost`
  always prefers the model whose bill has not arrived.
* **A band, not a point.** Bootstrap over the item set. A 1.5-point accuracy margin from 1,560
  items has real sampling error, and the whole reason Track E failed was acting on differences
  smaller than their noise.

Usage::

    RA_OUTCOMES=g1_screen.jsonl python scripts/routerarena/rank_candidates.py --boots 30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cost_predictability  # noqa: E402

OUTCOMES = REPO / "data" / "outcomes" / os.environ.get("RA_OUTCOMES", "g1_screen.jsonl")
BETA, C_MAX, C_MIN = 0.1, 200.0, 0.0044

# The incumbent's sealed-half performance, the number any candidate has to beat.
INCUMBENT = "google/gemini-3.1-flash-lite-preview"
INCUMBENT_SEALED_ACC, INCUMBENT_SEALED_COST = 0.7553, 0.0644


def arena(accuracy: float, cost_per_1k: float) -> float:
    c = max(C_MIN, min(cost_per_1k, C_MAX))
    ci = (math.log2(C_MAX) - math.log2(c)) / (math.log2(C_MAX) - math.log2(C_MIN))
    return ((1 + BETA) * accuracy * ci) / (BETA * accuracy + ci)


def load() -> tuple[dict, dict, dict]:
    acc: dict[str, dict[str, float]] = defaultdict(dict)
    cost: dict[str, dict[str, float]] = defaultdict(dict)
    errors: dict[str, int] = defaultdict(int)
    calls: dict[str, int] = defaultdict(int)
    for line in OUTCOMES.open():
        r = json.loads(line)
        calls[r["model"]] += 1
        if r.get("error"):
            errors[r["model"]] += 1
            continue
        acc[r["model"]][r["prompt_hash"]] = r["correct"]
        cost[r["model"]][r["prompt_hash"]] = r["cost_usd"]
    return acc, cost, {m: (calls[m] - errors[m]) / max(calls[m], 1) for m in calls}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--boots", type=int, default=30)
    ap.add_argument("--min-completion", type=float, default=0.90)
    args = ap.parse_args()

    acc, cost, completion = load()
    models = sorted(acc)
    print(f"{len(models)} models in {OUTCOMES.name}\n")

    # ---- G1.4: completion floor, then cost-predictability ------------------------------
    unmeasured = [m for m in models if completion[m] < args.min_completion]
    if unmeasured:
        print("UNMEASURED -- completion below floor, reported rather than ranked:")
        for m in unmeasured:
            print(f"  {m:<48}{completion[m] * 100:>6.1f}% of calls returned")
        print()

    ranked_pool = [m for m in models if completion[m] >= args.min_completion]
    predictable = set(cost_predictability.candidate_models(restrict_to=ranked_pool))
    excluded = [m for m in ranked_pool if m not in predictable]
    if excluded:
        print("EXCLUDED -- cost is a lower bound, not a measurement (E2.1 criterion):")
        for m in excluded:
            print(f"  {m}")
        print()
    ranked_pool = [m for m in ranked_pool if m in predictable]

    # ---- G1.5: rank on Arena, with a bootstrap band -------------------------------------
    # Common item set, so every model is scored on the same questions. A ragged comparison
    # measures which items each model happened to get, not which model is better.
    common = set.intersection(*[set(acc[m]) for m in ranked_pool]) if ranked_pool else set()
    items = sorted(common)
    print(f"ranking {len(ranked_pool)} models on {len(items)} commonly-covered items, "
          f"{args.boots} bootstrap resamples\n")

    rows = []
    for m in ranked_pool:
        a = statistics.mean(acc[m][h] for h in items)
        c = statistics.mean(cost[m][h] for h in items) * 1000
        boots = []
        for b in range(args.boots):
            rng = random.Random(b)
            sample = [items[rng.randrange(len(items))] for _ in range(len(items))]
            ba = statistics.mean(acc[m][h] for h in sample)
            bc = statistics.mean(cost[m][h] for h in sample) * 1000
            boots.append(arena(ba, bc) * 100)
        rows.append({
            "model": m, "acc": a, "cost_1k": c, "arena": arena(a, c) * 100,
            "lo": min(boots), "hi": max(boots),
            "acc_boots": sorted(boots),
        })
    rows.sort(key=lambda r: -r["arena"])

    print(f"{'model':<46}{'acc':>8}{'$/1k':>10}{'arena':>8}{'band':>16}")
    for r in rows:
        tag = "  <-- incumbent" if r["model"] == INCUMBENT else ""
        band = f"{r['lo']:.1f}-{r['hi']:.1f}"
        print(f"{r['model']:<46}{r['acc'] * 100:>7.2f}%{r['cost_1k']:>10.4f}"
              f"{r['arena']:>8.2f}{band:>16}{tag}")

    # ---- the G1 decision ----------------------------------------------------------------
    inc = next((r for r in rows if r["model"] == INCUMBENT), None)
    print(f"\nincumbent on the SEALED half: {INCUMBENT_SEALED_ACC * 100:.2f}% @ "
          f"${INCUMBENT_SEALED_COST:.4f}/1k -> {arena(INCUMBENT_SEALED_ACC, INCUMBENT_SEALED_COST) * 100:.2f}")
    if inc:
        print(f"incumbent on THIS screen:     {inc['acc'] * 100:.2f}% @ ${inc['cost_1k']:.4f}/1k"
              f" -> {inc['arena']:.2f}   (the in-run control)")

    if inc is None:
        print("\nno incumbent control in this screen -- comparison is uncontrolled")
        return 1

    winners = [
        r for r in rows
        if r["model"] != INCUMBENT
        and (r["acc"] - inc["acc"]) * 100 >= 1.5
        and r["cost_1k"] <= inc["cost_1k"]
        and r["lo"] > inc["arena"]          # band must not cross the incumbent
    ]
    print("\nGATE: >=1 model beats the incumbent by >=1.5 accuracy points at "
          "equal-or-lower cost, band clear of the incumbent")
    if winners:
        print(f"  PASS -- {len(winners)} candidate(s):")
        for r in winners:
            print(f"    {r['model']:<44}{(r['acc'] - inc['acc']) * 100:>+7.2f} acc pts"
                  f"{r['arena'] - inc['arena']:>+8.2f} arena")
    else:
        best = max((r for r in rows if r["model"] != INCUMBENT),
                   key=lambda r: r["arena"], default=None)
        print("  FAIL -- no candidate clears every condition.")
        if best:
            print(f"  best non-incumbent: {best['model']} "
                  f"({best['acc'] * 100:.2f}%, ${best['cost_1k']:.4f}, arena {best['arena']:.2f}, "
                  f"band {best['lo']:.1f}-{best['hi']:.1f})")
        print("  The pool being exhausted at this price point is a result, not a failure to "
              "report. Continue to G2.")

    out = REPO / "data" / "policy" / "screen_ranking.json"
    out.write_text(json.dumps(
        {"ranking": [{k: v for k, v in r.items() if k != "acc_boots"} for r in rows],
         "unmeasured": unmeasured, "cost_excluded": excluded,
         "n_items": len(items), "gate_passed": bool(winners),
         "winners": [r["model"] for r in winners]}, indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
