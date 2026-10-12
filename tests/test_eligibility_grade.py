"""P2.0 task 6 / MUST P2.0-b: the one eligibility rule."""
import itertools

import pytest

from llm_router.eligibility import Evidence, evidence_from_counts, grade, newcombe_diff_ci


@pytest.mark.parametrize("ev,want", [
    (Evidence(60, 3.0, -5.0), "eligible"),      # gap 3.0
    (Evidence(60, 3.1, -5.0), "failed"),        # gap 3.1
    (Evidence(60, 0.0, -10.0), "eligible"),     # LB -10.0
    (Evidence(60, 0.0, -10.1), "failed"),       # LB -10.1
    (Evidence(59, 0.0, 0.0), "probe"),          # n 59
    (Evidence(60, 0.0, 0.0), "eligible"),       # n 60
    (Evidence(0, 0.0, 0.0), "unknown"),         # n 0
    ({}, "unknown"),                            # empty evidence
    (None, "unknown"),
    ({"n": 60, "gap_pts": 3.0, "lb_pts": -10.0}, "eligible"),
    (Evidence(60, 3.0, -10.1), "failed"),
    (Evidence(1, 50.0, -90.0), "probe"),        # little evidence is never failed
    (Evidence(60, None, None), "probe"),        # n ok but no measurement
    (Evidence(200, -4.0, 2.0), "eligible"),     # candidate better than reference
])
def test_boundaries(ev, want):
    assert grade(ev) == want


def test_eligible_implies_n_at_least_60():
    gaps = [-5.0, 0.0, 2.9, 3.0, 3.1, 20.0, None]
    lbs = [-30.0, -10.1, -10.0, 0.0, 5.0, None]
    seen = 0
    for n, gap, lb in itertools.product(range(0, 201), gaps, lbs):
        if grade(Evidence(n, gap, lb)) == "eligible":
            seen += 1
            assert n >= 60
    assert seen > 0  # the property was exercised


def test_newcombe_known_value():
    lo, hi = newcombe_diff_ci(45, 60, 50, 60)
    d = 45 / 60 - 50 / 60
    assert lo < d < hi
    assert lo == pytest.approx(-0.22606, abs=1e-4) and hi == pytest.approx(0.06303, abs=1e-4)  # hand-computed, method 10


def test_newcombe_identical_arms_contain_zero():
    lo, hi = newcombe_diff_ci(30, 60, 30, 60)
    assert lo < 0 < hi and lo == pytest.approx(-hi)


def test_newcombe_rejects_bad_input():
    with pytest.raises(ValueError):
        newcombe_diff_ci(1, 0, 1, 5)


def test_evidence_from_counts():
    assert evidence_from_counts(0, 0, 5, 10) == Evidence()
    ev = evidence_from_counts(58, 64, 60, 64)
    assert ev.n == 64 and ev.gap_pts == pytest.approx(3.125) and grade(ev) == "failed"
    assert grade(evidence_from_counts(60, 60, 60, 60)) == "eligible"


def test_grade_accepts_raw_counts():
    assert grade({"k_cand": 60, "n_cand": 60, "k_ref": 60, "n_ref": 60}) == "eligible"
    assert grade({"k_cand": 10, "n_cand": 20, "k_ref": 20, "n_ref": 20}) == "probe"
    assert grade({"k_cand": 0, "n_cand": 0, "k_ref": 5, "n_ref": 10}) == "unknown"
    assert grade({"k_cand": 30, "n_cand": 64, "k_ref": 60, "n_ref": 64}) == "failed"
