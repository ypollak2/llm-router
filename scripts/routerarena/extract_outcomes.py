#!/usr/bin/env python3
"""Extract a cross-model outcome table from our graded RouterArena submission.

RouterArena's evaluator re-runs every model in a router's declared pool over the sub_10
subset to compute the "Optimal Selection" metric. Those rows come back in the graded
prediction file tagged ``for_optimality: True`` -- which means the merged file contains,
for free, a dense (query x model) table of real graded outcomes.

That table is an offline evaluation harness: any routing policy over the declared pool can
be scored on it exactly, with no inference and no spend.

Usage::

    python scripts/routerarena/extract_outcomes.py                 # fetch + extract
    python scripts/routerarena/extract_outcomes.py --graded PATH   # use a local copy

Writes ``scripts/routerarena/data/outcomes_sub10.json``.

INTEGRITY NOTE: this artifact is RouterArena data. It is used to *evaluate* policies and to
*diagnose* our own past submission. It must never be used to fit a router parameter --
see docs/routerarena-number-one-plan.md section 3.3.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

GRADED_URL = (
    "https://raw.githubusercontent.com/RouteWorks/RouterArena/main/"
    "router_inference/predictions/llm-router.json"
)
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DEFAULT_OUT = DATA / "outcomes_sub10.json"
CACHE = DATA / "_graded_llm-router.json"


def load_graded(path: Path | None) -> list[dict]:
    """Return the graded prediction rows, fetching and caching them if needed."""
    if path is not None:
        return json.loads(path.read_text())
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"fetching {GRADED_URL} ...", file=sys.stderr)
    with urllib.request.urlopen(GRADED_URL, timeout=300) as resp:
        blob = resp.read()
    CACHE.write_bytes(blob)
    return json.loads(blob)


def family_of(global_index: str) -> str:
    """RouterArena's ``global index`` is ``<dataset>_<n>``; strip the counter."""
    return global_index.rsplit("_", 1)[0]


def extract(rows: list[dict]) -> dict:
    """Build the cross-model outcome table plus the routed baseline, from graded rows."""
    routed: dict[str, dict] = {}
    outcomes: dict[str, dict[str, dict]] = defaultdict(dict)

    for row in rows:
        gid = row["global index"]
        model = row["prediction"]
        cell = {"accuracy": float(row["accuracy"]), "cost": float(row["cost"])}
        outcomes[gid][model] = cell
        if str(row.get("for_optimality")) == "False":
            routed[gid] = {"model": model, "prompt": row["prompt"], **cell}

    models = sorted({m for cells in outcomes.values() for m in cells})
    # Keep only queries where every pool model was actually run -- the dense sub-table.
    dense = [gid for gid, cells in outcomes.items() if len(cells) == len(models)]
    dense.sort()

    return {
        "source": GRADED_URL,
        "models": models,
        "n_queries": len(dense),
        "queries": [
            {
                "global_index": gid,
                "family": family_of(gid),
                "prompt": routed[gid]["prompt"],
                "routed_model": routed[gid]["model"],
                "outcomes": {m: outcomes[gid][m] for m in models},
            }
            for gid in dense
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graded", type=Path, help="local copy of the graded prediction file")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    rows = load_graded(args.graded)
    table = extract(rows)

    if not table["queries"]:
        print("no dense cross-model rows found -- is this the merged graded file?", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, indent=1))

    fams = {q["family"] for q in table["queries"]}
    print(f"models     : {len(table['models'])}")
    for m in table["models"]:
        print(f"             {m}")
    print(f"queries    : {table['n_queries']} (dense across all models)")
    print(f"families   : {len(fams)}")
    print(f"written    : {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
