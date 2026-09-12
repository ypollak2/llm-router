#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""E4 — a per-query escalation predictor, fit on external outcomes only.

E1.3 closed the category approach: the shipped clusters carry 0.078 normalised MI about which
model wins, and a *perfect* cluster map fit with hindsight is worth only +1.51 points. Category
and difficulty are different variables, and the score lives in difficulty.

So the question this model answers is not "what kind of query is this" but **"will the cheap
model get this one wrong?"** -- a binary label taken from measured outcomes rather than from a
taxonomy. The policy is then: run the cheap model by default, escalate when the predictor says
the cheap model is about to fail *and* the expensive one would not.

Everything here is fit on ``data/outcomes/cheap_tier.jsonl`` and ``data/corpus/`` -- our own
runs on the audited external corpus. No RouterArena prompt, label or outcome is read.

Two constraints shape the model, both inherited from where it has to run:

* **Pure Python throughout.** It executes inside RouterArena's harness, which has no embedding
  backend and no network. So: hashed bag-of-words features and a linear model trained by sparse
  SGD, exported as a weight dict. The feature vectors are ~100 non-zeros in 2,048 buckets, so
  sparse updates beat a dense matrix product anyway and the dependency disappears.
* **Recall at a false-positive budget, not accuracy.** The classes are ~10/90, so an
  accuracy-optimal model predicts "never escalate" and scores 90%. The operating point that
  matters is how much of the *helps* set we catch per unit of wasted escalation.

Usage::

    python scripts/routerarena/escalation.py table  --cheap <m> --expensive <m>
    python scripts/routerarena/escalation.py fit    --cheap <m> --expensive <m> --seeds 30
    python scripts/routerarena/escalation.py ablate --cheap <m> --expensive <m>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

CORPUS = REPO / "data" / "corpus"
import os as _os

# Selectable so the Track F rebuild can be evaluated without editing the module. Defaults to
# the original sweep so every earlier number in this track stays reproducible.
OUTCOMES = REPO / "data" / "outcomes" / _os.environ.get("RA_OUTCOMES", "cheap_tier.jsonl")

import cost_predictability  # noqa: E402
import export_policy  # noqa: E402

N_BUCKETS = export_policy.N_BUCKETS


# ----------------------------------------------------------------------------------------
# E4.1 -- the training table
# ----------------------------------------------------------------------------------------


def load_external() -> tuple[dict, dict, dict, list[str]]:
    """(prompt_by_hash, cluster_by_hash, correct[hash][model], measured_models)."""
    prompts, clusters = {}, {}
    for path in sorted(CORPUS.glob("*.jsonl")):
        for line in path.open():
            it = json.loads(line)
            h = hashlib.sha256(it["prompt"].encode()).hexdigest()[:16]
            prompts[h] = it["prompt"]
            clusters[h] = it["cluster"]

    correct: dict[str, dict[str, float]] = defaultdict(dict)
    cost: dict[str, dict[str, float]] = defaultdict(dict)
    for line in OUTCOMES.open():
        r = json.loads(line)
        if r.get("error"):
            continue
        correct[r["prompt_hash"]][r["model"]] = r["correct"]
        cost[r["prompt_hash"]][r["model"]] = r["cost_usd"]
    models = sorted({m for cells in correct.values() for m in cells})
    return prompts, clusters, {"correct": correct, "cost": cost}, models


def build_table(cheap: str, expensive: str) -> list[dict]:
    """One row per item: prompt, and whether escalating would have helped.

    ``label = 1`` means the cheap model got it wrong *and* the expensive one got it right --
    the only case where escalation buys anything. Escalating when both are right wastes money;
    escalating when both are wrong wastes money and fixes nothing; escalating when only the
    cheap model was right actively loses a point. All three are label 0, deliberately, because
    the predictor's job is to find the case that pays and avoid the rest without distinguishing
    among them.
    """
    prompts, clusters, tab, models = load_external()
    correct = tab["correct"]
    rows = []
    for h, cells in correct.items():
        if cheap not in cells or expensive not in cells or h not in prompts:
            continue
        rows.append({
            "prompt_hash": h,
            "prompt": prompts[h],
            "cluster": clusters.get(h, ""),
            "cheap_correct": cells[cheap],
            "expensive_correct": cells[expensive],
            "label": int(cells[cheap] < 1.0 and cells[expensive] >= 1.0),
            "would_hurt": int(cells[cheap] >= 1.0 and cells[expensive] < 1.0),
        })
    return rows


