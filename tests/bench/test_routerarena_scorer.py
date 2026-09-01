# SPDX-License-Identifier: MIT
"""Regression tests for the offline RouterArena policy scorer.

These pin the baselines the whole #1 plan is argued from. If one of them moves, either the
outcome table was rebuilt from a different submission or the scoring formula drifted -- both
are things we want to hear about loudly, because every projected score downstream is quoted
relative to these numbers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCORER = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "routerarena"
    / "score_policy.py"
)


def _load():
    """Import score_policy.py by path -- scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("ra_score_policy", _SCORER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ra_score_policy"] = module
    spec.loader.exec_module(module)
    return module


sp = _load()


@pytest.fixture(scope="module")
def table():
    if not sp.DATA.exists():
        pytest.skip("outcome table not built -- run scripts/routerarena/extract_outcomes.py")
    return sp.load_table()


def test_arena_score_reproduces_published_rows():
    """The formula must reproduce leaderboard rows we did not compute ourselves."""
    # Paix2 (#1): 79.69% at $0.27/1K -> 77.63
    assert sp.arena_score(0.27, 0.7969) * 100 == pytest.approx(77.63, abs=0.02)
    # Sqwish (#2): 79.76% at $0.70/1K -> 76.21
    assert sp.arena_score(0.70, 0.7976) * 100 == pytest.approx(76.21, abs=0.02)


def test_table_shape(table):
    assert table["n_queries"] == 786
    assert len(table["models"]) == 5
    for query in table["queries"]:
        assert len(query["outcomes"]) == len(table["models"])


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("our router (as submitted)", 75.84),
        ("always gemini-3.1-flash-lite", 74.75),
        ("always qwen3-235b-a22b-2507", 73.78),
        ("family oracle (by accuracy)", 79.41),
        ("family oracle (by arena score)", 79.78),
        ("oracle: cheapest correct", 84.15),
    ],
)
def test_baselines_are_pinned(table, label, expected):
    results = {r.name: r for r in sp.baselines(table)}
    assert results[label].score * 100 == pytest.approx(expected, abs=0.05)


def test_routing_beats_the_best_constant(table):
    """The premise of the plan: routing earns its keep, but only just."""
    results = {r.name: r for r in sp.baselines(table)}
    ours = results["our router (as submitted)"].score
    best_constant = max(
        r.score for name, r in results.items() if name.startswith("always ")
    )
    assert ours > best_constant
    assert (ours - best_constant) * 100 == pytest.approx(1.09, abs=0.05)


def test_oracle_gap_is_the_prize(table):
    """~8 points sit between us and a perfect selector over the same five models."""
    results = {r.name: r for r in sp.baselines(table)}
    gap = results["oracle: cheapest correct"].score - results["our router (as submitted)"].score
    assert gap * 100 == pytest.approx(8.31, abs=0.05)


def test_cheapest_correct_oracle_is_cheaper_than_us(table):
    """The oracle is not buying accuracy with money -- it spends a third of what we do."""
    results = {r.name: r for r in sp.baselines(table)}
    assert results["oracle: cheapest correct"].cost_per_1k < (
        results["our router (as submitted)"].cost_per_1k / 2
    )


def test_policy_choosing_unknown_model_is_rejected(table):
    with pytest.raises(KeyError):
        sp.score("bogus", lambda _q: "not/a-real-model", table)
