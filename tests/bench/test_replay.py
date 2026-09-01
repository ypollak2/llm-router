# SPDX-License-Identifier: MIT
"""Tests for the offline RouterArena replay harness and the leakage protocol.

Two things are under test and they matter for different reasons. The Arena formula has to be
right or every number downstream is wrong in a way that looks plausible. The sealed-half
refusal has to be right because it is the only thing standing between us and quietly fitting
a policy to the benchmark we are claiming not to fit to.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "routerarena"))

import replay  # noqa: E402


# ----------------------------------------------------------------------------------------
# The scoring formula
# ----------------------------------------------------------------------------------------


def test_arena_score_matches_hand_computed_cases():
    """Three points on the curve, worked out by hand from the published definition.

    S = ((1+B)*A*C) / (B*A + C),  C = (log2(200) - log2(c)) / (log2(200) - log2(0.0044))
    """
    # A = 0.7544, c = 0.7459 -> the shipped submission's own numbers.
    assert replay.arena_score(0.7544, 0.7459) == pytest.approx(0.72492, abs=1e-4)
    # C collapses to 1.0 at the minimum cost, giving the ceiling for that accuracy.
    assert replay.arena_score(0.7544, replay.C_MIN) == pytest.approx(0.77163, abs=1e-4)
    # And to 0.0 at the maximum cost, which zeroes the score regardless of accuracy.
    assert replay.arena_score(0.9, replay.C_MAX) == pytest.approx(0.0, abs=1e-9)


def test_cost_is_clamped_at_both_ends():
    """Costs outside [C_MIN, C_MAX] clamp rather than running the log off the end."""
    assert replay.arena_score(0.8, 0.0001) == replay.arena_score(0.8, replay.C_MIN)
    assert replay.arena_score(0.8, 10_000.0) == replay.arena_score(0.8, replay.C_MAX)


def test_a_wrong_beta_would_fail_these_cases():
    """The acceptance criterion asked that a deliberately wrong beta be caught."""
    original = replay.BETA
    try:
        replay.BETA = 0.5
        assert replay.arena_score(0.7544, 0.7459) != pytest.approx(0.72492, abs=1e-4)
    finally:
        replay.BETA = original


# ----------------------------------------------------------------------------------------
# Replaying known policies over the captured matrix
# ----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def matrix():
    if not replay.MATRIX.exists():
        pytest.skip("sub10 matrix not extracted")
    return replay.load_matrix()


def test_shipped_policy_reproduces_the_harness(matrix):
    """The whole harness is only trustworthy if it reproduces the real evaluation exactly."""
    got = replay.score(matrix, replay.policy_shipped)
    assert got["n_queries"] == 809
    assert got["arena_score"] == pytest.approx(0.72492, abs=1e-4)
    assert got["accuracy"] == pytest.approx(0.754370, abs=1e-5)
    assert got["cost_per_1k"] == pytest.approx(0.7459, abs=1e-3)
    assert got["opt_sel"] == pytest.approx(0.0443, abs=1e-3)
    assert got["opt_cost"] == pytest.approx(0.0612, abs=1e-3)
    assert got["opt_acc"] == pytest.approx(0.8523, abs=1e-3)
    assert got["queries_with_optimal_data"] == 677


def test_oracle_dominates_every_constant(matrix):
    """The oracle is a ceiling by construction; if a constant beats it, the search is wrong."""
    oracle = replay.score(matrix, replay.policy_oracle)["arena_score"]
    models = set()
    for rec in matrix.values():
        models |= set(rec["models"])
    for m in models:
        assert replay.score(matrix, replay.policy_always(m))["arena_score"] <= oracle + 1e-9


def test_oracle_accuracy_is_at_least_every_constant(matrix):
    oracle_acc = replay.score(matrix, replay.policy_oracle)["accuracy"]
    for m in ("google/gemini-3.1-flash-lite-preview", "deepseek/deepseek-v4-flash"):
        assert replay.score(matrix, replay.policy_always(m))["accuracy"] <= oracle_acc + 1e-9


def test_pair_oracle_sits_between_cheap_constant_and_full_oracle(matrix):
    cheap = "google/gemini-3.1-flash-lite-preview"
    exp = "deepseek/deepseek-v4-flash"
    constant = replay.score(matrix, replay.policy_always(cheap))["accuracy"]
    pair = replay.score(matrix, replay.policy_pair_oracle(cheap, exp))["accuracy"]
    full = replay.score(matrix, replay.policy_oracle)["accuracy"]
    assert constant <= pair <= full + 1e-9


# ----------------------------------------------------------------------------------------
# The split
# ----------------------------------------------------------------------------------------


def test_split_is_total_disjoint_and_family_stratified(matrix):
    split = replay.load_split(matrix)
    assert set(split) == set(matrix)
    assert set(split.values()) == {"dev", "sealed"}

    by_family: dict[str, list[str]] = {}
    for gi, half in split.items():
        by_family.setdefault(replay.family(gi), []).append(half)
    for fam, halves in by_family.items():
        n_dev = halves.count("dev")
        # Stratification means every family is split to within one query. A plain per-query
        # hash failed this, and the resulting skew moved the always-gemini baseline 3.34
        # Arena points between halves -- enough to swamp the effects we are trying to measure.
        assert abs(n_dev - (len(halves) - n_dev)) <= 1, fam


def test_split_is_stable_across_calls(matrix):
    assert replay.build_split(matrix) == replay.build_split(matrix)


def test_halves_agree_on_a_fixed_baseline(matrix):
    """A split that disagrees with itself on a constant cannot measure a policy."""
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    sealed = {k: v for k, v in matrix.items() if split[k] == "sealed"}
    pol = replay.policy_always("google/gemini-3.1-flash-lite-preview")
    a = replay.score(dev, pol)["arena_score"]
    b = replay.score(sealed, pol)["arena_score"]
    assert abs(a - b) * 100 <= 2.0, f"halves differ by {abs(a - b) * 100:.2f} Arena points"


# ----------------------------------------------------------------------------------------
# The leakage protocol
# ----------------------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "routerarena" / "replay.py"), *args],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_sealed_half_is_refused_without_the_freeze_flag():
    r = _run("--policy", "shipped", "--half", "sealed")
    assert r.returncode == 2
    assert "i-am-freezing" in r.stderr


def test_all_is_also_refused_because_it_contains_sealed():
    r = _run("--policy", "shipped", "--half", "all")
    assert r.returncode == 2


def test_dev_half_needs_no_flag():
    r = _run("--policy", "shipped", "--half", "dev", "--no-log")
    assert r.returncode == 0, r.stderr
    assert "ARENA" in r.stdout


def test_reads_are_logged(tmp_path, monkeypatch):
    log = tmp_path / "peek_log.jsonl"
    monkeypatch.setattr(replay, "PEEK_LOG", log)
    replay.log_peek("dev", "shipped", {
        "arena_score": 0.5, "accuracy": 0.6, "cost_per_1k": 0.1, "n_queries": 400
    })
    replay.log_peek("dev", "oracle", {
        "arena_score": 0.8, "accuracy": 0.9, "cost_per_1k": 0.1, "n_queries": 400
    })
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["policy"] == "shipped" and lines[0]["half"] == "dev"
