#!/usr/bin/env python3
"""Check the dataset can tell known-different policies apart.

    python3 scripts/groundtruth/discriminate.py --version v1 --split tune

This is a test of the ruler, not of the router. It replays fixed policies over
the already-computed outcome matrix — no new model calls — and asks whether
they separate:

    always-cheapest   take the cheapest tier every time
    always-premium    take the costliest tier every time
    random            uniform over tiers, averaged over --trials
    oracle            the ground-truth label itself (the ceiling)

If always-cheapest and always-premium score the same, the dataset is not
measuring model capability, and any router number computed on it would be
noise wearing a decimal point. That has a specific precedent worth respecting:
on RouterArena, a constant policy was competitive with everything except
retrieval, which said more about the benchmark than about the routers.

The router is deliberately absent. Comparing it here would invite reading a
router result off a dataset that has not yet been shown to work.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import dataset as ds  # noqa: E402
from groundtruth.run_matrix import TIERS  # noqa: E402


def policy_score(
    matrix: dict, choose, tier_order: list[str], *, strict: bool = True
) -> tuple[float, float, int]:
    """Return (accept_rate, total_cost, n) over DETERMINISTICALLY verified cells.

    The verification-type filter is the point. This function pooled every cell's
    `accepted` boolean with no filtering at all and never imported
    `DETERMINISTIC_METHODS`, so a judge's verdict and a passing assertion would
    have counted identically toward a published accept rate.

    That was harmless only by accident: `run_matrix` runs
    `verifier_kind == MECHANICAL` tasks and nothing else, so no judge-verified
    cell has ever reached this matrix. An accidental barrier is not a designed
    one -- extending `generate_snippet()` to judges is a natural next step, and it
    would have silently started pooling subjective verdicts with mechanical ones
    with no code change here and no signal that anything had altered.

    Fails CLOSED on a cell with no recorded `verification_type`: a matrix written
    before that field existed is UNKNOWN, not assumed mechanical. Same rule as
    `is_evaluable`, for the same reason -- the cost of excluding an old real cell
    is a smaller n, and the cost of admitting a subjective one is a number that
    is quietly wrong.

    `strict=False` is for inspecting a legacy matrix deliberately. Nothing in the
    published path passes it.
    """
    ok = 0
    cost = 0.0
    n = 0
    excluded = 0
    for _task_id, cell in matrix.items():
        present = [t for t in tier_order if t in cell]
        if not present:
            continue
        pick = choose(present, cell)
        if pick not in cell:
            continue
        if strict:
            vtype = cell[pick].get("verification_type")
            if vtype not in ds.DETERMINISTIC_METHODS:
                excluded += 1
                continue
        n += 1
        ok += bool(cell[pick]["accepted"])
        cost += float(cell[pick].get("cost_usd") or 0.0)
    if excluded:
        print(f"  [policy_score] excluded {excluded} cell(s): verification type "
              f"is not deterministic, or was not recorded", file=sys.stderr)
    return (ok / n if n else 0.0), cost, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--split", choices=("tune", "test"), default="tune")
    ap.add_argument("--root", type=Path, default=Path("data/groundtruth"))
    ap.add_argument("--outcomes", default="")
    ap.add_argument("--trials", type=int, default=200, help="random-policy trials")
    ap.add_argument("--min-gap", type=float, default=0.10,
                    help="required accept-rate gap between cheapest and premium")
    ap.add_argument("--seed", type=int, default=20260920)
    args = ap.parse_args()

    root = args.root / args.version
    src = Path(args.outcomes) if args.outcomes else root / f"outcomes.{args.split}.json"
    if not src.exists():
        raise SystemExit(f"no outcome matrix at {src} — run run_matrix.py first")

    payload = json.loads(src.read_text(encoding="utf-8"))
    matrix = payload["matrix"]
    if not matrix:
        raise SystemExit("outcome matrix is empty — nothing to discriminate")

    tier_order = sorted(payload["tiers"], key=lambda t: TIERS[t]["order"])
    n_tasks = len(matrix)

    cheap_rate, cheap_cost, n = policy_score(
        matrix, lambda present, _c: present[0], tier_order)
    prem_rate, prem_cost, _ = policy_score(
        matrix, lambda present, _c: present[-1], tier_order)
    oracle_rate, oracle_cost, _ = policy_score(
        matrix,
        lambda present, cell: next(
            (t for t in present if cell[t]["accepted"]), present[-1]),
        tier_order)

    rng = random.Random(args.seed)
    rand_rates = []
    rand_costs = []
    for _ in range(args.trials):
        r, c, _ = policy_score(matrix, lambda present, _c: rng.choice(present), tier_order)
        rand_rates.append(r)
        rand_costs.append(c)
    rand_rate = sum(rand_rates) / len(rand_rates)
    rand_cost = sum(rand_costs) / len(rand_costs)

    rows = [
        ("always-cheapest", cheap_rate, cheap_cost),
        ("random", rand_rate, rand_cost),
        ("always-premium", prem_rate, prem_cost),
        ("oracle (ground truth)", oracle_rate, oracle_cost),
    ]
    print(f"dataset {args.version} split={args.split}  n={n_tasks} tasks, "
          f"tiers={tier_order}\n")
    print(f"  {'policy':24s} {'accept rate':>12s} {'cost usd':>10s}")
    for name, rate, cost in rows:
        print(f"  {name:24s} {rate:11.1%} {cost:10.4f}")

    gap = prem_rate - cheap_rate
    print(f"\n  premium - cheapest gap   {gap:+.1%}   (need >= {args.min_gap:.0%})")
    print(f"  headroom for a router    {oracle_rate - cheap_rate:+.1%} "
          f"above always-cheapest")

    verdict = 0
    if n_tasks < 50:
        print(f"\n  n={n_tasks} is below 50: too few to tell. Treat every number "
              f"above as illustrative, not as a measurement.", file=sys.stderr)
        verdict = max(verdict, 4)
    if gap < args.min_gap:
        print(f"\n  FAIL: the dataset does not separate cheapest from premium.\n"
              f"  Investigate before using it — either the tasks are too easy "
              f"(everything passes), too hard (nothing passes), or the "
              f"verifiers are not testing what the prompt asks for.",
              file=sys.stderr)
        verdict = max(verdict, 5)
    else:
        print("\n  PASS: the dataset separates the constant policies.")

    (root / f"discrimination.{args.split}.json").write_text(json.dumps({
        "dataset_version": args.version,
        "split": args.split,
        "n_tasks": n_tasks,
        "tiers": tier_order,
        "policies": {name: {"accept_rate": rate, "cost_usd": cost}
                     for name, rate, cost in rows},
        "premium_minus_cheapest": gap,
        "min_gap_required": args.min_gap,
        "verdict": "pass" if verdict == 0 else "fail",
        "note": ("n below 50 means the numbers are illustrative"
                 if n_tasks < 50 else ""),
    }, indent=2) + "\n", encoding="utf-8")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
