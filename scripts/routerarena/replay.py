#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Replay any routing policy over the captured RouterArena sub_10 outcome matrix, offline.

We hold a complete 809 x 8 outcome matrix: for every sub_10 query, what each of the eight
candidate models scored and what it cost. That makes scoring a policy free -- no API calls, no
harness, under a second -- which is exactly as dangerous as it is useful. A tool that can score
a thousand policies an hour will find one that scores well on 809 queries by luck.

So this module **measures**; it must never **select**. The leakage protocol in
``docs/routerarena-track-e-plan.md`` splits sub_10 by hash into ``dev`` and ``sealed``: all
iteration reads ``dev``, and ``sealed`` requires ``--i-am-freezing`` and is meant to be read
exactly once, after the policy is frozen. Every read appends to ``data/policy/peek_log.jsonl``
so the submission can state how many looks the number cost.

Usage::

    python scripts/routerarena/replay.py --policy shipped --half dev
    python scripts/routerarena/replay.py --policy oracle --half all
    python scripts/routerarena/replay.py --policy always:google/gemini-3.1-flash-lite-preview
    python scripts/routerarena/replay.py --policy pair:cheap,expensive --half dev
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
MATRIX = REPO / "data" / "ra_eval" / "sub10_matrix.json"
PEEK_LOG = REPO / "data" / "policy" / "peek_log.jsonl"

# RouterArena's Acc-Cost Arena constants (router_evaluation/compute_scores.py).
BETA, C_MAX, C_MIN = 0.1, 200.0, 0.0044

# The split. Committed here so the halves are reproducible from the repo alone; changing it
# invalidates every sealed read that came before, which is the point of writing it down.
SPLIT_SALT = "track-e-2026-08-21"
SPLIT_FILE = REPO / "data" / "ra_eval" / "split.json"

# A plain per-query hash was tried first and rejected by its own acceptance check: it put 1 of
# 14 OpenTDB Science-Computers queries in dev and 13 in sealed, 5 of 21 MMLUPro-health in dev
# and 16 in sealed, and the always-gemini baseline came out 3.34 Arena points apart across the
# halves. sub_10 has 809 queries spread over ~90 families, so families are small enough that
# independent coin flips are visibly lumpy, and every later dev-vs-sealed comparison would have
# inherited that skew as if it were signal. Stratifying within family fixes it and stays
# deterministic: order each family by hash, then alternate.


def arena_score(accuracy: float, cost_per_1k: float) -> float:
    """RouterArena's Acc-Cost Arena score."""
    cost = max(C_MIN, min(cost_per_1k, C_MAX))
    c_i = (math.log2(C_MAX) - math.log2(cost)) / (math.log2(C_MAX) - math.log2(C_MIN))
    return ((1 + BETA) * accuracy * c_i) / (BETA * accuracy + c_i)


def family(global_index: str) -> str:
    """Dataset family, e.g. ``MMLUPro_health_12`` -> ``MMLUPro_health``."""
    return global_index.rsplit("_", 1)[0]


def build_split(matrix: dict[str, Any]) -> dict[str, str]:
    """Family-stratified, deterministic dev/sealed assignment.

    Within each family, order by hash and alternate. Every family is therefore split to
    within one query, which is what keeps a dev-vs-sealed difference interpretable as a
    property of the policy rather than of the split.
    """
    by_family: dict[str, list[str]] = {}
    for gi in matrix:
        by_family.setdefault(family(gi), []).append(gi)
    assignment: dict[str, str] = {}
    for fam in sorted(by_family):
        ordered = sorted(
            by_family[fam],
            key=lambda g: hashlib.sha256(f"{SPLIT_SALT}|{g}".encode()).hexdigest(),
        )
        for i, gi in enumerate(ordered):
            assignment[gi] = "dev" if i % 2 == 0 else "sealed"
    return assignment


_split_cache: dict[str, str] | None = None