# ----------------------------------------------------------------------------------------
# E4.2 -- the model
# ----------------------------------------------------------------------------------------


def featurise(text: str) -> dict[int, float]:
    """The exact feature function that ships in the router."""
    return export_policy.features(text)


def train_logreg(
    rows: list[dict],
    l2: float = 5e-3,
    epochs: int = 25,
    lr: float = 0.5,
    seed: int = 0,
    featuriser=None,
) -> tuple[dict[int, float], float]:
    """L2-regularised logistic regression over hashed bag-of-words, sparse SGD.

    Pure Python on purpose. The feature vectors are ~100 non-zeros out of 2,048 buckets, so a
    sparse update is cheaper than a dense matrix product anyway, and it keeps the whole path --
    training *and* the scoring that has to run inside RouterArena's harness -- free of
    dependencies. Weights come back as a dict, which is what the artifact ships.

    Positives run ~10%, so an unweighted fit converges on "never escalate": 90% accurate and
    worth exactly nothing. Each positive is therefore weighted by the negative/positive ratio.

    ``l2`` defaults to 5e-3 rather than something token. At 1e-4 this model reaches **99.4%
    recall at a 15% false-positive budget in-sample and 33% held out** -- 2,048 hashed buckets
    against ~2,600 training rows memorises rather than generalises. Sweeping the penalty buys
    back about six points of held-out recall; the features are not the binding constraint, the
    sample size is.
    """
    fx = featuriser or (lambda r: featurise(r["prompt"]))
    feats = [fx(r) for r in rows]
    ys = [float(r["label"]) for r in rows]
    n_pos = sum(ys)
    pos_weight = (len(ys) - n_pos) / max(n_pos, 1.0)

    w: dict[int, float] = {}
    b = 0.0
    order = list(range(len(rows)))
    rng = random.Random(seed)
    decay = 1.0 - l2

    for epoch in range(epochs):
        rng.shuffle(order)
        step = lr / (1.0 + epoch)
        for i in order:
            f = feats[i]
            if not f:
                continue
            z = b + sum(w.get(k, 0.0) * v for k, v in f.items())
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            g = (p - ys[i]) * (pos_weight if ys[i] > 0 else 1.0) * step
            if g:
                for k, v in f.items():
                    w[k] = w.get(k, 0.0) * decay - g * v
                b -= g
    return w, b


def predict_scores(rows: list[dict], w: dict[int, float], b: float) -> list[float]:
    return [
        b + sum(w.get(k, 0.0) * v for k, v in featurise(r["prompt"]).items())
        for r in rows
    ]


def recall_at_fp(rows: list[dict], scores: list[float], fp_budget: float) -> tuple[float, float]:
    """Recall at the highest threshold whose *actual* false-positive rate stays within budget.

    Indexing into the sorted negatives -- ``negs[int(len(negs) * budget)]`` -- looks equivalent
    and is not, whenever scores tie. A coarse scorer has few distinct values, so a whole block
    of negatives sits exactly at the threshold and all of them get admitted: a 13-cell cluster
    prior asked for 15% delivers 19.1%, and a 4-cell prompt-length prior asked for 5% delivers
    **61.8%** while reporting a triumphant 74.7% recall. Two scorers compared at their
    *requested* budgets are then not being compared at all, and the coarser one wins by
    cheating on the axis nobody printed.

    So: walk the distinct thresholds downward and stop before the budget is breached. Returns
    (recall, actual_fp), and actual_fp is now a promise rather than a hope.
    """
    negs = sorted((s for s, r in zip(scores, rows) if not r["label"]), reverse=True)
    pos = [s for s, r in zip(scores, rows) if r["label"]]
    if not negs or not pos:
        return 0.0, 0.0
    best = (0.0, 0.0)
    for thr in sorted(set(scores), reverse=True):
        fp = sum(1 for s in negs if s >= thr) / len(negs)
        if fp > fp_budget:
            break
        best = (sum(1 for s in pos if s >= thr) / len(pos), fp)
    return best


def split_rows(rows: list[dict], seed: int, frac: float = 0.5):
    rng = random.Random(seed)
    shuf = rows[:]
    rng.shuffle(shuf)
    k = int(len(shuf) * frac)
    return shuf[:k], shuf[k:]


# ----------------------------------------------------------------------------------------
# E4.2b -- stacking the cluster prior on the text model
# ----------------------------------------------------------------------------------------


