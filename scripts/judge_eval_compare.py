#!/usr/bin/env python3
"""Combine two `judge_discrimination_eval.py --raw-out` files into a before/after table.

OPTIONAL, not part of the default test run — a small reporting helper for the
feat/judge-discrimination PR description. Usage:

    python3 scripts/judge_eval_compare.py --before /tmp/old_tune.json --after /tmp/new_tune.json
"""

from __future__ import annotations

import argparse
import json


def _metrics(results: list[dict]) -> dict:
    def scores_for(lbl: str) -> list[float]:
        return [r["judge_score"] for r in results if r["label"] == lbl and r["judge_score"] is not None]

    correct = scores_for("correct")
    wrong = scores_for("wrong")
    partial = scores_for("partial")
    ungraded = [r for r in results if r["judge_score"] is None]

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    pair_hits = 0.0
    for c in correct:
        for w in wrong:
            if c > w:
                pair_hits += 1.0
            elif c == w:
                pair_hits += 0.5
    pair_rate = pair_hits / (len(correct) * len(wrong)) if correct and wrong else None

    labelled = [(s, True) for s in correct] + [(s, False) for s in wrong]
    acc = (
        sum(1 for s, is_c in labelled if (s >= 0.5) == is_c) / len(labelled)
        if labelled else None
    )

    return {
        "n": len(results),
        "n_correct": len(correct),
        "n_wrong": len(wrong),
        "n_partial": len(partial),
        "n_ungraded": len(ungraded),
        "mean_correct": mean(correct),
        "mean_wrong": mean(wrong),
        "mean_partial": mean(partial),
        "pair_rate_auc": pair_rate,
        "threshold_0.5_accuracy": acc,
        "n_pairs": len(correct) * len(wrong),
        "n_threshold_cases": len(labelled),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    args = parser.parse_args()

    before = json.loads(open(args.before).read())
    after = json.loads(open(args.after).read())

    b = _metrics(before)
    a = _metrics(after)

    print(f"{'metric':30s} {'before':>15s} {'after':>15s}")
    for key in [
        "n", "n_correct", "n_wrong", "n_partial", "n_ungraded",
        "mean_correct", "mean_wrong", "mean_partial",
        "pair_rate_auc", "threshold_0.5_accuracy",
    ]:
        bv, av = b[key], a[key]
        bv_s = f"{bv:.3f}" if isinstance(bv, float) else str(bv)
        av_s = f"{av:.3f}" if isinstance(av, float) else str(av)
        print(f"{key:30s} {bv_s:>15s} {av_s:>15s}")
    print(f"\npair rate computed over n_pairs before={b['n_pairs']} after={a['n_pairs']}")
    print(f"threshold accuracy computed over n before={b['n_threshold_cases']} after={a['n_threshold_cases']}")


if __name__ == "__main__":
    main()
