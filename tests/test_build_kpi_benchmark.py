"""scripts/build_kpi_benchmark.py: O2/D5 aggregation on a synthetic truth set (no git, no text)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_kpi_benchmark", Path(__file__).resolve().parent.parent / "scripts" / "build_kpi_benchmark.py")
bkb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bkb)


def _it(h, s, o, truth, pred):
    return {"id": "x", "pass": {"haiku": h, "sonnet": s, "opus": o}, "cheapest_tier": truth,
            "predicted_raw": pred, "predicted_effective": pred}


ITEMS = [
    _it(True, True, True, "haiku", "sonnet"),      # opus passed, haiku passed; over-route
    _it(False, True, True, "sonnet", "sonnet"),    # opus passed, haiku failed; exact
    _it(False, False, True, "opus", "sonnet"),     # opus passed, haiku failed; UNDER-route
    _it(True, True, False, "haiku", "opus"),       # opus failed: not in O2 pool; over-route
    _it(False, False, False, "none", "sonnet"),    # none passed: excluded from D5
]


def test_o2_d5_numerators_and_denominators():
    o2, d5 = bkb.aggregate(ITEMS)
    assert (o2["n"], o2["acceptable_rate"]) == (3, 1 / 3)
    assert d5["n"] == 4 and d5["n_no_truth"] == 1                 # the 'none' task is excluded
    assert d5["accuracy"] == 1 / 4
    assert d5["under_route_rate"] == 1 / 4                        # only the opus-truth item
    assert d5["predicted_tier_counts"] == {"haiku": 0, "sonnet": 3, "opus": 1}   # graded only
    assert "not informative" in d5["note"]


def test_empty_input_is_zero_n_not_a_crash():
    o2, d5 = bkb.aggregate([])
    assert o2["n"] == 0 and d5["n"] == 0