def cluster_logodds(
    rows: list[dict], cluster_of: dict[str, str], prior_strength: float = 20.0
) -> tuple[dict[str, float], float]:
    """Smoothed log-odds of "escalation helps", per cluster, from training rows only.

    E1.3 found the clusters carry almost nothing about *which model wins* (0.078 normalised
    MI). They carry considerably more about *whether the cheap model will fail*, which is a
    different and easier question: on external data the rate runs from 2.8% (`science_qa`) to
    25.8% (`table_numeric`), a 9x spread.

    Smoothed toward the global rate with a Beta-style pseudo-count, so a cluster with a handful
    of training rows cannot assert a confident prior on the strength of noise.
    """
    total = collections_counter(rows, cluster_of)
    global_rate = sum(r["label"] for r in rows) / max(len(rows), 1)
    out = {}
    for cl, (pos, n) in total.items():
        rate = (pos + prior_strength * global_rate) / (n + prior_strength)
        rate = min(max(rate, 1e-4), 1 - 1e-4)
        out[cl] = math.log(rate / (1 - rate))
    g = min(max(global_rate, 1e-4), 1 - 1e-4)
    return out, math.log(g / (1 - g))


def collections_counter(rows: list[dict], cluster_of: dict[str, str]):
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for r in rows:
        cl = cluster_of[r["prompt_hash"]]
        agg[cl][0] += r["label"]
        agg[cl][1] += 1
    return {k: (v[0], v[1]) for k, v in agg.items()}


def predicted_clusters(train: list[dict], every: list[dict]) -> dict[str, str]:
    """Cluster for each row, from a classifier fit on the training half only.

    The cluster has to come from the classifier, not from the corpus label: inside
    RouterArena's harness there is no ground-truth cluster to look up, so evaluating with the
    true label would measure a router we cannot ship. The classifier runs ~84% held-out, and
    that error is part of what the stack has to survive.
    """
    centroids = export_policy.fit_centroids([(r["prompt"], r["cluster"]) for r in train])
    return {
        r["prompt_hash"]: (export_policy.classify(r["prompt"], centroids)[0] or "")
        for r in every
    }


def stacked_scores(
    train: list[dict],
    test: list[dict],
    lam_prior: float,
    l2: float = 5e-3,
    epochs: int = 25,
    seed: int = 0,
) -> list[float]:
    """z_text + lam_prior * cluster log-odds, with the cluster predicted, not looked up."""
    cluster_of = predicted_clusters(train, train + test)
    w, b = train_logreg(train, l2=l2, epochs=epochs, seed=seed)
    z = predict_scores(test, w, b)
    lo, fallback = cluster_logodds(train, cluster_of)
    return [
        zi + lam_prior * lo.get(cluster_of[r["prompt_hash"]], fallback)
        for zi, r in zip(z, test)
    ]


def prior_only_scores(train: list[dict], test: list[dict]) -> list[float]:
    """The cluster prior with no text model at all -- the bar the stack has to clear."""
    cluster_of = predicted_clusters(train, train + test)
    lo, fallback = cluster_logodds(train, cluster_of)
    return [lo.get(cluster_of[r["prompt_hash"]], fallback) for r in test]


def feature_stacked_scores(
    train: list[dict], test: list[dict], l2: float = 5e-3, epochs: int = 25, seed: int = 0
) -> list[float]:
    """Variant (a): the prior as one more input feature, fitted rather than blended.

    Included so the choice between blending and fitting rests on measurement. With 2,048
    buckets already overfitting ~2,600 rows, handing the optimiser one more direction to
    exploit is the thing to check, not assume.
    """
    cluster_of = predicted_clusters(train, train + test)
    lo, fallback = cluster_logodds(train, cluster_of)
    prior_bucket = N_BUCKETS  # one slot past the hashed space, so it cannot collide

    def feats(row: dict) -> dict[int, float]:
        f = dict(featurise(row["prompt"]))
        f[prior_bucket] = lo.get(cluster_of[row["prompt_hash"]], fallback)
        return f

    w, b = train_logreg(train, l2=l2, epochs=epochs, seed=seed, featuriser=feats)
    return [b + sum(w.get(k, 0.0) * v for k, v in feats(r).items()) for r in test]


# ----------------------------------------------------------------------------------------


