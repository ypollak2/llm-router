#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""E5 — choose the model pair and the escalation threshold, on external data only.

The policy is: run ``cheap`` by default, escalate to ``expensive`` when the predictor's score
clears a threshold. Three things have to be chosen — which two models, which way round, and
where the threshold sits — and all three are chosen here by **held-out external Arena score**,
never by anything measured on RouterArena.

That distinction has already cost us once. The pair I proposed from sub_10 outcomes,
``gemini -> deepseek``, is **net −246** on external data: deepseek is simply the weaker model on
our corpus, so escalating into it loses more than it wins. sub_10 was evidence; the external fit
is the input; when they disagreed the external fit won.

**Robustness to the cost scale we do not know.** E3.1 established that our external costs are
right in *ordering* (Spearman +0.900 after the E2.1 pool filter) but low in *absolute* level,
because RouterArena does not cap output while our labelling did — implied lengths there run
849 tokens against our 20.6. Absolute level matters here: cost that looks free encourages
escalating more than we should. Since we cannot measure the true scale without new API calls,
the threshold is chosen to be **near-optimal across the whole plausible range** (1x to 8x our
measured costs) rather than optimal at an assumed point. A choice that only works at one
unknown scale is not a choice, it is a guess with a decimal point.

Usage::

    python scripts/routerarena/select_policy.py --seeds 8
    python scripts/routerarena/select_policy.py --seeds 8 --pairs-only
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cost_predictability  # noqa: E402
import escalation as E  # noqa: E402

OUTCOMES = E.OUTCOMES  # follow the same RA_OUTCOMES selection

BETA, C_MAX, C_MIN = 0.1, 200.0, 0.0044
COST_SCALES = (1.0, 2.0, 4.0, 8.0)


def arena(accuracy: float, cost_per_1k: float) -> float:
    cost = max(C_MIN, min(cost_per_1k, C_MAX))
    c_i = (math.log2(C_MAX) - math.log2(cost)) / (math.log2(C_MAX) - math.log2(C_MIN))
    return ((1 + BETA) * accuracy * c_i) / (BETA * accuracy + c_i)


def load_costs() -> dict[str, dict[str, float]]:
    """Per (item, model) cost from our own external runs."""
    cost: dict[str, dict[str, float]] = defaultdict(dict)
    for line in OUTCOMES.open():
        r = json.loads(line)
        if not r.get("error"):
            cost[r["prompt_hash"]][r["model"]] = r["cost_usd"]
    return cost


def policy_arena(
    rows: list[dict],
    scores: list[float],
    threshold: float,
    cheap: str,
    expensive: str,
    cost: dict,
    scale: float,
) -> tuple[float, float, float]:
    """(accuracy, cost_per_1k, arena) for 'escalate when score >= threshold'."""
    acc = c = 0.0
    for r, s in zip(rows, scores):
        up = s >= threshold
        acc += r["expensive_correct"] if up else r["cheap_correct"]
        c += cost[r["prompt_hash"]][expensive if up else cheap]
    n = len(rows)
    a = acc / n
    c1k = c / n * 1000 * scale
    return a, c1k, arena(a, c1k)


def evaluate_pair(cheap: str, expensive: str, seeds: int, cost: dict) -> dict | None:
    """Held-out external Arena across thresholds and cost scales."""
    rows = E.build_table(cheap, expensive)
    if sum(r["label"] for r in rows) < 100:
        return None

    # The escalation rate has to be free to reach 100%, or the search cannot report the one
    # answer that matters most: that the optimum is "just use the expensive model", i.e. that
    # routing adds nothing. Capping the grid below 100% hides that outcome behind a boundary.
    grid = {q: {s: [] for s in COST_SCALES} for q in range(0, 101, 5)}

    for seed in range(seeds):
        tr, te = E.split_rows(rows, seed)
        w, b = E.train_logreg(tr, seed=seed)
        sc = E.predict_scores(te, w, b)
        ordered = sorted(sc, reverse=True)
        for q in grid:
            if q == 0:
                thr = float("inf")
            elif q >= 100:
                thr = float("-inf")
            else:
                thr = ordered[max(0, min(len(ordered) - 1, int(len(ordered) * q / 100)))]
            for scale in COST_SCALES:
                grid[q][scale].append(
                    policy_arena(te, sc, thr, cheap, expensive, cost, scale)[2]
                )

    # Rank on ABSOLUTE held-out Arena, worst-case over the cost scales we cannot rule out --
    # never on improvement over the pair's own default. Ranking by self-improvement rewards
    # starting from a bad model: ministral -> gemini "gains" +11 points and lands at 73.1,
    # below simply always using gemini (76.1). The bar is the best constant, not the pair's
    # own floor.
    summary = {}
    for q in grid:
        per_scale = [statistics.mean(grid[q][s]) for s in COST_SCALES]
        summary[q] = {
            "worst_arena": min(per_scale) * 100,
            "arena_at_1x": statistics.mean(grid[q][1.0]) * 100,
        }
    best_q = max(summary, key=lambda q: summary[q]["worst_arena"])
    return {
        "cheap": cheap,
        "expensive": expensive,
        "positives": sum(r["label"] for r in rows),
        "base_arena_1x": summary[0]["arena_at_1x"],
        "best_rate": best_q,
        "summary": summary,
        "worst_arena": summary[best_q]["worst_arena"],
        "arena_at_1x": summary[best_q]["arena_at_1x"],
    }


