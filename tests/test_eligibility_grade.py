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


def test_exact_three_point_gap_from_counts():
    ev = evidence_from_counts(97, 100, 100, 100)
    assert ev.gap_pts == 3.0 and grade(ev) == "eligible"
    ev = evidence_from_counts(2, 100, 5, 100)
    assert ev.gap_pts == 3.0
    assert grade(ev) == ("eligible" if ev.lb_pts >= -10.0 - 1e-9 else "failed")
    assert grade(evidence_from_counts(96, 100, 100, 100)) == "failed"  # 4.0 pts


def test_exact_three_point_gap_sweep():
    found = 0
    for n in range(60, 121):
        for k_ref in range(n + 1):
            for k_cand in range(n + 1):
                if 100 * (k_ref - k_cand) != 3 * n:
                    continue
                found += 1
                ev = evidence_from_counts(k_cand, n, k_ref, n)
                assert ev.gap_pts == 3.0, (k_cand, k_ref, n)
                want = "eligible" if ev.lb_pts >= -10.0 - 1e-9 else "failed"
                assert grade(ev) == want, (k_cand, k_ref, n)
    assert found >= 20  # the sweep must find something to check


# ── review fixes ────────────────────────────────────────────────────────────

def test_lane_quality_row_key_newcombe_lb_is_read():
    assert grade({"n": 64, "gap_pts": 2.1, "newcombe_lb": -6.4}) == "eligible"
    assert grade({"n": 64, "gap_pts": 2.1, "newcombe_lb": -12.0}) == "failed"


@pytest.mark.parametrize("counts", [
    {"k_cand": 70, "n_cand": 60, "k_ref": 5, "n_ref": 60},     # k > n
    {"k_cand": -1, "n_cand": 60, "k_ref": 5, "n_ref": 60},     # negative
    {"k_cand": 59.9, "n_cand": 60.9, "k_ref": 60, "n_ref": 60},  # fractional
    {"k_cand": True, "n_cand": 60, "k_ref": 60, "n_ref": 60},  # bool
    {"k_cand": "59", "n_cand": 60, "k_ref": 60, "n_ref": 60},  # string
])
def test_malformed_counts_grade_unknown_never_raise(counts):
    assert grade(counts) == "unknown"


def test_malformed_mapping_values():
    assert grade({"n": "60", "gap_pts": 1, "lb_pts": 0}) == "unknown"
    assert grade({"n": 60, "gap_pts": float("nan"), "lb_pts": 0}) == "probe"
    assert grade({"n": 60, "gap_pts": 1, "lb_pts": float("inf")}) == "probe"


def test_supplied_gap_float_noise_is_tolerated():
    assert grade(Evidence(60, (0.05 - 0.02) * 100, -5.0)) == "eligible"
    assert grade(Evidence(60, 3.0001, -5.0)) == "failed"


def test_lane_quality_uses_the_one_rule():
    from llm_router import lane_quality
    row = {"kind": "shadow_pairs", "n": 64, "gap_pts": (0.05 - 0.02) * 100, "newcombe_lb": -5.0}
    assert lane_quality.batch_passed(row) is True
    assert lane_quality.batch_passed({**row, "n": 59}) is False
    assert lane_quality.batch_passed({**row, "newcombe_lb": None}) is None
