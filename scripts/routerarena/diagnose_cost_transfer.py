#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt diagnostic only -- writes no policy artifact, only a report
"""E1.2 — re-run the Gate D holdout with RouterArena-realised prices substituted.

Gate D said cluster routing was worth **+2.67 Arena points** held out over 30 splits. sub_10
said **−2.63**. This asks the narrow question: was the cost axis the reason?

E1.1 established that it was distorted, and distorted *unevenly*. Our external labelling ran
with reasoning disabled and tight output caps -- the fix that made the sweep affordable
($30.73 → $2.32) -- while RouterArena's harness lets reasoning run. So the models that emit
reasoning tokens were priced as if they did not:

    qwen3.5-flash   $0.0152/1k as fit   ->  $1.1938/1k realised   (78.5x)
    qwen3.5-9b      $0.0109/1k as fit   ->  $0.8753/1k realised   (80.2x)
    gemini-lite     $0.0206/1k as fit   ->  $0.0636/1k realised    (3.1x)

The cost *ordering* the Lagrangian compares has Spearman rho = -0.048 against the real one --
uncorrelated. `argmax(accuracy - lambda * cost)` was reading a cost axis that was noise, and it
duly picked the model that looked nearly free and scored a hair higher.

**This script is a diagnostic and its output must never be fit on.** It substitutes
RouterArena-realised per-model costs into the external outcome table to ask "would we have
made the same choice knowing the real prices?". Substituting them into a *shipped* fit would be
fitting to the benchmark. The shipped repair (E3) rebuilds costs from the published price list
plus our own measured token counts instead.

Usage::

    python scripts/routerarena/diagnose_cost_transfer.py --seeds 30
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "routerarena"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fs = _load("fit_selector", REPO / "scripts" / "routerarena" / "fit_selector.py")
replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")


def realised_cost_per_query() -> dict[str, float]:
    """Mean per-query cost each model actually incurred inside RouterArena's harness."""
    by_model: dict[str, list[float]] = collections.defaultdict(list)
    for rec in replay.load_matrix().values():
        for model, (_acc, cost) in rec["models"].items():
            by_model[model].append(cost)
    return {m: statistics.mean(v) for m, v in by_model.items()}


def reprice(meta: dict, realised: dict[str, float]) -> int:
    """Overwrite every cell's cost with the realised per-query cost for that model."""
    missing, touched = set(), 0
    for cells in meta["table"].values():
        for model, cell in cells.items():
            if model in realised:
                cell["cost"] = realised[model]
                touched += 1
            else:
                missing.add(model)
    if missing:
        print(f"  no realised price for: {sorted(missing)}", file=sys.stderr)
    return touched


def gate_d(items, models, meta, seeds: int, train_frac: float, min_n: int):
    """The Gate D comparison: best swept-lambda cluster policy vs the best constant."""
    best = None
    for lam in (0.0, 0.02, 0.05, 0.11, 0.25, 0.5, 1.0, 2.0):
        vals = []
        for seed in range(seeds):
            rng = fs.random.Random(seed)
            shuf = items[:]
            rng.shuffle(shuf)
            k = int(len(shuf) * train_frac)
            train, test = shuf[:k], shuf[k:]
            policy, fallback = fs.fit_cluster_policy(train, models, meta, lam, min_n)
            a, c = fs.evaluate(test, models, meta,
                               lambda h: policy.get(meta["cluster"][h], fallback))
            vals.append(fs.arena_score(c, a) * 100)
        m = statistics.mean(vals)
        if best is None or m > best[1]:
            best = (lam, m)

    const = []
    for model in models:
        vals = []
        for seed in range(seeds):
            rng = fs.random.Random(seed)
            shuf = items[:]
            rng.shuffle(shuf)
            test = shuf[int(len(shuf) * train_frac):]
            a, c = fs.evaluate(test, models, meta, lambda _h, m=model: m)
            vals.append(fs.arena_score(c, a) * 100)
        const.append((statistics.mean(vals), model))
    const.sort(reverse=True)
    return best[0], best[1], const[0][0], const[0][1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--train-frac", type=float, default=0.5)
    ap.add_argument("--min-n", type=int, default=25)
    args = ap.parse_args()

    outcomes = [REPO / "data" / "outcomes" / "cheap_tier.jsonl"]

    print("=== A. as fit: external costs (reasoning disabled, output capped) ===")
    items, models, meta = fs.load(outcomes)
    lam_a, routed_a, bar_a, best_const_a = gate_d(
        items, models, meta, args.seeds, args.train_frac, args.min_n
    )
    print(f"  cluster routing {routed_a:.2f} (lambda={lam_a})  vs  best constant "
          f"{bar_a:.2f} ({best_const_a.split('/')[-1]})  ->  {routed_a - bar_a:+.2f}")

    print("\n=== B. repriced: RouterArena-realised per-model costs ===")
    items, models, meta = fs.load(outcomes)
    realised = realised_cost_per_query()
    reprice(meta, realised)
    lam_b, routed_b, bar_b, best_const_b = gate_d(
        items, models, meta, args.seeds, args.train_frac, args.min_n
    )
    print(f"  cluster routing {routed_b:.2f} (lambda={lam_b})  vs  best constant "
          f"{bar_b:.2f} ({best_const_b.split('/')[-1]})  ->  {routed_b - bar_b:+.2f}")

    delta_a, delta_b = routed_a - bar_a, routed_b - bar_b
    print("\n=== VERDICT ===")
    print(f"  Gate D margin as fit:    {delta_a:+.2f} Arena points")
    print(f"  Gate D margin repriced:  {delta_b:+.2f} Arena points")
    print(f"  sub_10 reality:          {72.49 - 75.12:+.2f} Arena points")
    if delta_b <= 0:
        print("\n  CONFIRMED: the cost model was the bug. Repricing alone flips Gate D's "
              "sign, matching what sub_10 actually did. Fix the cost model (E3).")
    elif delta_b < delta_a / 2:
        print("\n  PARTIAL: repricing removes most of the claimed margin but not all of it. "
              "The cost model is *a* bug; E1.3 must check whether the clusters carry signal.")
    else:
        print("\n  REFUTED: the margin survives repricing, so cost was not the whole story. "
              "E1.3 is required -- do not proceed to E3 on this diagnosis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
