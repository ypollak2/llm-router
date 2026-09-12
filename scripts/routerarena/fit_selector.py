#!/usr/bin/env python3
"""Fit a routing policy on the external outcome table, and evaluate it honestly.

Fitting uses **only** ``data/outcomes/*.jsonl`` -- outcomes measured by us, on the audited
external corpus. RouterArena contributes nothing to any parameter here.

Everything is scored on a held-out split, repeated over many random seeds. Track A established
why: fit a per-family model map on RouterArena's own data and evaluate it in-sample and it
looks worth +3.94 Arena points; hold it out and it is worth -0.76. In-sample numbers from this
kind of table are not weak evidence, they are actively misleading.

The decision rule is the Lagrangian from the plan: pick the action maximising
``quality - lambda * cost``, with lambda swept rather than assumed.

Usage::

    python scripts/routerarena/fit_selector.py --outcomes data/outcomes/cheap_tier.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

BETA, C_MAX, C_MIN = 0.1, 200.0, 0.0044


def arena_score(cost_per_1k: float, accuracy: float) -> float:
    """RouterArena's Acc-Cost Arena score, so external results are quoted in the same unit."""
    cost_per_1k = max(cost_per_1k, 1e-6)
    c_i = (math.log2(C_MAX) - math.log2(cost_per_1k)) / (math.log2(C_MAX) - math.log2(C_MIN))
    return ((1 + BETA) * accuracy * c_i) / (BETA * accuracy + c_i)


def load(paths: list[Path]) -> tuple[list[str], list[str], dict[str, dict[str, dict]]]:
    """Return (items, models, table[item][model] -> {correct, cost}).

    Only items every model was actually run on are kept -- a ragged table makes model
    comparisons reflect which items each model happened to get, which is the Simpson's-paradox
    trap that produced the plan's false "+4.40 from dropping a dominated model".
    """
    table: dict[str, dict[str, dict]] = defaultdict(dict)
    cluster: dict[str, str] = {}
    provisional: set[str] = set()
    for path in paths:
        for line in path.open():
            r = json.loads(line)
            if r.get("error"):
                continue
            table[r["prompt_hash"]][r["model"]] = {
                "correct": r["correct"], "cost": r["cost_usd"]
            }
            cluster[r["prompt_hash"]] = r["cluster"]
            if r.get("provisional"):
                provisional.add(r["cluster"])

    models = sorted({m for cells in table.values() for m in cells})
    items = sorted(h for h, cells in table.items() if len(cells) == len(models))
    return items, models, {"table": table, "cluster": cluster, "provisional": provisional}


def evaluate(items: list[str], models: list[str], meta: dict, choose) -> tuple[float, float]:
    """Mean accuracy and cost per 1K queries for a policy over the given items."""
    acc = cost = 0.0
    for h in items:
        cell = meta["table"][h][choose(h)]
        acc += cell["correct"]
        cost += cell["cost"]
    n = max(len(items), 1)
    return acc / n, cost / n * 1000


