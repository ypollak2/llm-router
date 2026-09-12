#!/usr/bin/env python3
"""Fit the shipped routing policy and export it as a single self-contained artifact.

The artifact holds two things, both fit **only** on the audited external corpus and our own
measured outcomes:

1. a **skill-cluster classifier** -- hashed bag-of-words centroids, so classification at
   evaluation time is pure Python with no model download, no Ollama and no network call; and
2. a **cluster -> model map**, chosen by ``argmax(accuracy - lambda * cost)`` per cluster.

Why hashed bag-of-words rather than the embedding classifier we use locally: RouterArena runs
our router inside their harness, where an Ollama embedding backend does not exist. A router
that cannot run in the evaluator's environment is not a router. Held-out accuracy of the
classifier is reported below so the cost of that choice is visible rather than assumed.

Density floor: any cluster with fewer than ``--min-n`` training items falls back to the global
best model. Measured twice, independently -- Track A's learning curve on RouterArena data and
a sweep on external data -- both put the knee at ~25 items per group. Below it, a cluster's
"best model" is mostly noise and acting on it is worse than not routing at all.

Usage::

    python scripts/routerarena/export_policy.py --out data/policy/routerarena_policy.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cost_predictability

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "data" / "corpus"
OUTCOMES = REPO / "data" / "outcomes" / "cheap_tier.jsonl"

N_BUCKETS = 2048
_TOKEN = re.compile(r"[a-z0-9']+")
_STOP = frozenset(
    "the a an of and or to in is are was were be been for on at by with as it its this that "
    "these those from which who whom what when where how why not no if then than there here "
    "do does did done has have had can could would should will shall may might must".split()
)


def features(text: str) -> dict[int, float]:
    """L2-normalised hashed bag of words, with light 2-gram signal.

    Deterministic and dependency-free by design: this exact function has to run inside
    RouterArena's harness, so it cannot import anything that is not already there.
    """
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP and len(w) > 1]
    if not words:
        return {}
    counts: Counter[int] = Counter()
    for w in words[:400]:
        counts[hash_token(w)] += 1
    for a, b in zip(words[:400], words[1:400]):
        counts[hash_token(f"{a}_{b}")] += 1
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {k: v / norm for k, v in counts.items()}


def hash_token(tok: str) -> int:
    """Stable across processes and Python runs -- ``hash()`` is not (PYTHONHASHSEED)."""
    h = 2166136261
    for ch in tok.encode():
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h % N_BUCKETS


def dot(a: dict[int, float], b: dict[int, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def fit_centroids(items: list[tuple[str, str]]) -> dict[str, dict[int, float]]:
    """Mean feature vector per cluster, L2-normalised, so scoring is a cosine."""
    sums: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for text, cluster in items:
        for k, v in features(text).items():
            sums[cluster][k] += v
    out = {}
    for cluster, vec in sums.items():
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        out[cluster] = {k: v / norm for k, v in vec.items()}
    return out


def classify(text: str, centroids: dict[str, dict[int, float]]) -> tuple[str, float]:
    f = features(text)
    if not f:
        return "", 0.0
    scored = sorted(((dot(f, c), name) for name, c in centroids.items()), reverse=True)
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    return best, best_score - runner_up  # margin doubles as the confidence signal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=REPO / "data" / "policy" / "routerarena_policy.json")
    ap.add_argument("--min-n", type=int, default=25)
    ap.add_argument("--lam", type=float, default=0.25)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--all-models", action="store_true",
                    help="skip the cost-predictability filter (reproduces the v1 artifact)")
    ap.add_argument("--include-provisional", action="store_true",
                    help="let shape-check-graded clusters fit their own map")
    args = ap.parse_args()

    # ---- corpus: prompt text -> cluster -------------------------------------------------
    texts: dict[str, tuple[str, str]] = {}
    import hashlib

    for path in sorted(CORPUS.glob("*.jsonl")):
        for line in path.open():
            it = json.loads(line)
            h = hashlib.sha256(it["prompt"].encode()).hexdigest()[:16]
            texts[h] = (it["prompt"], it["cluster"])

    # ---- outcomes: (item, model) -> correct, cost ---------------------------------------
    table: dict[str, dict[str, dict]] = defaultdict(dict)
    for line in OUTCOMES.open():
        r = json.loads(line)
        if r.get("error"):
            continue
        table[r["prompt_hash"]][r["model"]] = {"correct": r["correct"], "cost": r["cost_usd"]}

    measured = sorted({m for cells in table.values() for m in cells})
    items = [h for h, cells in table.items() if len(cells) == len(measured) and h in texts]

    # Only models whose cost we have *measured* may be traded off against each other. A model
    # that ran over its output cap, or finished exactly on it, gave us a lower bound instead --
    # and `accuracy - lambda * cost` will always prefer the model whose bill has not arrived.
    # See cost_predictability.py; the verdict is reproducible from our own runs alone.
    if args.all_models:
        models = measured
    else:
        models = cost_predictability.candidate_models(restrict_to=measured)
        dropped = [m for m in measured if m not in models]
        if dropped:
            print("excluded -- cost is a lower bound, not a measurement:")
            for m in dropped:
                print(f"  {m}")
    print(f"{len(items)} items x {len(models)} candidate models "
          f"(of {len(measured)} measured), {len(texts)} corpus texts")

    # ---- 1. classifier, validated held-out ----------------------------------------------
    accs = []
    for seed in range(args.seeds):
        rng = random.Random(seed)
        shuf = items[:]
        rng.shuffle(shuf)
        k = len(shuf) // 2
        cents = fit_centroids([texts[h] for h in shuf[:k]])
        hit = sum(1 for h in shuf[k:] if classify(texts[h][0], cents)[0] == texts[h][1])
        accs.append(hit / len(shuf[k:]))
    print(f"cluster classifier held-out accuracy: {statistics.mean(accs):.4f} "
          f"({min(accs):.4f}-{max(accs):.4f})")

    # ---- 2. cluster -> model, with the density floor ------------------------------------
    by_cluster: dict[str, list[str]] = defaultdict(list)
    for h in items:
        by_cluster[texts[h][1]].append(h)

    def best_over(hs: list[str]) -> str:
        return max(
            models,
            key=lambda m: (
                sum(table[h][m]["correct"] for h in hs) / len(hs)
                - args.lam * sum(table[h][m]["cost"] for h in hs) / len(hs) * 1000
            ),
        )

    # A cluster whose grader only checks the *shape* of an answer (code: "is this valid
    # Python?") has no trustworthy accuracy signal, so its argmax is noise wearing a number.
    # Those clusters take the global fallback rather than a map of their own.
    provisional = set()
    if not args.include_provisional:
        for line in OUTCOMES.open():
            r = json.loads(line)
            if r.get("provisional"):
                provisional.add(r["cluster"])

    global_best = best_over(items)
    policy, thin, shape_only = {}, [], []
    for cl, hs in sorted(by_cluster.items()):
        if cl in provisional:
            shape_only.append(cl)
            policy[cl] = global_best
        elif len(hs) < args.min_n:
            thin.append(cl)
            policy[cl] = global_best
        else:
            policy[cl] = best_over(hs)

    print(f"\nglobal fallback: {global_best}")
    if thin:
        print(f"below density floor ({args.min_n}), using fallback: {thin}")
    if shape_only:
        print(f"shape-check grader only, using fallback: {shape_only}")
    print(f"\n{'cluster':<20}{'model':>44}{'n':>7}")
    for cl in sorted(policy):
        print(f"{cl:<20}{policy[cl]:>44}{len(by_cluster.get(cl, [])):>7}")

    centroids = fit_centroids([texts[h] for h in items])
    artifact = {
        "version": 1,
        "provenance": {
            "corpus": "external, contamination-audited; see data/corpus/*_audit.json",
            "outcomes": f"{len(items)} items x {len(models)} models, measured by us",
            "routerarena_data_used": "none -- no RouterArena item, label or outcome was fit",
            "lambda": args.lam,
            "min_cluster_n": args.min_n,
            "candidate_pool": "models with a measured (not lower-bounded) cost; see "
                              "scripts/routerarena/cost_predictability.py",
            "excluded_models": [m for m in measured if m not in models],
            "shape_only_clusters": sorted(shape_only),
            "classifier_heldout_accuracy": round(statistics.mean(accs), 4),
        },
        "models": models,
        "global_fallback": global_best,
        "cluster_to_model": policy,
        "n_buckets": N_BUCKETS,
        "centroids": {cl: {str(k): round(v, 6) for k, v in vec.items()}
                      for cl, vec in centroids.items()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=1))
    size_kb = args.out.stat().st_size / 1024
    print(f"\nwrote {args.out} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
