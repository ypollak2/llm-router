#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt diagnostic only -- writes no policy artifact, only a report
"""E1.3 — do the skill clusters carry routing signal on RouterArena queries?

E1.2 refuted the cost hypothesis: repricing the external outcome table with RouterArena's
realised per-model costs barely moved Gate D (+2.71 -> +2.52), while sub_10 delivered -2.63. So
the cost axis was wrong (Spearman rho = -0.048 against reality) but it is not why the mechanism
inverted. That leaves the clusters themselves.

The shipped router does two things, and either can break independently:

1. **Assignment.** Put a query in a cluster from its text.
2. **Mapping.** Send that cluster to the model measured best for it on external data.

This measures both, on the ``dev`` half only:

* Mapping transfer -- for each cluster, is the external best model the same as the RouterArena
  best model? A map fit on one distribution and applied to another is only worth anything if
  the argmax agrees.
* Assignment informativeness -- mutual information between the assigned cluster and the
  identity of the best model per query. If MI is ~0, the clusters do not know anything about
  which model wins, and no mapping on top of them can.
* The counterfactual bar -- what the *perfect* cluster map would score on dev. This separates
  "we learned the wrong map" from "no map over these clusters could have worked", which need
  completely different fixes.

Read-only on RouterArena data. Nothing here is fit.

Usage::

    python scripts/routerarena/diagnose_cluster_transfer.py
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "data" / "policy" / "routerarena_policy.json"
SUBMISSION = REPO / "submissions" / "routerarena" / "llm_router.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")
fs = _load("fit_selector", REPO / "scripts" / "routerarena" / "fit_selector.py")

# The submission module imports RouterArena's BaseRouter, which is not on this path. We only
# need its pure feature/classify functions, so stub the dependency rather than vendor it.
_stub = type(sys)("router_inference.router.base_router")
_stub.BaseRouter = type("BaseRouter", (), {"__init__": lambda self, *a, **k: None})
sys.modules.setdefault("router_inference", type(sys)("router_inference"))
sys.modules.setdefault("router_inference.router", type(sys)("router_inference.router"))
sys.modules["router_inference.router.base_router"] = _stub
sub = _load("submission_router", SUBMISSION)


def classify_all(matrix: dict, policy: dict) -> dict[str, str]:
    """Assign every query to a cluster with the exact shipped classifier."""
    n_buckets = policy["n_buckets"]
    centroids = {c: {int(k): v for k, v in vec.items()}
                 for c, vec in policy["centroids"].items()}
    out = {}
    for gi, rec in matrix.items():
        feats = sub._features(rec["prompt"], n_buckets)
        best_name, best = "", -1.0
        for name, cent in centroids.items():
            score = sum(v * feats.get(k, 0.0) for k, v in cent.items())
            if score > best:
                best, best_name = score, name
        out[gi] = best_name or policy["global_fallback"]
    return out


def best_model_per_query(rec: dict) -> str:
    """Cheapest model that answers correctly; cheapest overall when none does."""
    correct = {m: v for m, v in rec["models"].items() if v[0] >= 1.0}
    pool = correct or rec["models"]
    return min(pool, key=lambda m: pool[m][1])


def mutual_information(xs: list[str], ys: list[str]) -> tuple[float, float]:
    """MI(X;Y) in bits, and MI normalised by H(Y)."""
    n = len(xs)
    joint = collections.Counter(zip(xs, ys))
    px = collections.Counter(xs)
    py = collections.Counter(ys)
    mi = 0.0
    for (x, y), c in joint.items():
        pxy = c / n
        mi += pxy * math.log2(pxy / ((px[x] / n) * (py[y] / n)))
    hy = -sum((c / n) * math.log2(c / n) for c in py.values())
    return mi, (mi / hy if hy else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--half", choices=("dev", "sealed", "all"), default="dev")
    args = ap.parse_args()
    if args.half != "dev":
        print("This diagnostic reads dev only, by protocol.", file=sys.stderr)
        return 2

    policy = json.loads(POLICY.read_text())
    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    print(f"dev half: {len(dev)} queries\n")

    assigned = classify_all(dev, policy)
    best = {gi: best_model_per_query(rec) for gi, rec in dev.items()}

    # ---- 1. does the map's argmax agree with reality, cluster by cluster? ----------------
    shipped_map = policy["cluster_to_model"]
    fallback = policy["global_fallback"]
    by_cluster: dict[str, list[str]] = collections.defaultdict(list)
    for gi, cl in assigned.items():
        by_cluster[cl].append(gi)

    print("mapping transfer -- external argmax vs RouterArena argmax, per cluster")
    print(f"{'cluster':<20}{'n':>5}  {'shipped (external)':<32}{'RA-best (dev)':<32}{'':<6}")
    agree = total = 0
    for cl, gis in sorted(by_cluster.items(), key=lambda kv: -len(kv[1])):
        if len(gis) < 5:
            continue
        shipped = shipped_map.get(cl, fallback)
        # Which model would have been best for this cluster on RA dev, by the same rule the
        # fit uses: highest accuracy, ties broken by cost.
        scores = {}
        for m in next(iter(dev.values()))["models"]:
            a = sum(dev[g]["models"][m][0] for g in gis) / len(gis)
            c = sum(dev[g]["models"][m][1] for g in gis) / len(gis)
            scores[m] = (a, -c)
        ra_best = max(scores, key=lambda m: scores[m])
        ok = shipped == ra_best
        agree += ok
        total += 1
        print(f"{cl:<20}{len(gis):>5}  {shipped.split('/')[-1]:<32}"
              f"{ra_best.split('/')[-1]:<32}{'OK' if ok else 'MISS':<6}")
    print(f"\n  argmax agreement: {agree}/{total} clusters")

    # ---- 2. do the clusters know anything about which model wins? -----------------------
    gis = sorted(dev)
    mi, nmi = mutual_information([assigned[g] for g in gis], [best[g] for g in gis])
    fam_mi, fam_nmi = mutual_information(
        [replay.family(g) for g in gis], [best[g] for g in gis]
    )
    print(f"\nassignment informativeness on dev (H(best model) = "
          f"{-sum((c / len(gis)) * math.log2(c / len(gis)) for c in collections.Counter(best.values()).values()):.3f} bits)")
    print(f"  MI(shipped cluster ; best model) = {mi:.4f} bits   (normalised {nmi:.3f})")
    print(f"  MI(dataset family  ; best model) = {fam_mi:.4f} bits   (normalised {fam_nmi:.3f})")
    print("    the family row is the reference: it is the strongest categorical signal")
    print("    available, so it bounds what any category scheme could carry.")

    # ---- 3. could ANY map over these clusters have worked? ------------------------------
    print("\ncounterfactual: the best possible cluster->model map, fit on dev itself")
    for label, mapper in (
        ("shipped map", lambda cl: shipped_map.get(cl, fallback)),
        ("perfect map (in-sample, cheats)", None),
    ):
        if mapper is None:
            perfect = {}
            for cl, cgis in by_cluster.items():
                cand = {}
                for m in next(iter(dev.values()))["models"]:
                    a = sum(dev[g]["models"][m][0] for g in cgis) / len(cgis)
                    c = sum(dev[g]["models"][m][1] for g in cgis) / len(cgis) * 1000
                    cand[m] = fs.arena_score(c, a)
                perfect[cl] = max(cand, key=cand.get)
            mapper = lambda cl: perfect[cl]  # noqa: E731
        res = replay.score(dev, lambda gi, _rec, f=mapper: f(assigned[gi]))
        print(f"  {label:<34} acc={res['accuracy'] * 100:6.2f}%  "
              f"${res['cost_per_1k']:.4f}/1k  arena={res['arena_score'] * 100:6.2f}")
    G = "google/gemini-3.1-flash-lite-preview"
    for label, pol in (("always gemini-lite", replay.policy_always(G)),
                       ("oracle (per-query ceiling)", replay.policy_oracle)):
        res = replay.score(dev, pol)
        print(f"  {label:<34} acc={res['accuracy'] * 100:6.2f}%  "
              f"${res['cost_per_1k']:.4f}/1k  arena={res['arena_score'] * 100:6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