def fit_cluster_policy(
    train: list[str], models: list[str], meta: dict, lam: float, min_n: int
) -> dict[str, str]:
    """Per-cluster argmax of (accuracy - lambda * cost_per_1k), on training items only.

    ``min_n`` is the density floor Track A's learning curve produced: a cluster with too few
    training items has a best-model estimate that is mostly noise, and acting on it is worse
    than not routing at all. Those clusters fall back to the global pick.
    """
    by_cluster: dict[str, list[str]] = defaultdict(list)
    for h in train:
        by_cluster[meta["cluster"][h]].append(h)

    def best_over(hs: list[str]) -> str:
        scores = {}
        for m in models:
            a = sum(meta["table"][h][m]["correct"] for h in hs) / len(hs)
            c = sum(meta["table"][h][m]["cost"] for h in hs) / len(hs) * 1000
            scores[m] = a - lam * c
        return max(scores, key=scores.get)

    global_best = best_over(train)
    return {
        cl: (best_over(hs) if len(hs) >= min_n else global_best)
        for cl, hs in by_cluster.items()
    }, global_best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outcomes", type=Path, nargs="+",
                    default=[REPO / "data" / "outcomes" / "cheap_tier.jsonl"])
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--train-frac", type=float, default=0.5)
    ap.add_argument("--min-n", type=int, default=25,
                    help="per-cluster training-item floor below which we fall back to global")
    ap.add_argument("--exclude-provisional", action="store_true",
                    help="drop clusters whose grader is only a shape check (code)")
    args = ap.parse_args()

    items, models, meta = load(args.outcomes)
    if not items:
        print("no items with complete model coverage -- is the sweep still running?")
        return 1

    if args.exclude_provisional:
        items = [h for h in items if meta["cluster"][h] not in meta["provisional"]]

    clusters = sorted({meta["cluster"][h] for h in items})
    print(f"{len(items)} items x {len(models)} models across {len(clusters)} clusters")
    if meta["provisional"]:
        print(f"provisional graders (shape-check only): {sorted(meta['provisional'])}")
    print()

    # ---- constants and hindsight bounds, on the whole set -------------------------------
    print(f"{'policy':<44}{'acc':>8}{'$/1k':>10}{'arena':>8}")
    for m in models:
        a, c = evaluate(items, models, meta, lambda _h, m=m: m)
        print(f"  always {m.split('/')[-1]:<36}{a:>8.4f}{c:>10.4f}{arena_score(c, a) * 100:>8.2f}")

    def oracle(h: str) -> str:
        ok = {m: v for m, v in meta["table"][h].items() if v["correct"] > 0}
        pool = ok or meta["table"][h]
        return min(pool, key=lambda m: pool[m]["cost"])

    a, c = evaluate(items, models, meta, oracle)
    print(f"  {'oracle: cheapest correct':<42}{a:>8.4f}{c:>10.4f}{arena_score(c, a) * 100:>8.2f}")

    # ---- the real test: held-out cluster routing, lambda swept --------------------------
    print(f"\nHELD-OUT cluster routing ({args.seeds} random "
          f"{args.train_frac:.0%}/{1 - args.train_frac:.0%} splits, min_n={args.min_n}):")
    print(f"{'lambda':>10}{'acc':>9}{'$/1k':>10}{'arena':>9}{'band':>18}")

    best_row = None
    for lam in (0.0, 0.02, 0.05, 0.11, 0.25, 0.5, 1.0, 2.0):
        rows = []
        for seed in range(args.seeds):
            rng = random.Random(seed)
            shuf = items[:]
            rng.shuffle(shuf)
            k = int(len(shuf) * args.train_frac)
            train, test = shuf[:k], shuf[k:]
            policy, fallback = fit_cluster_policy(train, models, meta, lam, args.min_n)
            a, c = evaluate(test, models, meta,
                            lambda h: policy.get(meta["cluster"][h], fallback))
            rows.append((a, c, arena_score(c, a) * 100))
        acc = statistics.mean(r[0] for r in rows)
        cost = statistics.mean(r[1] for r in rows)
        arena = statistics.mean(r[2] for r in rows)
        lo, hi = min(r[2] for r in rows), max(r[2] for r in rows)
        print(f"{lam:>10}{acc:>9.4f}{cost:>10.4f}{arena:>9.2f}{f'{lo:.2f}-{hi:.2f}':>18}")
        if best_row is None or arena > best_row[1]:
            best_row = (lam, arena, acc, cost)

    # ---- the honest comparison ----------------------------------------------------------
    # The bar is the best CONSTANT evaluated the same way. A router that cannot beat "always
    # use one model" is not adding anything, however sophisticated its internals.
    print(f"\nheld-out best-constant bar ({args.seeds} splits):")
    const_rows = []
    for m in models:
        vals = []
        for seed in range(args.seeds):
            rng = random.Random(seed)
            shuf = items[:]
            rng.shuffle(shuf)
            test = shuf[int(len(shuf) * args.train_frac):]
            a, c = evaluate(test, models, meta, lambda _h, m=m: m)
            vals.append(arena_score(c, a) * 100)
        const_rows.append((statistics.mean(vals), m))
    const_rows.sort(reverse=True)
    for v, m in const_rows[:3]:
        print(f"  {m:<44}{v:>8.2f}")

    bar = const_rows[0][0]
    lam, arena, acc, cost = best_row
    delta = arena - bar
    print(f"\nGATE D: cluster routing {arena:.2f} vs best constant {bar:.2f} "
          f"-> {delta:+.2f} Arena points (lambda={lam})")
    print("  PASS -- routing earns its keep on external data" if delta > 0
          else "  FAIL -- a constant policy is as good; the mechanism does not transfer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