def load_split(matrix: dict[str, Any] | None = None) -> dict[str, str]:
    """The committed split, built once and then read from disk so it cannot drift."""
    global _split_cache
    if _split_cache is not None:
        return _split_cache
    if SPLIT_FILE.exists():
        _split_cache = json.loads(SPLIT_FILE.read_text())["assignment"]
        return _split_cache
    if matrix is None:
        matrix = load_matrix()
    assignment = build_split(matrix)
    SPLIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPLIT_FILE.write_text(
        json.dumps(
            {
                "_README": "Family-stratified dev/sealed split of RouterArena sub_10. "
                "Changing this file invalidates every sealed read taken before it.",
                "salt": SPLIT_SALT,
                "assignment": assignment,
            }
        )
    )
    _split_cache = assignment
    return assignment


def half_of(global_index: str) -> str:
    """Deterministic dev/sealed assignment for one query."""
    return load_split()[global_index]


def load_matrix(path: Path = MATRIX) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise SystemExit(
            f"{path} not found -- run the extraction step in E0.1 first."
        )
    return json.loads(path.read_text())["queries"]


# ----------------------------------------------------------------------------------------
# Policies. A policy maps (global_index, record) -> model name present in that record.
# ----------------------------------------------------------------------------------------


def policy_shipped(_gi: str, rec: dict) -> str:
    """What the submitted router actually chose, as recorded by the harness."""
    return rec["router_pick"]


def policy_always(model: str) -> Callable[[str, dict], str]:
    def pick(_gi: str, rec: dict) -> str:
        if model not in rec["models"]:
            raise KeyError(f"{model} not measured on {_gi}")
        return model
    return pick


def policy_oracle(_gi: str, rec: dict) -> str:
    """Cheapest model that answers correctly; cheapest overall when none does.

    This is the pool ceiling, not a shippable policy -- it reads the answer key.
    """
    correct = {m: v for m, v in rec["models"].items() if v[0] >= 1.0}
    pool = correct or rec["models"]
    return min(pool, key=lambda m: pool[m][1])


def policy_cheapest(_gi: str, rec: dict) -> str:
    return min(rec["models"], key=lambda m: rec["models"][m][1])


def policy_pair_oracle(cheap: str, expensive: str) -> Callable[[str, dict], str]:
    """Escalate exactly when it helps -- the ceiling for a two-model system."""
    def pick(_gi: str, rec: dict) -> str:
        c, e = rec["models"][cheap], rec["models"][expensive]
        return expensive if e[0] > c[0] else cheap
    return pick


def resolve_policy(spec: str) -> tuple[str, Callable[[str, dict], str]]:
    if spec == "shipped":
        return spec, policy_shipped
    if spec == "oracle":
        return spec, policy_oracle
    if spec == "cheapest":
        return spec, policy_cheapest
    if spec.startswith("always:"):
        return spec, policy_always(spec.split(":", 1)[1])
    if spec.startswith("pair:"):
        cheap, expensive = spec.split(":", 1)[1].split(",")
        return spec, policy_pair_oracle(cheap, expensive)
    if spec.startswith("picks:"):
        # A frozen policy's decisions, exported as {global_index: model}. This is how a real
        # candidate policy gets scored: it decides from prompt text elsewhere, and only its
        # decisions come here, so the matrix cannot leak into the decision.
        picks = json.loads(Path(spec.split(":", 1)[1]).read_text())
        return spec, lambda gi, _rec: picks[gi]
    raise SystemExit(f"unknown policy spec: {spec}")


# ----------------------------------------------------------------------------------------


