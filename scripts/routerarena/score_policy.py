#!/usr/bin/env python3
"""Score a routing policy offline against RouterArena's own graded outcomes.

Uses the dense (query x model) table built by ``extract_outcomes.py``. A policy is any
callable ``route(query) -> model_name``; this module replays it over the table and reports
accuracy, cost per 1K queries, and the Arena score, using RouterArena's exact formula.

No inference, no network, no spend -- so a policy change can be measured in milliseconds.

Run directly to print the baseline table::

    python scripts/routerarena/score_policy.py

INTEGRITY NOTE: this is a RouterArena-derived evaluation harness. Use it to *measure*
policies, never to fit one -- see docs/routerarena-number-one-plan.md section 3.3. The
selector itself must be fit on external corpora only.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

DATA = Path(__file__).resolve().parent / "data" / "outcomes_sub10.json"

# RouterArena's constants, from router_evaluation/compute_scores.py
BETA = 0.1
C_MAX = 200.0
C_MIN = 0.0044


def arena_score(cost_per_1k: float, accuracy: float, beta: float = BETA) -> float:
    """RouterArena's Acc-Cost Arena score. Verbatim port of ``compute_arena_score``."""
    c_i = (math.log2(C_MAX) - math.log2(cost_per_1k)) / (math.log2(C_MAX) - math.log2(C_MIN))
    return ((1 + beta) * accuracy * c_i) / (beta * accuracy + c_i)


class Result(NamedTuple):
    """Outcome of replaying one policy over the table."""

    name: str
    accuracy: float
    cost_per_1k: float
    score: float

    def __str__(self) -> str:
        return (
            f"{self.name:<44} acc={self.accuracy:.4f}  "
            f"${self.cost_per_1k:.4f}/1k  S={self.score * 100:6.2f}"
        )


def load_table(path: Path = DATA) -> dict[str, Any]:
    """Load the outcome table, with a pointed error if it has not been built yet."""
    if not path.exists():
        raise SystemExit(
            f"{path} not found -- run scripts/routerarena/extract_outcomes.py first"
        )
    return json.loads(path.read_text())


def score(
    name: str,
    route: Callable[[dict[str, Any]], str],
    table: dict[str, Any] | None = None,
) -> Result:
    """Replay ``route`` over every query and score the resulting selections."""
    table = table or load_table()
    queries = table["queries"]
    acc = cost = 0.0

    for query in queries:
        model = route(query)
        cell = query["outcomes"].get(model)
        if cell is None:
            raise KeyError(
                f"policy {name!r} chose {model!r}, which is not in the pool: "
                f"{sorted(query['outcomes'])}"
            )
        acc += cell["accuracy"]
        cost += cell["cost"]

    n = len(queries)
    return Result(name, acc / n, cost / n * 1000, arena_score(cost / n * 1000, acc / n))


# ---------------------------------------------------------------------------
# Reference policies -- the baselines every new policy has to be read against.
# ---------------------------------------------------------------------------


def as_submitted(query: dict[str, Any]) -> str:
    """What our merged submission actually chose for this query."""
    return query["routed_model"]


def always(model: str) -> Callable[[dict[str, Any]], str]:
    """A constant policy. The floor a real router has to clear."""
    return lambda _query: model


def oracle_best_accuracy(query: dict[str, Any]) -> str:
    """Hindsight upper bound on accuracy, ignoring cost."""
    return max(query["outcomes"], key=lambda m: query["outcomes"][m]["accuracy"])


def oracle_cheapest_correct(query: dict[str, Any]) -> str:
    """Hindsight bound that respects cost: cheapest model that gets it right."""
    correct = {m: c for m, c in query["outcomes"].items() if c["accuracy"] > 0}
    pool = correct or query["outcomes"]
    return min(pool, key=lambda m: pool[m]["cost"])


def family_oracle(
    table: dict[str, Any], by: str = "score"
) -> Callable[[dict[str, Any]], str]:
    """Best single model per skill family -- the ceiling for family-level routing.

    ``by="accuracy"`` picks each family's most accurate model; ``by="score"`` picks the one
    with the best Arena score for that family, which is the cost-aware choice and the one
    worth targeting.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for query in table["queries"]:
        groups.setdefault(query["family"], []).append(query)

    choice: dict[str, str] = {}
    for family, queries in groups.items():
        n = len(queries)
        best, best_key = None, -1.0
        for model in table["models"]:
            acc = sum(q["outcomes"][model]["accuracy"] for q in queries) / n
            cost = sum(q["outcomes"][model]["cost"] for q in queries) / n * 1000
            key = acc if by == "accuracy" else arena_score(max(cost, C_MIN), max(acc, 1e-6))
            if key > best_key:
                best, best_key = model, key
        choice[family] = best  # type: ignore[assignment]

    return lambda query: choice[query["family"]]


def baselines(table: dict[str, Any]) -> Iterable[Result]:
    """The reference set: what we did, what a constant does, what hindsight could do."""
    yield score("our router (as submitted)", as_submitted, table)
    for model in table["models"]:
        yield score(f"always {model.split('/')[-1]}", always(model), table)
    yield score("family oracle (by accuracy)", family_oracle(table, "accuracy"), table)
    yield score("family oracle (by arena score)", family_oracle(table, "score"), table)
    yield score("oracle: cheapest correct", oracle_cheapest_correct, table)
    yield score("oracle: best accuracy", oracle_best_accuracy, table)


def main() -> int:
    table = load_table()
    print(f"RouterArena offline harness -- {table['n_queries']} queries x {len(table['models'])} models\n")
    for result in baselines(table):
        print(f"  {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
