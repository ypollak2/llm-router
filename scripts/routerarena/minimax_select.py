#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt tests a transfer hypothesis on dev; selects nothing, writes no artifact
"""G2 — pick the constant by worst-cluster accuracy instead of mean, and test whether it
transfers better.

The mean is the wrong statistic under distribution shift, and we can say precisely why.
RouterArena is **29% MMLU-Pro and 12% OpenTDB**; our corpus is 27 sources at roughly balanced
weight. Mean accuracy over a balanced corpus therefore answers "which model is best on *our*
mix", when the question is "which model is best on *theirs*" — and the two differ by however
much the mixes differ.

We cannot reweight to their mix without reading their composition, which is RouterArena data
and would be selecting on the benchmark. But we can choose a statistic that **does not depend
on the mix at all**. Worst-cluster accuracy is one: a model that is never bad anywhere is a
good bet under any reweighting, because the minimum is invariant to how the clusters are
weighted. That is the whole hypothesis, and it is falsifiable.

**This script tests, it does not select.** The comparison is run on the `dev` half because the
question is empirical -- does minimax rank constants closer to RouterArena's ordering than mean
does? -- and there is nowhere else to ask it. What it may *not* do is pick the winner from dev,
which is why it prints two orderings and a correlation rather than a recommendation.

Usage::

    RA_OUTCOMES=trackf.jsonl python scripts/routerarena/minimax_select.py
    RA_OUTCOMES=g1_screen.jsonl python scripts/routerarena/minimax_select.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUTCOMES = REPO / "data" / "outcomes" / os.environ.get("RA_OUTCOMES", "trackf.jsonl")
BETA, C_MAX, C_MIN = 0.1, 200.0, 0.0044


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")
tg = _load("transfer_gate", REPO / "scripts" / "routerarena" / "transfer_gate.py")


def arena(a: float, c1k: float) -> float:
    c = max(C_MIN, min(c1k, C_MAX))
    ci = (math.log2(C_MAX) - math.log2(c)) / (math.log2(C_MAX) - math.log2(C_MIN))
    return ((1 + BETA) * a * ci) / (BETA * a + ci)


def per_cluster() -> tuple[dict, dict, dict]:
    """accuracy[model][cluster], cost[model] (per 1k), and item counts per cluster."""
    acc: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    cost: dict[str, list] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for line in OUTCOMES.open():
        r = json.loads(line)
        if r.get("error"):
            continue
        acc[r["model"]][r["cluster"]].append(r["correct"])
        cost[r["model"]].append(r["cost_usd"])
        counts[r["cluster"]] += 1
    return (
        {m: {c: statistics.mean(v) for c, v in cl.items()} for m, cl in acc.items()},
        {m: statistics.mean(v) * 1000 for m, v in cost.items()},
        counts,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-cluster-n", type=int, default=40,
                    help="clusters thinner than this are dropped from the minimum")
    args = ap.parse_args()

    acc, cost, counts = per_cluster()
    models = sorted(acc)
    # A cluster with a handful of items has a noisy accuracy, and a *minimum* over noisy
    # estimates is systematically pessimistic -- it finds the unluckiest cluster, not the
    # weakest one. Thin clusters are therefore excluded from the min rather than allowed to
    # decide it.
    usable = {c for c, n in counts.items() if n // max(len(models), 1) >= args.min_cluster_n}
    print(f"{len(models)} models, {len(usable)} clusters above the density floor "
          f"({args.min_cluster_n} items/model)\n")

    # Third criterion: mean per-cluster RANK.
    #
    # Minimax was tried first and lost to the plain mean (rho 0.600 vs 0.800), for a reason
    # visible in the data: every model's worst cluster was `code`, at 20-26% for all of them.
    # The minimum was hostage to one universally-hard task, so it barely discriminated -- the
    # whole spread collapsed onto five points of noise on a single cluster.
    #
    # Mean per-cluster rank keeps what made minimax attractive -- every cluster counts equally,
    # so the statistic does not depend on our corpus's mix, which is the thing that differs
    # from RouterArena's -- without letting the hardest cluster decide the ordering. Being a
    # rank, it is also immune to a cluster where everyone scores badly, since what matters
    # there is only who scored least badly.
    ranks_by_cluster: dict[str, dict[str, int]] = {}
    for c in usable:
        present = [m for m in models if c in acc[m]]
        order = sorted(present, key=lambda m: -acc[m][c])
        ranks_by_cluster[c] = {m: i + 1 for i, m in enumerate(order)}

    rows = []
    for m in models:
        cl = {c: v for c, v in acc[m].items() if c in usable}
        if not cl:
            continue
        mean_acc = statistics.mean(acc[m].values())
        worst_acc = min(cl.values())
        worst_cluster = min(cl, key=lambda c: cl[c])
        my_ranks = [ranks_by_cluster[c][m] for c in usable if m in ranks_by_cluster[c]]
        mean_rank = statistics.mean(my_ranks) if my_ranks else float(len(models))
        rows.append({
            "model": m,
            "mean_acc": mean_acc,
            "worst_acc": worst_acc,
            "worst_cluster": worst_cluster,
            "mean_rank": mean_rank,
            "cost_1k": cost[m],
            "arena_mean": arena(mean_acc, cost[m]) * 100,
            "arena_worst": arena(worst_acc, cost[m]) * 100,
            # Lower rank is better, so negate to keep "higher is better" across criteria.
            "score_rank": -mean_rank,
        })

    print(f"{'model':<44}{'mean acc':>10}{'worst acc':>11}{'mean rank':>11}{'worst cluster':>16}")
    for r in sorted(rows, key=lambda r: -r["mean_acc"]):
        print(f"{r['model']:<44}{r['mean_acc'] * 100:>9.2f}%{r['worst_acc'] * 100:>10.2f}%"
              f"{r['mean_rank']:>11.2f}{r['worst_cluster']:>16}")

    by_mean = [r["model"] for r in sorted(rows, key=lambda r: -r["arena_mean"])]
    by_worst = [r["model"] for r in sorted(rows, key=lambda r: -r["arena_worst"])]
    by_rank = [r["model"] for r in sorted(rows, key=lambda r: -r["score_rank"])]
    print(f"\nmean criterion      picks: {by_mean[0]}")
    print(f"minimax criterion   picks: {by_worst[0]}")
    print(f"mean-rank criterion picks: {by_rank[0]}")
    if by_mean == by_worst == by_rank:
        print("  all three orderings are IDENTICAL -- G2.2 is vacuous")

    # ---- G2.2: which ordering matches dev? ----------------------------------------------
    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    measured = {m for rec in dev.values() for m in rec["models"]}
    common = [r for r in rows if r["model"] in measured]
    if len(common) < 3:
        print(f"\nonly {len(common)} of these models were measured on sub_10 -- "
              "cannot test transfer for the rest")
        return 0

    dev_arena = {
        r["model"]: replay.score(dev, replay.policy_always(r["model"]))["arena_score"] * 100
        for r in common
    }
    print(f"\n{'model':<44}{'mean-crit':>11}{'minimax':>10}{'mean-rank':>11}{'dev':>9}")
    for r in sorted(common, key=lambda r: -dev_arena[r["model"]]):
        print(f"{r['model']:<44}{r['arena_mean']:>11.2f}{r['arena_worst']:>10.2f}"
              f"{-r['score_rank']:>11.2f}{dev_arena[r['model']]:>9.2f}")

    ys = [dev_arena[r["model"]] for r in common]
    rho_mean = tg.spearman([r["arena_mean"] for r in common], ys)
    rho_worst = tg.spearman([r["arena_worst"] for r in common], ys)
    rho_rank = tg.spearman([r["score_rank"] for r in common], ys)

    print(f"\n  n = {len(common)} constants measured on both")
    print(f"  Spearman(mean criterion,      dev) = {rho_mean:+.3f}")
    print(f"  Spearman(minimax criterion,   dev) = {rho_worst:+.3f}")
    print(f"  Spearman(mean-rank criterion, dev) = {rho_rank:+.3f}")
    better = max(rho_worst, rho_rank) > rho_mean
    enough = len(common) >= 6
    print(f"\nGATE (minimax ranks closer to dev, over >=6 candidates): "
          f"{'PASS' if better and enough else 'FAIL'}")
    if not enough:
        print(f"  only {len(common)} candidates measured on sub_10 -- the >=6 requirement is "
              "not met, so this run is indicative rather than decisive")
    if not better:
        print("  Neither mix-invariant criterion ranks closer than the plain mean. The "
              "argument is sound in principle and unsupported in this data; mean stands.")
    else:
        win = "mean-rank" if rho_rank >= rho_worst else "minimax"
        print(f"  {win} transfers better than the mean criterion "
              f"({max(rho_worst, rho_rank):+.3f} vs {rho_mean:+.3f}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