def score(
    matrix: dict[str, dict[str, Any]], policy: Callable[[str, dict], str]
) -> dict[str, Any]:
    """Accuracy, cost and Arena score, plus RouterArena's three optimality ratios."""
    n = len(matrix)
    if n == 0:
        raise SystemExit("no queries selected")

    acc_sum = cost_sum = 0.0
    opt_hits = opt_n = 0
    opt_cost_sum = router_cost_on_opt = 0.0
    opt_acc_sum = router_acc_on_opt = 0.0

    for gi, rec in matrix.items():
        chosen = policy(gi, rec)
        a, c = rec["models"][chosen]
        acc_sum += a
        cost_sum += c

        # RouterArena defines the optimal model as the cheapest with accuracy >= 1.0, and
        # computes the three ratios only over queries where such a model exists.
        correct = {m: v for m, v in rec["models"].items() if v[0] >= 1.0}
        if not correct:
            continue
        best = min(correct, key=lambda m: correct[m][1])
        opt_n += 1
        # Opt.Sel: answered correctly AND picked the cheapest-correct model.
        opt_hits += int(a >= 1.0 and chosen == best)
        # Opt.Acc: ratio over the whole conditioned set.
        opt_acc_sum += correct[best][0]
        router_acc_on_opt += a
        # Opt.Cost: RouterArena restricts both sides to queries the router got right, so a
        # cheap-but-wrong pick cannot push the ratio above 1.0. Summing over all queries
        # instead reads ~2x too favourable (11.21 vs the harness's 6.12) -- matched here
        # deliberately rather than approximated.
        if a >= 1.0:
            opt_cost_sum += correct[best][1]
            router_cost_on_opt += c

    accuracy = acc_sum / n
    cost_per_1k = cost_sum / n * 1000
    return {
        "n_queries": n,
        "accuracy": accuracy,
        "cost_per_1k": cost_per_1k,
        "arena_score": arena_score(accuracy, cost_per_1k),
        "opt_sel": opt_hits / opt_n if opt_n else 0.0,
        "opt_cost": opt_cost_sum / router_cost_on_opt if router_cost_on_opt else 0.0,
        "opt_acc": router_acc_on_opt / opt_acc_sum if opt_acc_sum else 0.0,
        "queries_with_optimal_data": opt_n,
    }


def log_peek(half: str, policy_spec: str, result: dict[str, Any]) -> None:
    """Record the read. The count is part of the provenance, not bookkeeping."""
    PEEK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PEEK_LOG.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "half": half,
                    "policy": policy_spec,
                    "arena_score": round(result["arena_score"], 6),
                    "accuracy": round(result["accuracy"], 6),
                    "cost_per_1k": round(result["cost_per_1k"], 6),
                    "n_queries": result["n_queries"],
                }
            )
            + "\n"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--policy",
        default="shipped",
        help="shipped | oracle | cheapest | always:<model> | pair:<cheap>,<exp> | picks:<file>",
    )
    ap.add_argument("--half", choices=("dev", "sealed", "all"), default="dev")
    ap.add_argument(
        "--i-am-freezing",
        action="store_true",
        help="required to read the sealed half; the policy must already be frozen",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    ap.add_argument("--no-log", action="store_true", help="skip the peek log (tests only)")
    args = ap.parse_args()

    if args.half in ("sealed", "all") and not args.i_am_freezing:
        print(
            f"Refusing to read the '{args.half}' half without --i-am-freezing.\n"
            "The sealed half is the honest number; it is spent by looking at it. "
            "Iterate on --half dev.",
            file=sys.stderr,
        )
        return 2

    matrix = load_matrix()
    if args.half != "all":
        matrix = {k: v for k, v in matrix.items() if half_of(k) == args.half}

    spec, policy = resolve_policy(args.policy)
    result = score(matrix, policy)
    result["half"] = args.half
    result["policy"] = spec

    if not args.no_log:
        log_peek(args.half, spec, result)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"policy={spec}  half={args.half}  n={result['n_queries']}")
        print(f"  accuracy    {result['accuracy'] * 100:6.2f}%")
        print(f"  cost/1k     ${result['cost_per_1k']:.4f}")
        print(f"  ARENA       {result['arena_score'] * 100:6.2f}")
        print(
            f"  opt_sel {result['opt_sel'] * 100:5.2f}  "
            f"opt_cost {result['opt_cost'] * 100:5.2f}  "
            f"opt_acc {result['opt_acc'] * 100:5.2f}  "
            f"(n={result['queries_with_optimal_data']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
