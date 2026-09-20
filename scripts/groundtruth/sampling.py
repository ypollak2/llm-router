"""Proportional stratified sampling for a future production Ground Truth v1.

Ready now, deliberately unused: there is not yet enough clean traffic to
sample. Writing it early is what forces the stratification decisions to be made
before anyone is staring at a number they want to move.

Taxonomy
--------
Reuses the repo's own, never a new one:

    TaskType   (llm_router.types)  query research generate analyze code
                                   introspect coordinate image video audio
    Complexity (llm_router.types)  simple moderate complex deep_reasoning

The categories in a sampling brief — "coding, debugging, research, reasoning,
generation, summarisation, agentic, tool-heavy" — are mostly already expressed
here. `code` covers coding and debugging; `deep_reasoning` is reasoning;
`route_kind=delegate` is the agentic axis; `tool_execution_attempted` is the
tool-heavy one. Inventing a parallel scheme would produce strata the router
does not actually use, and nothing downstream could act on them.

Rare-but-important strata
-------------------------
Proportional sampling alone will drop a stratum that is 0.4% of traffic, and
those are frequently the ones worth measuring — `analyze` was 0.4% of
non-test traffic in the 2026-09-20 audit, and quality escalations are rarer
still. `min_per_stratum` therefore guarantees a floor for any stratum that
exists at all, and the manifest records the resulting over-representation so
nobody later reads the sample as an unbiased frequency estimate. Guaranteeing
the floor and hiding it would be worse than not guaranteeing it.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

try:
    from llm_router.types import Complexity, TaskType
    TASK_TYPES: tuple[str, ...] = tuple(t.value for t in TaskType)
    COMPLEXITIES: tuple[str, ...] = tuple(c.value for c in Complexity)
    TAXONOMY_SOURCE = "llm_router.types"
except Exception:  # noqa: BLE001 — a vendored copy must still be able to sample
    TASK_TYPES = ("query", "research", "generate", "analyze", "code",
                  "introspect", "coordinate", "image", "video", "audio")
    COMPLEXITIES = ("simple", "moderate", "complex", "deep_reasoning")
    TAXONOMY_SOURCE = "fallback-literal"

# Strata that must never be sampled to zero when present. These are rare and
# high-consequence: a dataset with no agentic or tool-using tasks cannot say
# anything about the routes where getting it wrong costs the most.
HIGH_RISK_DIMENSIONS = ("agentic", "tool_heavy", "escalated")


@dataclass(frozen=True)
class Stratum:
    task_type: str
    complexity: str
    agentic: bool = False
    tool_heavy: bool = False

    def key(self) -> tuple:
        return (self.task_type, self.complexity, self.agentic, self.tool_heavy)

    def __str__(self) -> str:
        extra = "".join(["+agentic" if self.agentic else "",
                         "+tools" if self.tool_heavy else ""])
        return f"{self.task_type}/{self.complexity}{extra}"


def stratum_of(unit) -> Stratum:
    """Derive a stratum from an EvaluationUnit (or any object with the fields)."""
    return Stratum(
        task_type=(getattr(unit, "task_type", None) or "unknown"),
        complexity=(getattr(unit, "complexity", None) or "unknown"),
        agentic=str(getattr(unit, "route_kind", "") or "").startswith("delegate"),
        tool_heavy=bool(getattr(unit, "tool_execution_attempted", False)),
    )


@dataclass
class SamplePlan:
    """What will be drawn, and how far it departs from the true distribution."""

    target_n: int
    allocation: dict[tuple, int] = field(default_factory=dict)
    population: dict[tuple, int] = field(default_factory=dict)
    boosted: list[tuple] = field(default_factory=list)
    shortfalls: dict[tuple, int] = field(default_factory=dict)
    # How far the rare-stratum floors pushed the plan past target_n, if at all.
    floor_overshoot: int = 0

    @property
    def planned_n(self) -> int:
        return sum(self.allocation.values())

    def representation(self) -> dict[str, dict]:
        """Per stratum: true share, sampled share, and the ratio between them.

        A ratio far from 1.0 is not a bug — it is the price of the floor — but
        it has to be visible, because a reader who assumes proportionality will
        otherwise draw a frequency conclusion the sample cannot support.
        """
        pop_total = sum(self.population.values()) or 1
        samp_total = self.planned_n or 1
        out: dict[str, dict] = {}
        for key, n_pop in sorted(self.population.items(), key=lambda kv: -kv[1]):
            n_samp = self.allocation.get(key, 0)
            true_share = n_pop / pop_total
            samp_share = n_samp / samp_total
            out[str(key)] = {
                "population": n_pop,
                "sampled": n_samp,
                "true_share": round(true_share, 4),
                "sampled_share": round(samp_share, 4),
                "over_representation": round(samp_share / true_share, 2)
                if true_share else None,
                "boosted_to_floor": key in self.boosted,
            }
        return out


def plan_sample(
    units: Sequence,
    *,
    target_n: int = 400,
    min_per_stratum: int = 3,
    stratify: Callable[[object], Stratum] = stratum_of,
) -> SamplePlan:
    """Largest-remainder proportional allocation, with a floor for rare strata.

    Largest-remainder rather than rounding each share independently: rounding
    does not sum to `target_n`, and the shortfall lands wherever the arithmetic
    happens to put it.
    """
    population: Counter[tuple] = Counter()
    for u in units:
        population[stratify(u).key()] += 1
    if not population:
        return SamplePlan(target_n=target_n)

    total = sum(population.values())
    plan = SamplePlan(target_n=target_n, population=dict(population))

    # Floor first, capped by what the stratum actually has.
    alloc: dict[tuple, int] = {}
    for key, n_pop in population.items():
        floor = min(min_per_stratum, n_pop)
        alloc[key] = floor
        if floor > round(target_n * n_pop / total):
            plan.boosted.append(key)

    # The floors are a hard commitment, so they consume budget before anything
    # proportional is allocated. Only what is LEFT gets distributed by share —
    # an earlier version allocated each stratum its full proportional count on
    # top of its floor, which overshot the target by the size of the floors
    # (454 drawn against a target of 400).
    remaining = target_n - sum(alloc.values())

    if remaining < 0:
        # The floors alone exceed the target. Legitimate when there are many
        # rare strata; the caller needs to know rather than have strata silently
        # dropped to fit, so the floor wins and the overshoot is reported.
        plan.floor_overshoot = -remaining
    elif remaining > 0:
        headroom = {k: population[k] - alloc[k] for k in population}
        capacity = sum(headroom.values())
        remaining = min(remaining, capacity)
        # Largest-remainder over the REMAINING budget, weighted by population.
        exact = {k: remaining * population[k] / total for k in population}
        whole = {k: min(int(exact[k]), headroom[k]) for k in population}
        for k, v in whole.items():
            alloc[k] += v
        left = remaining - sum(whole.values())
        order = sorted(population,
                       key=lambda k: (-(exact[k] % 1), -population[k], str(k)))
        i = 0
        # `left` is bounded by capacity, so this terminates.
        while left > 0 and any(population[k] - alloc[k] > 0 for k in population):
            k = order[i % len(order)]
            if population[k] - alloc[k] > 0:
                alloc[k] += 1
                left -= 1
            i += 1

    plan.allocation = {k: v for k, v in alloc.items() if v > 0}
    proportional = {k: target_n * population[k] // total for k in population}
    plan.shortfalls = {k: proportional[k] - plan.allocation.get(k, 0)
                       for k in population
                       if plan.allocation.get(k, 0) < proportional[k]}
    return plan


def draw(
    units: Sequence,
    plan: SamplePlan,
    *,
    seed: int = 20260920,
    stratify: Callable[[object], Stratum] = stratum_of,
) -> list:
    """Draw the planned sample. Deterministic for a given seed and input order."""
    buckets: dict[tuple, list] = {}
    for u in units:
        buckets.setdefault(stratify(u).key(), []).append(u)
    rng = random.Random(seed)
    out: list = []
    for key in sorted(plan.allocation, key=str):
        pool = list(buckets.get(key, []))
        rng.shuffle(pool)
        out.extend(pool[: plan.allocation[key]])
    return out


def describe(plan: SamplePlan) -> str:
    lines = [
        f"taxonomy source     {TAXONOMY_SOURCE}",
        f"target n            {plan.target_n}",
        f"planned n           {plan.planned_n}",
        f"strata              {len(plan.population)}",
        f"boosted to floor    {len(plan.boosted)}",
        (f"floor overshoot     +{plan.floor_overshoot} over target "
         f"(floors take precedence)" if plan.floor_overshoot else
         "floor overshoot     none"),
        "",
        f"  {'stratum':46s} {'pop':>6s} {'samp':>6s} {'true%':>7s} {'samp%':>7s} {'x':>6s}",
    ]
    for key, row in plan.representation().items():
        lines.append(
            f"  {key[:46]:46s} {row['population']:6d} {row['sampled']:6d} "
            f"{row['true_share']:6.1%} {row['sampled_share']:6.1%} "
            f"{(str(row['over_representation']) + ('*' if row['boosted_to_floor'] else '')):>6s}")
    if plan.boosted:
        lines.append("\n  * boosted to the rare-stratum floor — over-represented ON PURPOSE.")
        lines.append("    Do not read sampled shares as traffic frequencies.")
    return "\n".join(lines)
