"""W2-8b / R-CLS-6 (UN-12): the daily row carries classifier agreement with n and window, or "not measured"."""
from __future__ import annotations

import json

import pytest

from llm_router import classifier_health as ch
from llm_router import daily_offload as do

from tests.test_daily_offload import NOW, _day, day_rows, home, inputs  # noqa: F401  (home is a fixture)

pytestmark = pytest.mark.usefixtures("home")


def _row(ts, rules, knn):
    return {"ts": ts, "rules_tier": rules, "knn_tier": knn, "source": "proxy"}


def test_agreement_hand_computed_n_and_pct():
    d = _day(16)
    s, e = do.day_bounds(d)
    rows = [_row(s + 10, "complex", "frontier"),          # agree
            _row(s + 20, "simple", "standard"),           # agree
            _row(s + 30, "moderate", "standard"),         # agree
            _row(s + 40, "deep_reasoning", "standard"),   # disagree
            _row(s - 1, "simple", "frontier"),            # before the day: out
            _row(e, "simple", "frontier")]                # half-open end: out
    ca = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t", agreement_rows=rows)["classifier_agreement"]
    assert (ca["n"], ca["agree_pct"], ca["status"]) == (4, 75.0, "measured")
    assert ca["window"] == {"since": s, "until": e} and ca["source"] == "classifier_agreement.jsonl"


def test_no_rows_is_null_not_zero():
    d = _day(16)
    ca = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t", agreement_rows=[])["classifier_agreement"]
    assert ca["agree_pct"] is None and ca["n"] == 0 and ca["status"] == "not_measured"
    assert ca["agree_pct"] != 0


def test_only_out_of_window_or_shadow_rows_is_not_measured():
    d = _day(16)
    s, e = do.day_bounds(d)
    rows = [_row(s - 5, "simple", "frontier"),
            {"ts": s + 5, "kind": "classifier_shadow", "source": "llm", "rules": {"tier": "simple"},
             "llm": {"tier": "simple"}}]
    ca = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t", agreement_rows=rows)["classifier_agreement"]
    assert ca["status"] == "not_measured" and ca["agree_pct"] is None


def test_default_reads_the_state_log():
    d = _day(16)
    s, _ = do.day_bounds(d)
    p = ch.agreement_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(_row(s + i, "simple", "standard")) + "\n" for i in range(3)))
    row = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t")
    assert (row["classifier_agreement"]["n"], row["classifier_agreement"]["agree_pct"]) == (3, 100.0)
    assert do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t")["schema"] == do.SCHEMA
