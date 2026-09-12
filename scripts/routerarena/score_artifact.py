#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt measurement tool -- scores a frozen artifact, fits nothing
"""Score a frozen policy artifact against a half of the captured sub_10 matrix.

The artifact decides from prompt text using the same pure-Python classifier that ships inside
RouterArena's harness. Only its **decisions** reach the matrix, so the outcome data cannot leak
back into the decision -- which is the whole reason the router and the scorer are separate
programs rather than one convenient function.

Reads ``dev`` by default; ``sealed`` and ``all`` require ``--i-am-freezing``, and every read is
appended to ``data/policy/peek_log.jsonl``.

Usage::

    python scripts/routerarena/score_artifact.py data/policy/routerarena_policy_v2.json
    python scripts/routerarena/score_artifact.py <artifact> --compare
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")

# The submission module imports RouterArena's BaseRouter, absent here. We only want its pure
# feature function, so stub the dependency rather than vendor the harness.
_stub = type(sys)("router_inference.router.base_router")
_stub.BaseRouter = type("BaseRouter", (), {"__init__": lambda self, *a, **k: None})
sys.modules.setdefault("router_inference", type(sys)("router_inference"))
sys.modules.setdefault("router_inference.router", type(sys)("router_inference.router"))
sys.modules["router_inference.router.base_router"] = _stub
sub = _load("submission_router", REPO / "submissions" / "routerarena" / "llm_router.py")


def decisions(artifact: dict, matrix: dict) -> dict[str, str]:
    """What this artifact routes each query to, from prompt text alone."""
    n_buckets = artifact["n_buckets"]
    centroids = {c: {int(k): v for k, v in vec.items()}
                 for c, vec in artifact["centroids"].items()}
    cluster_to_model = artifact["cluster_to_model"]
    fallback = artifact["global_fallback"]

    picks = {}
    for gi, rec in matrix.items():
        feats = sub._features(rec["prompt"], n_buckets)
        best_name, best = "", -1.0
        for name, cent in centroids.items():
            score = sum(v * feats.get(k, 0.0) for k, v in cent.items())
            if score > best:
                best, best_name = score, name
        model = cluster_to_model.get(best_name, fallback) if best_name else fallback
        # A model the artifact names but the matrix never measured cannot be scored. Falling
        # back silently would flatter the result, so say so.
        if model not in rec["models"]:
            raise SystemExit(
                f"artifact routes {gi} to {model!r}, which sub_10 never measured. "
                "The candidate pool and the measured pool have diverged."
            )
        picks[gi] = model
    return picks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("artifact", type=Path)
    ap.add_argument("--half", choices=("dev", "sealed", "all"), default="dev")
    ap.add_argument("--i-am-freezing", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="also print the constants and ceilings this has to beat")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()

    if args.half in ("sealed", "all") and not args.i_am_freezing:
        print(f"Refusing to read '{args.half}' without --i-am-freezing.", file=sys.stderr)
        return 2

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    if args.half != "all":
        matrix = {k: v for k, v in matrix.items() if split[k] == args.half}

    artifact = json.loads(args.artifact.read_text())
    picks = decisions(artifact, matrix)
    res = replay.score(matrix, lambda gi, _rec: picks[gi])

    label = args.artifact.name
    if not args.no_log:
        replay.log_peek(args.half, f"artifact:{label}", res)

    print(f"{label}  half={args.half}  n={res['n_queries']}")
    print(f"  accuracy  {res['accuracy'] * 100:6.2f}%   cost/1k ${res['cost_per_1k']:.4f}"
          f"   ARENA {res['arena_score'] * 100:6.2f}")

    if args.compare:
        print("\n  reference points on the same queries:")
        rows = []
        models = sorted({m for rec in matrix.values() for m in rec["models"]})
        for m in models:
            r = replay.score(matrix, replay.policy_always(m))
            rows.append((r["arena_score"] * 100, f"always {m.split('/')[-1]}", r))
        for name, pol in (("oracle (per-query ceiling)", replay.policy_oracle),
                          ("cheapest candidate", replay.policy_cheapest)):
            r = replay.score(matrix, pol)
            rows.append((r["arena_score"] * 100, name, r))
        for s, name, r in sorted(rows, reverse=True):
            print(f"    {name:<38}{r['accuracy'] * 100:>7.2f}%"
                  f"{r['cost_per_1k']:>10.4f}{s:>9.2f}")
        best_const = max(s for s, n, _ in rows if n.startswith("always"))
        delta = res["arena_score"] * 100 - best_const
        print(f"\n  vs best constant: {delta:+.2f} Arena points "
              f"({'earns its keep' if delta > 0 else 'ROUTING IS COSTING POINTS'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