def constant_baselines(pool: list[str], seeds: int, cost: dict) -> dict[str, dict]:
    """Held-out external Arena for 'always use model X', the bar every pair must clear."""
    rows = E.build_table(pool[0], pool[1])
    out = {}
    prompts, clusters, tab, _ = E.load_external()
    correct = tab["correct"]
    for m in pool:
        per_scale = []
        for scale in COST_SCALES:
            vals = []
            for seed in range(seeds):
                _tr, te = E.split_rows(rows, seed)
                a = statistics.mean(correct[r["prompt_hash"]][m] for r in te)
                c = statistics.mean(cost[r["prompt_hash"]][m] for r in te) * 1000 * scale
                vals.append(arena(a, c))
            per_scale.append(statistics.mean(vals))
        out[m] = {"worst_arena": min(per_scale) * 100, "arena_at_1x": per_scale[0] * 100}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--pairs-only", action="store_true")
    args = ap.parse_args()

    cost = load_costs()
    measured = sorted({m for cells in cost.values() for m in cells})
    pool = cost_predictability.candidate_models(restrict_to=measured)
    print(f"candidate pool ({len(pool)}): {[m.split('/')[-1] for m in pool]}")
    print(f"cost scales considered: {COST_SCALES} (absolute level unknown; ordering is not)\n")

    consts = constant_baselines(pool, args.seeds, cost)
    bar_model = max(consts, key=lambda m: consts[m]["worst_arena"])
    bar = consts[bar_model]["worst_arena"]
    print("held-out external Arena for 'always use X' (worst case over cost scales):")
    for m in sorted(consts, key=lambda m: -consts[m]["worst_arena"]):
        print(f"  {m.split('/')[-1]:<36}{consts[m]['worst_arena']:>8.2f}"
              f"{'   <-- the bar' if m == bar_model else ''}")
    print()

    results = []
    for cheap, expensive in itertools.permutations(pool, 2):
        r = evaluate_pair(cheap, expensive, args.seeds, cost)
        if r:
            results.append(r)
    results.sort(key=lambda r: -r["worst_arena"])

    print(f"{'cheap -> expensive':<56}{'rate':>7}{'arena':>9}{'vs bar':>9}")
    for r in results[:8]:
        print(f"{r['cheap'].split('/')[-1] + ' -> ' + r['expensive'].split('/')[-1]:<56}"
              f"{r['best_rate']:>6}%{r['worst_arena']:>9.2f}{r['worst_arena'] - bar:>+9.2f}")
    print("\n  Ranked on ABSOLUTE held-out Arena at the least favourable cost scale, against")
    print("  the best constant. A rate at 100% means the search found no reason to route.")

    if args.pairs_only or not results:
        return 0

    top = results[0]
    verdict = ("ROUTING EARNS ITS KEEP" if top["worst_arena"] > bar
               else "NO ROUTING POLICY BEATS THE BEST CONSTANT")
    print(f"\nSELECTED: {top['cheap'].split('/')[-1]} -> {top['expensive'].split('/')[-1]}"
          f"  at a {top['best_rate']}% escalation rate")
    print(f"  {top['worst_arena']:.2f} vs best constant {bar:.2f} "
          f"({bar_model.split('/')[-1]}) -> {top['worst_arena'] - bar:+.2f}  {verdict}")
    print(f"\n{'esc rate':>9}{'arena@1x':>11}{'worst-scale arena':>20}")
    for q in sorted(top["summary"]):
        row = top["summary"][q]
        mark = "  <-- selected" if q == top["best_rate"] else ""
        print(f"{q:>8}%{row['arena_at_1x']:>11.2f}{row['worst_arena']:>20.2f}{mark}")

    out = REPO / "data" / "policy" / "escalation_selection.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {k: v for k, v in top.items() if k != "summary"} |
        {"cost_scales": list(COST_SCALES), "seeds": args.seeds,
         "provenance": "selected on held-out external corpus only; no RouterArena data"},
        indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
