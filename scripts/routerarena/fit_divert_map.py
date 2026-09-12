#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fit a frozen task-type routing policy from external evidence only.

The policy has two halves, and both are fit here so that nothing downstream needs to look at
RouterArena:

1. **A task-type classifier.** At inference the router sees a bare prompt -- no dataset label,
   no cluster tag. So it has to infer the task type from text. Trained on the 29,693 labelled
   items of the external corpus with a multinomial Naive Bayes over hashed word features
   (pure Python; the repo has no sklearn, and this task -- code vs translation vs NLI -- is
   lexically separable enough not to need one).

2. **A divert map.** For each task type, which of the two models to send it to, and only where
   the measured accuracy gap is large enough to pay for the cost difference. The gap threshold
   is chosen on a held-out half of the external measurements, never on RouterArena.

The output is a JSON policy that ``apply_divert_router.py`` can execute with no further
fitting. Keeping fit and apply in separate files is what lets ``lint_ra_leakage.py`` prove no
file both writes a policy and reads RouterArena data.

Usage::

    python scripts/routerarena/fit_divert_map.py \\
        --outcomes data/outcomes/pair_boxed.jsonl \\
        --out data/policy/divert_policy.json
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import re
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "data" / "corpus"
sys.path.insert(0, str(Path(__file__).resolve().parent))

WORD = re.compile(r"[a-z0-9_]+")
NBUCKETS = 1 << 15
LO, HI, BETA = 0.0044, 200.0, 0.1


def arena(acc: float, cost_per_1k: float) -> float:
    c = min(max(cost_per_1k, LO), HI)
    C = (math.log2(HI) - math.log2(c)) / (math.log2(HI) - math.log2(LO))
    return (1 + BETA) * acc * C / (BETA * acc + C)


def featurize(text: str) -> list[int]:
    """Hashed unigrams over the first 400 words. Structure lives at the start of a prompt --
    a code stub, a table header, a 'Premise:' line -- so the tail adds cost, not signal.

    ``crc32`` rather than ``hash``: string hashing is salted per interpreter process, so a
    policy fit in one run would bucket words differently when applied in the next, silently
    scrambling every feature. A stable hash is what makes the frozen policy actually frozen.
    """
    toks = WORD.findall(text.lower())[:400]
    return [zlib.crc32(t.encode()) % NBUCKETS for t in toks]


class NaiveBayes:
    """Multinomial NB with Laplace smoothing. Small, deterministic, no dependencies."""

    def __init__(self) -> None:
        self.classes: list[str] = []
        self.logprior: dict[str, float] = {}
        self.counts: dict[str, dict[int, int]] = {}
        self.total: dict[str, int] = {}

    def fit(self, docs: list[tuple[list[int], str]]) -> None:
        self.counts = collections.defaultdict(lambda: collections.defaultdict(int))
        self.total = collections.defaultdict(int)
        ndoc = collections.Counter(label for _, label in docs)
        for feats, label in docs:
            for f in feats:
                self.counts[label][f] += 1
                self.total[label] += 1
        self.classes = sorted(ndoc)
        n = sum(ndoc.values())
        self.logprior = {c: math.log(ndoc[c] / n) for c in self.classes}

    def predict(self, feats: list[int]) -> str:
        best, best_score = self.classes[0], -1e18
        for c in self.classes:
            cnt, tot = self.counts[c], self.total[c] + NBUCKETS
            s = self.logprior[c]
            for f in feats:
                s += math.log((cnt.get(f, 0) + 1) / tot)
            if s > best_score:
                best, best_score = c, s
        return best

    def to_json(self) -> dict:
        return {"classes": self.classes, "logprior": self.logprior,
                "total": {c: self.total[c] for c in self.classes},
                "counts": {c: {str(k): v for k, v in self.counts[c].items()}
                           for c in self.classes},
                "nbuckets": NBUCKETS}


