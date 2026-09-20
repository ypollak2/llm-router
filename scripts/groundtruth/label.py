#!/usr/bin/env python3
"""Derive `cheapest_acceptable_model` from the outcome matrix.

    python3 scripts/groundtruth/label.py --version v1 --split tune

The label is the cheapest tier whose verifier accepted:

    local=Fail  cheap=Pass  mid=Pass  premium=Pass  ->  cheap

and it is defined by the assertions alone. What the router chose does not
appear anywhere in this file — the router is the thing being measured, so a
label derived from it would be circular.

Three outcomes get their own bucket rather than a label, because pretending
otherwise is how a dataset quietly starts lying:

    none-acceptable   every tier failed. The task may be too hard, or the
                      verifier may be wrong. Either way it carries no
                      information about routing and is excluded from the
                      labelled set.
    all-acceptable    every tier passed, including the cheapest. A real label
                      ("local"), but flagged: a split made only of these
                      cannot distinguish any router from always-cheapest.
    non-monotonic     a cheaper tier passed where a more expensive one failed.
                      Usually noise at --samples 1, sometimes a genuinely
                      badly-specified task. Reported, never silently smoothed.
    ambiguous-below-  a tier cheaper than the cheapest PASS could not be graded.
    cheapest-pass     The true label may be lower than the apparent one, so no
                      label is emitted. Calling the ambiguous cell a FAIL would
                      bias every such label upward.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import dataset as ds  # noqa: E402
from groundtruth.run_matrix import TIERS  # noqa: E402

NONE_ACCEPTABLE = "none-acceptable"
AMBIGUOUS_BELOW_PASS = "ambiguous-below-cheapest-pass"


def _outcome_of(cell_entry: dict) -> str:
    """Read a three-state outcome, tolerating the older boolean shape."""
    if "outcome" in cell_entry:
        return str(cell_entry["outcome"])
    return ds.PASS if cell_entry.get("accepted") else ds.FAIL


def derive(matrix: dict, tier_order: list[str]) -> tuple[dict, dict]:
    """Return (labels, diagnostics).

    A tier is only treated as acceptable on an explicit PASS. AMBIGUOUS never
    counts as acceptance and never counts as failure — a task with an ambiguous
    cell *below* its cheapest pass cannot be labelled at all, because the
    cheaper tier might well have been acceptable and nobody knows. Resolving
    that by assuming FAIL would bias every label upward, which is the direction
    that quietly makes the router look better than it is.
    """
    labels: dict[str, dict] = {}
    non_monotonic: list[str] = []
    none_acceptable: list[str] = []
    all_acceptable: list[str] = []
    ambiguous_blocked: list[str] = []
    mixed_methods: list[str] = []

    for task_id, cell in matrix.items():
        present = [t for t in tier_order if t in cell]
        outcomes = {t: _outcome_of(cell[t]) for t in present}
        accepted = [t for t in present if outcomes[t] == ds.PASS]
        ambiguous = [t for t in present if outcomes[t] == ds.AMBIGUOUS]

        # Never pool a judge verdict with a deterministic one in a single label.
        methods = {cell[t].get("verification_type") for t in present
                   if cell[t].get("verification_type")}
        if methods & ds.SUBJECTIVE_METHODS and methods & ds.DETERMINISTIC_METHODS:
            mixed_methods.append(task_id)

        if not accepted:
            none_acceptable.append(task_id)
            labels[task_id] = {
                "cheapest_acceptable_model": None,
                "status": NONE_ACCEPTABLE,
                "accepted_tiers": [],
                "ambiguous_tiers": ambiguous,
            }
            continue

        cheapest = min(accepted, key=lambda t: TIERS[t]["order"])
        cheaper_unknown = [t for t in ambiguous
                           if TIERS[t]["order"] < TIERS[cheapest]["order"]]
        if cheaper_unknown:
            ambiguous_blocked.append(task_id)
            labels[task_id] = {
                "cheapest_acceptable_model": None,
                "status": AMBIGUOUS_BELOW_PASS,
                "accepted_tiers": accepted,
                "ambiguous_tiers": ambiguous,
                "note": (f"{cheaper_unknown} could not be graded and are cheaper "
                         f"than {cheapest}; the true label may be lower"),
            }
            continue

        # Monotonic would mean: once a tier passes, every costlier tier passes.
        idx = present.index(cheapest)
        if any(outcomes[t] == ds.FAIL for t in present[idx:]):
            non_monotonic.append(task_id)
        if len(accepted) == len(present):
            all_acceptable.append(task_id)

        labels[task_id] = {
            "cheapest_acceptable_model": cheapest,
            "cheapest_acceptable_model_id": cell[cheapest]["model"],
            "status": "labelled",
            "accepted_tiers": accepted,
            "ambiguous_tiers": ambiguous,
            "monotonic": task_id not in non_monotonic,
            "verification_type": cell[cheapest].get("verification_type"),
            "verifier": cell[cheapest].get("verifier"),
            "confidence": cell[cheapest].get("confidence"),
        }

    diagnostics = {
        "non_monotonic": non_monotonic,
        "none_acceptable": none_acceptable,
        "all_acceptable": all_acceptable,
        "ambiguous_below_pass": ambiguous_blocked,
        "mixed_verification_methods": mixed_methods,
    }
    return labels, diagnostics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--split", choices=("tune", "test"), default="tune")
    ap.add_argument("--root", type=Path, default=Path("data/groundtruth"))
    ap.add_argument("--outcomes", default="")
    args = ap.parse_args()

    root = args.root / args.version
    src = Path(args.outcomes) if args.outcomes else root / f"outcomes.{args.split}.json"
    if not src.exists():
        raise SystemExit(f"no outcome matrix at {src} — run run_matrix.py first")

    payload = json.loads(src.read_text(encoding="utf-8"))
    matrix = payload["matrix"]
    if not matrix:
        raise SystemExit("outcome matrix is empty — nothing to label")

    tier_order = sorted(payload["tiers"], key=lambda t: TIERS[t]["order"])
    labels, diag = derive(matrix, tier_order)

    labelled = {k: v for k, v in labels.items() if v["status"] == "labelled"}
    dist = Counter(v["cheapest_acceptable_model"] for v in labelled.values())

    out = root / f"labels.{args.split}.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for task_id in sorted(labels):
            fh.write(json.dumps({"task_id": task_id, **labels[task_id]},
                                sort_keys=True) + "\n")

    report = {
        "dataset_version": args.version,
        "split": args.split,
        "source_matrix": str(src),
        "n_tasks": len(labels),
        "n_labelled": len(labelled),
        "n_none_acceptable": len(diag["none_acceptable"]),
        "n_all_acceptable": len(diag["all_acceptable"]),
        "n_non_monotonic": len(diag["non_monotonic"]),
        "n_ambiguous_below_pass": len(diag["ambiguous_below_pass"]),
        "n_mixed_verification_methods": len(diag["mixed_verification_methods"]),
        "label_distribution": dict(dist),
        "diagnostics": diag,
    }
    (root / f"labels.{args.split}.summary.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"tasks in matrix        {len(labels)}")
    print(f"labelled               {len(labelled)}")
    print(f"  distribution         {dict(dist)}")
    print(f"none acceptable        {len(diag['none_acceptable'])}  (excluded from labels)")
    print(f"ambiguous below pass   {len(diag['ambiguous_below_pass'])}  (excluded — true label unknown)")
    if diag["mixed_verification_methods"]:
        print(f"MIXED METHODS          {len(diag['mixed_verification_methods'])}  "
              f"deterministic and judge verdicts in one task — do not pool these")
    print(f"all tiers acceptable   {len(diag['all_acceptable'])}")
    print(f"non-monotonic          {len(diag['non_monotonic'])}")
    if diag["non_monotonic"]:
        print(f"    {diag['non_monotonic'][:8]}")
    print(f"\nwrote {out}")

    if labelled and len(diag["all_acceptable"]) == len(labelled):
        print("\nWARNING: every labelled task is satisfied by the cheapest tier.\n"
              "This split cannot distinguish any router from always-cheapest. "
              "Add harder tasks before measuring anything on it.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
