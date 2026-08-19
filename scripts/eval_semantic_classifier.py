#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Local evaluation of the semantic classifier + reason-gate on held-out data.

This measures the levers our changes actually move — task_type classification
accuracy, coverage (non-abstain rate), and confidence calibration — on the
self-generated HELD-OUT split. It is NOT a RouterArena leaderboard score:
per bench/routerarena/clean/STATUS.md the RA number is bound by escalation economics,
and query-surface classifiers do not transfer to RA's prompt surface (measured:
MemoryTree transfer 0.024). This eval tells us whether the production classifier
is sound; it does not claim an arena gain.

Usage:
    python scripts/eval_semantic_classifier.py --holdout src/llm_router/data/semantic_holdout.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def _run(holdout_path: Path, floor: float | None) -> int:
    from llm_router import semantic_classify as sc
    from llm_router.reason_gate import gate

    if not sc.is_available():
        print("No centroid artifact loaded — build it first "
              "(scripts/build_semantic_centroids.py).", file=sys.stderr)
        return 1

    rows = [json.loads(l) for l in holdout_path.read_text().splitlines() if l.strip()]
    n = len(rows)
    correct = covered = 0
    conf_correct: list[float] = []
    conf_wrong: list[float] = []
    confusion: Counter = Counter()

    for r in rows:
        res = await sc.classify_semantic(r["prompt"], confidence_floor=floor)
        if res is None:
            continue  # abstained → falls back to regex in production
        covered += 1
        gold = r["task_type"]
        pred = res.task_type.value
        if pred == gold:
            correct += 1
            conf_correct.append(res.task_confidence)
        else:
            conf_wrong.append(res.task_confidence)
            confusion[f"{gold}→{pred}"] += 1

    cov = covered / n if n else 0.0
    acc_cov = correct / covered if covered else 0.0    # accuracy on covered
    acc_all = correct / n if n else 0.0                # accuracy over all (abstain=miss)
    mc = sum(conf_correct) / len(conf_correct) if conf_correct else 0.0
    mw = sum(conf_wrong) / len(conf_wrong) if conf_wrong else 0.0

    print("── semantic_classify (task_type) on held-out ──")
    print(f"  n={n}  coverage={cov:.1%}  accuracy(covered)={acc_cov:.1%}  accuracy(all)={acc_all:.1%}")
    print(f"  mean confidence: correct={mc:.3f}  wrong={mw:.3f}  (separation={mc - mw:+.3f})")
    if confusion:
        print("  top confusions: " + ", ".join(f"{k}×{v}" for k, v in confusion.most_common(6)))

    # Reason-gate: report fire-rate + score spread on the same prompts (no gold
    # reasoning labels in this corpus, so this is a sanity/behaviour check).
    scores = [gate(r["prompt"]).score for r in rows]
    fired = sum(1 for s in scores if s >= 0.5)
    print("── reason_gate behaviour ──")
    print(f"  fire-rate={fired}/{n} ({fired / n:.1%})  "
          f"score range=[{min(scores):.2f},{max(scores):.2f}]  mean={sum(scores)/n:.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", type=Path, required=True)
    ap.add_argument("--floor", type=float, default=None,
                    help="Override artifact confidence floor for this eval.")
    args = ap.parse_args()
    return asyncio.run(_run(args.holdout, args.floor))


if __name__ == "__main__":
    raise SystemExit(main())