def load_corpus_labelled() -> list[tuple[str, str]]:
    out = []
    for path in sorted(CORPUS.glob("*.jsonl")):
        if path.name.startswith("_"):
            continue
        for line in path.open():
            it = json.loads(line)
            out.append((it["prompt"], it["cluster"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outcomes", default="data/outcomes/pair_boxed.jsonl")
    ap.add_argument("--out", default="data/policy/divert_policy.json")
    ap.add_argument("--base", default="google/gemini-3-flash-preview",
                    help="the model everything defaults to")
    ap.add_argument("--alt", default="stealth/ox-alpha",
                    help="the model a task type may be diverted to")
    ap.add_argument("--alt-cost-per-1k", type=float, default=0.0,
                    help="price for the alt model; free preview endpoints are 0")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    # ---- 1. task-type classifier -----------------------------------------------------
    labelled = load_corpus_labelled()
    rng = random.Random(args.seed)
    rng.shuffle(labelled)
    cut = int(len(labelled) * 0.85)
    train = [(featurize(p), c) for p, c in labelled[:cut]]
    test = [(featurize(p), c) for p, c in labelled[cut:]]
    nb = NaiveBayes()
    nb.fit(train)
    hits = sum(1 for f, c in test if nb.predict(f) == c)
    clf_acc = hits / len(test)
    print(f"task-type classifier: {clf_acc * 100:.2f}% on {len(test)} held-out external items")

    # ---- 2. divert map ---------------------------------------------------------------
    rows = [json.loads(line) for line in Path(args.outcomes).open()]
    by = collections.defaultdict(dict)
    for r in rows:
        if not r.get("error"):
            by[r["idx"]][r["model"]] = r
    paired = {i: v for i, v in by.items() if args.base in v and args.alt in v}
    print(f"paired external measurements: {len(paired)}")
    if len(paired) < 200:
        print("too few paired measurements to fit a map")
        return 1

    idxs = sorted(paired)
    rng.shuffle(idxs)
    half = len(idxs) // 2
    fit_ids, hold_ids = idxs[:half], idxs[half:]

    def per_cluster(ids):
        d = collections.defaultdict(lambda: [0, 0.0, 0.0])
        for i in ids:
            v = paired[i]
            c = v[args.base]["cluster"]
            d[c][0] += 1
            d[c][1] += v[args.base]["correct"]
            d[c][2] += v[args.alt]["correct"]
        return d

    fit_d = per_cluster(fit_ids)
    base_cost_1k = (sum(v[args.base].get("cost_usd", 0) or 0 for v in paired.values())
                    / max(len(paired), 1) * 1000) or None

    # Sweep the gap threshold on the fit half, score each candidate on the held-out half.
    # Both halves are external, so this is model selection, not benchmark tuning.
    best = None
    for thr in [x / 100 for x in range(0, 31)]:
        divert = {c for c, (n, b, a) in fit_d.items() if n >= 20 and (a - b) / n >= thr}
        accs = []
        for i in hold_ids:
            v = paired[i]
            c = v[args.base]["cluster"]
            accs.append(v[args.alt]["correct"] if c in divert else v[args.base]["correct"])
        acc = sum(accs) / len(accs)
        if best is None or acc > best[1]:
            best = (thr, acc, divert)
    thr, hold_acc, divert = best

    base_only = sum(paired[i][args.base]["correct"] for i in hold_ids) / len(hold_ids)
    alt_only = sum(paired[i][args.alt]["correct"] for i in hold_ids) / len(hold_ids)
    print(f"\nheld-out external (n={len(hold_ids)}):")
    print(f"  {args.base:<42}{base_only * 100:>7.2f}%")
    print(f"  {args.alt:<42}{alt_only * 100:>7.2f}%")
    print(f"  divert map (threshold {thr:.2f})            {hold_acc * 100:>7.2f}%"
          f"   {(hold_acc - max(base_only, alt_only)) * 100:+.2f} pts")
    print(f"  diverted task types: {sorted(divert) or '(none)'}")

    policy = {
        "base_model": args.base,
        "alt_model": args.alt,
        "divert_clusters": sorted(divert),
        "gap_threshold": thr,
        "fit_evidence": {
            "source": args.outcomes,
            "paired_items": len(paired),
            "holdout_accuracy": hold_acc,
            "base_only_holdout": base_only,
            "alt_only_holdout": alt_only,
            "classifier_heldout_accuracy": clf_acc,
        },
        "classifier": nb.to_json(),
        "provenance": "Fit exclusively on the external corpus and external outcome "
                      "measurements. No RouterArena prompt, label or outcome was read.",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(policy))
    print(f"\nfrozen policy -> {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
