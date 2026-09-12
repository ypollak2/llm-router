#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt measures the noise floor of the dev half; selects nothing
"""G3.0 — how well could *any* instrument correlate with sub_10, given sub_10's own noise?

Before spending on a bigger validation set, one question has to be settled: when external
holdout and `dev` disagreed at rho = 0.561, was that our instrument being biased, or `dev`
being noisy?

They call for opposite responses. If external holdout is biased -- it measures a different
distribution -- then a bigger, better-matched surrogate is the fix and G3 is worth its money.
If `dev` is simply noisy at 431 queries, then **no instrument can correlate with it better than
it correlates with itself**, our 0.561 may already be near the ceiling, and building a
surrogate buys nothing because the target is the limiting factor.

The measurement is a split-half reliability check, standard in psychometrics and free here.
Split `dev` into halves, score every candidate on each, and correlate the two halves against
each other. That correlation is an upper bound on what any external predictor could achieve,
because both halves are drawn from the *same* distribution by the *same* process -- any
disagreement between them is pure sampling noise.

Repeated over many random splits, since a single split is itself a noisy estimate of noise.

Usage::

    python scripts/routerarena/reliability_ceiling.py --splits 40
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import random
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")
tg = _load("transfer_gate", REPO / "scripts" / "routerarena" / "transfer_gate.py")

OBSERVED_RHO = 0.561  # what external holdout achieved against dev in F5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--splits", type=int, default=40)
    args = ap.parse_args()

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    models = sorted({m for rec in dev.values() for m in rec["models"]})
    keys = sorted(dev)
    print(f"dev half: {len(keys)} queries, {len(models)} constants\n")

    # Candidates span constants *and* escalation ladders, matching the mix the F5 gate ranked.
    # A reliability estimated over constants alone would not describe the gate that failed.
    cheap, exp = "google/gemini-3.1-flash-lite-preview", "qwen/qwen3-30b-a3b-instruct-2507"

    def policies():
        for m in models:
            yield f"always {m.split('/')[-1]}", replay.policy_always(m)
        for rate in (10, 30, 60):
            def pol(gi, rec, r=rate):
                # deterministic pseudo-escalation on a hash, standing in for a predictor:
                # what matters here is that it is a *policy shaped* candidate, not that it
                # is a good one.
                h = int(hashlib.sha256(gi.encode()).hexdigest()[:8], 16) % 100
                return exp if h < r else cheap
            yield f"escalate ~{rate}%", pol

    cands = list(policies())
    print(f"{len(cands)} candidate policies (constants + escalation ladders)\n")

    rhos = []
    for s in range(args.splits):
        rng = random.Random(s)
        shuf = keys[:]
        rng.shuffle(shuf)
        half = len(shuf) // 2
        a = {k: dev[k] for k in shuf[:half]}
        b = {k: dev[k] for k in shuf[half:]}
        xs, ys = [], []
        for _name, pol in cands:
            xs.append(replay.score(a, pol)["arena_score"] * 100)
            ys.append(replay.score(b, pol)["arena_score"] * 100)
        rhos.append(tg.spearman(xs, ys))

    mean_rho = statistics.mean(rhos)
    lo, hi = min(rhos), max(rhos)
    print(f"split-half reliability of dev, over {args.splits} random splits:")
    print(f"  mean rho = {mean_rho:+.3f}   range {lo:+.3f} to {hi:+.3f}")
    print("\n  This is dev correlating with ITSELF. Two halves of the same data, same "
          "distribution,\n  same process -- every point of disagreement is sampling noise, "
          "not bias.")

    # Spearman-Brown: a split-half estimate describes halves, but the gate is run on the whole
    # dev set, which is twice as long and therefore more reliable. Step it up accordingly.
    full = (2 * mean_rho) / (1 + mean_rho) if mean_rho > -1 else float("nan")
    print(f"\n  Spearman-Brown adjusted to full dev length: rho_max = {full:+.3f}")
    print(f"  external holdout achieved:                   rho     = {OBSERVED_RHO:+.3f}")

    print("\nVERDICT")
    if full <= OBSERVED_RHO + 0.05:
        print("  dev is the limiting factor. Our instrument is already performing at roughly")
        print("  the ceiling this target allows, and NO surrogate -- however large or well")
        print("  matched -- can push the F5 gate much higher, because the gate is measuring")
        print("  sub_10's own sampling noise. G3 would be spending money on the wrong problem.")
    elif full >= OBSERVED_RHO + 0.20:
        print("  headroom is real. dev is reliable enough to distinguish policies that our")
        print("  external holdout cannot, so the gap is our instrument's bias and a better")
        print("  validation set is the right fix. Proceed with G3.")
    else:
        print("  partial. Some headroom exists but less than the gap suggests; a surrogate")
        print("  could help modestly. Weigh the spend against the remaining margin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
