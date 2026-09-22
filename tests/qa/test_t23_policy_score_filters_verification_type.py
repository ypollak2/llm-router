"""Ground Truth audit item 4.3 — `policy_score` pooled every verdict alike.

`label.py` is careful: it detects any task mixing `SUBJECTIVE_METHODS` with
`DETERMINISTIC_METHODS` and never emits `cheapest_acceptable_model` for a
subjective task. That is the one place the headline label is produced, and it is
honest.

`discriminate.policy_score` is one layer out, and it pooled every cell's
`accepted` boolean with **zero** verification-type filtering — it never imported
`DETERMINISTIC_METHODS` at all. A judge's opinion and a passing assertion counted
identically toward a published accept rate.

This was harmless only by accident. `run_matrix` runs
`verifier_kind == MECHANICAL` tasks and nothing else, so no judge-verified cell
has ever reached the matrix. **An accidental barrier is not a designed one:**
extending `generate_snippet()` to judge strategies is a natural next step, and it
would have started pooling subjective verdicts with mechanical ones with no
change here and no signal that anything had altered.

Demonstrated on a mixed matrix:

    strict=True    rate=0.50  n=2   (mechanical only)
    strict=False   rate=0.75  n=4   (everything, as before)

The 0.75 is the number that would have been published.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TIER_ORDER = ["local", "cheap", "premium"]


@pytest.fixture(scope="module")
def gt():
    sys.path.insert(0, str(ROOT / "scripts"))
    from groundtruth import dataset as ds
    from groundtruth import discriminate as D

    return D, ds


def _cell(accepted: bool, vtype):
    return {"accepted": accepted, "cost_usd": 0.001, "verification_type": vtype}


def _mixed_matrix(ds):
    return {
        "t1": {"local": _cell(True, ds.V_MECHANICAL)},
        "t2": {"local": _cell(False, ds.V_MECHANICAL)},
        "t3": {"local": _cell(True, "judge")},     # subjective
        "t4": {"local": _cell(True, None)},        # unknown provenance
    }


def test_subjective_and_unknown_cells_do_not_count(gt):
    D, ds = gt
    rate, _cost, n = D.policy_score(_mixed_matrix(ds), lambda p, _c: p[0], TIER_ORDER)
    assert n == 2, f"expected 2 deterministic cells, counted {n}"
    assert rate == pytest.approx(0.5), (
        f"accept rate {rate} — a judge verdict or an unrecorded type is still "
        f"contributing to the published number"
    )


def test_without_the_filter_the_number_is_different(gt):
    """Anti-vacuity: the filter must actually change the result.

    If every fixture were mechanical, the strict test above would pass with or
    without the fix.
    """
    D, ds = gt
    rate, _c, n = D.policy_score(
        _mixed_matrix(ds), lambda p, _c: p[0], TIER_ORDER, strict=False)
    assert n == 4 and rate == pytest.approx(0.75), (
        "the unfiltered path no longer differs from the filtered one, so this "
        "suite cannot demonstrate that filtering happens"
    )


def test_missing_verification_type_fails_closed(gt):
    """A matrix written before the field existed is UNKNOWN, not mechanical."""
    D, ds = gt
    matrix = {"t1": {"local": {"accepted": True, "cost_usd": 0.0}}}   # no key at all
    _rate, _cost, n = D.policy_score(matrix, lambda p, _c: p[0], TIER_ORDER)
    assert n == 0, "a cell with no recorded verification type was counted"


def test_every_deterministic_method_is_admitted(gt):
    """Anti-over-correction: the filter must not reject legitimate verification.

    Driven from the canonical set rather than a hand-typed list, so adding a
    deterministic method cannot silently leave it unexercised here.
    """
    D, ds = gt
    methods = sorted(ds.DETERMINISTIC_METHODS)
    assert len(methods) >= 4, f"only {len(methods)} deterministic methods — set looks wrong"

    rejected = []
    for method in methods:
        matrix = {"t1": {"local": _cell(True, method)}}
        _rate, _cost, n = D.policy_score(matrix, lambda p, _c: p[0], TIER_ORDER)
        if n != 1:
            rejected.append(method)
    assert not rejected, f"deterministic methods excluded by the filter: {rejected}"


def test_the_filter_uses_the_canonical_set(gt):
    """Pin the mechanism. A local list of method names would drift from label.py."""
    src = (ROOT / "scripts" / "groundtruth" / "discriminate.py").read_text(encoding="utf-8")
    assert "DETERMINISTIC_METHODS" in src, (
        "discriminate.py no longer references the canonical deterministic set"
    )


def test_run_matrix_still_records_the_type(gt):
    """The filter depends on `run_matrix` writing the field. Pin that too.

    If the producer stopped recording it, every cell would read as UNKNOWN and
    `policy_score` would return n=0 — a clean-looking zero rather than an error.
    """
    src = (ROOT / "scripts" / "groundtruth" / "run_matrix.py").read_text(encoding="utf-8")
    assert '"verification_type"' in src, (
        "run_matrix no longer records verification_type; policy_score would "
        "silently exclude every cell"
    )