def cmd_table(args) -> int:
    rows = build_table(args.cheap, args.expensive)
    pos = sum(r["label"] for r in rows)
    hurt = sum(r["would_hurt"] for r in rows)
    print(f"pair: {args.cheap.split('/')[-1]} -> {args.expensive.split('/')[-1]}")
    print(f"  items                 {len(rows)}")
    print(f"  positives (helps)     {pos}   base rate {pos / len(rows) * 100:.1f}%")
    print(f"  escalation would hurt {hurt}   ({hurt / len(rows) * 100:.1f}%)")
    print(f"  net if we escalated everything: {pos - hurt:+d} queries")
    print(f"\nACCEPT (>=400 positives): {'PASS' if pos >= 400 else 'FAIL'}")
    print("  sub_10's own base rate is 11.0% (89/809); a wildly different external base rate")
    print("  would itself be a transfer warning.")
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in rows))
        print(f"\nwrote {args.out}")
    return 0 if pos >= 400 else 1


def cmd_fit(args) -> int:
    rows = build_table(args.cheap, args.expensive)
    print(f"pair: {args.cheap.split('/')[-1]} -> {args.expensive.split('/')[-1]}, "
          f"{len(rows)} items, {sum(r['label'] for r in rows)} positives\n")
    print(f"held-out over {args.seeds} random 50/50 splits of the EXTERNAL corpus:")
    print(f"{'fp budget':>11}{'recall':>18}{'actual fp':>12}")
    ok = False
    for fp in (0.05, 0.15, 0.30):
        recalls, fps = [], []
        for seed in range(args.seeds):
            tr, te = split_rows(rows, seed)
            w, b = train_logreg(tr)
            s = predict_scores(te, w, b)
            r, f = recall_at_fp(te, s, fp)
            recalls.append(r)
            fps.append(f)
        lo, hi = min(recalls), max(recalls)
        mean = statistics.mean(recalls)
        print(f"{fp * 100:>10.0f}%{f'{mean * 100:.1f}% ({lo * 100:.0f}-{hi * 100:.0f})':>18}"
              f"{statistics.mean(fps) * 100:>11.1f}%")
        if fp <= 0.15 and mean >= 0.60:
            ok = True
    print(f"\nACCEPT (>=60% recall at <=15% FP): {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_ablate(args) -> int:
    """E4.3 -- is the predictor better than escalating at random at the same rate?"""
    rows = build_table(args.cheap, args.expensive)
    print(f"pair: {args.cheap.split('/')[-1]} -> {args.expensive.split('/')[-1]}\n")
    print(f"{'strategy':<34}{'net queries won':>18}{'escalation rate':>18}")

    results = {}
    for fp in (0.15,):
        model_net, rand_net, rates = [], [], []
        for seed in range(args.seeds):
            tr, te = split_rows(rows, seed)
            w, b = train_logreg(tr)
            s = predict_scores(te, w, b)
            negs = sorted((x for x, r in zip(s, te) if not r["label"]), reverse=True)
            thr = negs[max(0, min(len(negs) - 1, int(len(negs) * fp)))]
            chosen = [r for x, r in zip(s, te) if x >= thr]
            model_net.append(sum(r["label"] for r in chosen) - sum(r["would_hurt"] for r in chosen))
            rate = len(chosen) / len(te)
            rates.append(rate)

            rng = random.Random(1000 + seed)
            rnd = rng.sample(te, int(len(te) * rate))
            rand_net.append(sum(r["label"] for r in rnd) - sum(r["would_hurt"] for r in rnd))
        results["model"] = statistics.mean(model_net)
        results["random"] = statistics.mean(rand_net)
        results["rate"] = statistics.mean(rates)
        print(f"{'predictor @15% FP':<34}{results['model']:>18.1f}{results['rate'] * 100:>17.1f}%")
        print(f"{'random at the same rate':<34}{results['random']:>18.1f}"
              f"{results['rate'] * 100:>17.1f}%")
        print(f"{'escalate everything':<34}"
              f"{sum(r['label'] for r in rows) - sum(r['would_hurt'] for r in rows):>18.1f}"
              f"{100.0:>17.1f}%")

    lift = results["model"] - results["random"]
    print(f"\nlift over matched-rate random: {lift:+.1f} queries per held-out half")
    print("ACCEPT (predictor beats matched-rate random): "
          f"{'PASS' if lift > 0 else 'FAIL -- ship always-cheap instead'}")
    return 0 if lift > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("table", cmd_table), ("fit", cmd_fit), ("ablate", cmd_ablate)):
        p = sp.add_parser(name)
        p.add_argument("--cheap", required=True)
        p.add_argument("--expensive", required=True)
        p.add_argument("--seeds", type=int, default=10)
        p.add_argument("--out")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
