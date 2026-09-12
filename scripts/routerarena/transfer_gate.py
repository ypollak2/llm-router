#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt measurement tool -- compares two scores, fits nothing
"""E6.2 — is external-holdout Arena an instrument that predicts RouterArena Arena?

This is the gate Gate D never had, and its absence is the whole reason Track E exists. Gate D
reported +2.67 held out and reality delivered −2.63: not an overstated effect but an inverted
one. It validated on the same external distribution it fit on, where both of its defects — a
cost axis uncorrelated with reality, and clusters that are genuinely informative about the
corpus they came from — are invisible by construction.

A single number cannot detect that. What can is **rank correlation across several candidate
policies**: if external holdout ranks policies the way `dev` does, then choosing by external
holdout is choosing well, even when the absolute values differ. If it does not, no downstream
number means anything and the honest move is to stop.

The candidates deliberately span the space that matters — every constant, and the selected
escalation policy across a range of rates — because a correlation measured only over policies
that are all nearly identical proves nothing.

Reads `dev` only. Nothing here is fit; the predictor is trained on external data and merely
*applied* to dev prompts.

Usage::

    python scripts/routerarena/transfer_gate.py --seeds 4
"""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import escalation as E  # noqa: E402
import select_policy as S  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")

MIN_RHO = 0.6


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):  # average ties, or a plateau silently inflates rho
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math_sqrt(sum((a - mx) ** 2 for a in rx)) * math_sqrt(
        sum((b - my) ** 2 for b in ry)
    )
    return num / den if den else 0.0


def math_sqrt(x: float) -> float:
    return x ** 0.5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--cheap", default="google/gemini-3.1-flash-lite-preview")
    ap.add_argument("--expensive", default="qwen/qwen3-30b-a3b-instruct-2507")
    args = ap.parse_args()

    cost = S.load_costs()
    measured = sorted({m for cells in cost.values() for m in cells})
    import cost_predictability
    pool = cost_predictability.candidate_models(restrict_to=measured)

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    print(f"dev half: {len(dev)} queries | external corpus: 5200 items\n")

    # ---- train the predictor on ALL external rows, then apply it to dev prompts ----------
    rows = E.build_table(args.cheap, args.expensive)
    w, b = E.train_logreg(rows, seed=0)
    dev_rows = [{"prompt": rec["prompt"], "prompt_hash": gi} for gi, rec in dev.items()]
    dev_scores = dict(zip(dev.keys(), E.predict_scores(dev_rows, w, b)))
    ordered_dev = sorted(dev_scores.values(), reverse=True)

    consts = S.constant_baselines(pool, args.seeds, cost)

    candidates: list[tuple[str, float, float]] = []

    for m in pool:
        ext = consts[m]["arena_at_1x"]
        got = replay.score(dev, replay.policy_always(m))["arena_score"] * 100
        candidates.append((f"always {m.split('/')[-1]}", ext, got))

    pair = S.evaluate_pair(args.cheap, args.expensive, args.seeds, cost)
    for rate in (0, 5, 10, 20, 40, 70, 100):
        ext = pair["summary"][rate]["arena_at_1x"]
        if rate == 0:
            thr = float("inf")
        elif rate >= 100:
            thr = float("-inf")
        else:
            thr = ordered_dev[max(0, min(len(ordered_dev) - 1,
                                         int(len(ordered_dev) * rate / 100)))]
        picks = {
            gi: (args.expensive if dev_scores[gi] >= thr else args.cheap) for gi in dev
        }
        got = replay.score(dev, lambda gi, _r: picks[gi])["arena_score"] * 100
        candidates.append((f"escalate @{rate}%", ext, got))

    print(f"{'candidate policy':<44}{'external':>10}{'dev':>9}")
    for name, ext, got in candidates:
        print(f"  {name:<42}{ext:>10.2f}{got:>9.2f}")

    rho = spearman([c[1] for c in candidates], [c[2] for c in candidates])
    print(f"\n  n = {len(candidates)} candidate policies")
    print(f"  Spearman rho (external holdout vs dev) = {rho:+.3f}")
    print(f"  gate: rho >= {MIN_RHO}  ->  {'PASS' if rho >= MIN_RHO else 'FAIL'}")
    if rho < MIN_RHO:
        print("\n  The instrument does not rank policies the way RouterArena does. Choosing by")
        print("  external holdout is therefore choosing at random, whatever the margins look")
        print("  like. Per the plan: stop here and fix the instrument -- do not proceed to E7.")
    return 0 if rho >= MIN_RHO else 1


if __name__ == "__main__":
    raise SystemExit(main())
