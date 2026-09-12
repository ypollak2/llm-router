#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt measurement tool -- scores frozen policies, fits and selects nothing
"""G6 — the internal evaluation. One command, offline, free, no API key.

Everything RouterArena's harness would tell us about a frozen policy, computed from the
captured 809x8 outcome matrix: accuracy, cost, Arena, the three optimality ratios, every
constant, the per-query oracle, and the gap between them.

**Reads `dev` only.** The sealed half was spent once, on the frozen constant, and re-reading it
would turn a one-shot honest number into a number we kept looking at until we liked it. The
sealed figures below are *recalled* from the run that produced them, not recomputed.

Usage::

    python scripts/routerarena/evaluate.py
    python scripts/routerarena/evaluate.py --artifact data/policy/routerarena_policy_v3_constant.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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

# Recorded at the time of the single sealed read. Quoted, never recomputed.
SEALED = {
    "submission (always gemini-3.1-flash-lite)": (0.7553, 0.0644, 75.48),
    "always deepseek-v4-flash": (0.7583, 0.2649, 74.30),
    "previously shipped router": (0.7505, 0.7282, 72.21),
}
SUB10_TO_FULL = -1.23  # our own router: 72.49 on sub_10 (all 809) -> 71.26 on the leaderboard
LEADERBOARD_TOP = 77.63


def line(label: str, r: dict, mark: str = "") -> str:
    return (f"  {label:<44}{r['accuracy'] * 100:>8.2f}%{r['cost_per_1k']:>10.4f}"
            f"{r['arena_score'] * 100:>9.2f}{mark}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifact", type=Path,
                    default=REPO / "data" / "policy" / "routerarena_policy_v3_constant.json")
    args = ap.parse_args()

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    models = sorted({m for rec in matrix.values() for m in rec["models"]})

    print("=" * 78)
    print("RouterArena internal evaluation  ·  offline, no API key, dev half only")
    print("=" * 78)
    print(f"\nmatrix: {len(matrix)} queries x {len(models)} models "
          f"| dev {len(dev)} | sealed {len(matrix) - len(dev)} (spent, not read)\n")

    print(f"  {'policy':<44}{'acc':>9}{'$/1k':>10}{'arena':>9}")
    rows = []
    for m in models:
        r = replay.score(dev, replay.policy_always(m))
        rows.append((r["arena_score"], f"always {m.split('/')[-1]}", r))
    for s, label, r in sorted(rows, reverse=True):
        print(line(label, r))

    print()
    shipped = replay.score(dev, replay.policy_shipped)
    print(line("previously shipped router (cluster map)", shipped))
    oracle = replay.score(dev, replay.policy_oracle)
    print(line("ORACLE: cheapest correct per query", oracle, "   <-- pool ceiling"))

    best_arena, best_label, best_r = max(rows)
    print(f"\n  best constant on dev: {best_label} at {best_arena * 100:.2f}")
    print(f"  gap to the oracle:    {(oracle['arena_score'] - best_arena) * 100:+.2f} Arena "
          f"points still on the table inside this pool")

    # ---- the sealed record, recalled ---------------------------------------------------
    print("\n" + "-" * 78)
    print("SEALED HALF -- recorded at the single read, quoted not recomputed")
    print("-" * 78)
    print(f"  {'policy':<44}{'acc':>9}{'$/1k':>10}{'arena':>9}")
    for label, (a, c, s) in SEALED.items():
        print(f"  {label:<44}{a * 100:>8.2f}%{c:>10.4f}{s:>9.2f}")
    sub = SEALED["submission (always gemini-3.1-flash-lite)"][2]
    print(f"\n  submission vs previously shipped: "
          f"{sub - SEALED['previously shipped router'][2]:+.2f} Arena points")

    # ---- what it means on the leaderboard ----------------------------------------------
    print("\n" + "-" * 78)
    print("PROJECTION")
    print("-" * 78)
    print(f"  sub_10 sealed:                  {sub:.2f}")
    print(f"  sub_10 -> full adjustment:      {SUB10_TO_FULL:+.2f}  "
          f"(measured on our own router: 72.49 -> 71.26)")
    print(f"  projected on the full split:    {sub + SUB10_TO_FULL:.2f}")
    print(f"  current leaderboard #1:         {LEADERBOARD_TOP:.2f}")
    print(f"  shortfall:                      {sub + SUB10_TO_FULL - LEADERBOARD_TOP:+.2f}")

    reached = (sub >= 78.0)
    print(f"\n  Was 78 reached on sub_10?  {'YES' if reached else 'NO'} "
          f"({sub:.2f})")

    # ---- gate ledger --------------------------------------------------------------------
    print("\n" + "-" * 78)
    print("GATE LEDGER")
    print("-" * 78)
    for name, verdict, note in [
        ("E6.2 transfer gate", "FAIL", "rho +0.561 vs 0.60 required"),
        ("F5 transfer gate (rebuilt corpus)", "FAIL", "rho +0.561; constants +0.800"),
        ("G1.5 better constant", "FAIL", "kimi-k2.5 +3.91 acc but 7.7x cost"),
        ("G2.2 mix-invariant ranking", "PASS", "mean-rank +0.524 vs mean +0.381"),
        ("G3 surrogate benchmark", "NOT BUILT", "premise falsified: rho flat in sample size"),
        ("G4 invariant rules", "FAIL", "-0.47 vs best constant on dev"),
    ]:
        print(f"  {name:<38}{verdict:<12}{note}")

    print("\n  Every routing mechanism tried has failed a gate. The constant stands because")
    print("  it is the only thing the evidence supports, not because it was preferred.")

    if args.artifact.exists():
        art = json.loads(args.artifact.read_text())
        print(f"\n  frozen artifact: {args.artifact.name}")
        print(f"    models: {art['models']}  fallback: {art['global_fallback']}")
        print(f"    cluster_to_model entries: {len(art['cluster_to_model'])} "
              "(empty = constant router)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
