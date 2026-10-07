"""Tests for the pure parts of m18_r3.py (amendment 2): tau_abs fit, abstain application, K1, staging."""
import ast
import inspect
import os
import sys
import textwrap
from pathlib import Path

import pytest

# m18_r3 imports the external eval harness (eval_router, m18_select) from $PP; without it there is nothing to test here.
if not os.environ.get("PP"):
    pytest.skip("PP (primary-plan directory with the eval harness) is not set", allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bench"))
import m18_r3 as R  # noqa: E402


def row(i, tier, conf, ms=500.0, st="e2", invalid=False):
    slow = ms is None or ms > R.BUDGET_MS
    return {"id": i, "set": st, "ms": ms, "tier": None if invalid else tier, "conf": None if invalid else conf,
            "invalid": invalid, "slow": slow, "base_fb": invalid or slow}


def test_apply_tau_abstain_takes_rules_and_counts_as_fallback():
    rows = [row("a", "haiku", 0.55), row("b", "opus", 0.9)]
    preds, flags = R.apply_tau(rows, 0.6, {"a": "sonnet", "b": "sonnet"})
    assert preds == {"a": "sonnet", "b": "opus"}
    assert flags["a"]["abstain"] and flags["a"]["fallback"] and not flags["b"]["fallback"]


def test_apply_tau_equal_to_threshold_does_not_abstain():
    preds, flags = R.apply_tau([row("a", "haiku", 0.6)], 0.6, {"a": "sonnet"})
    assert preds["a"] == "haiku" and not flags["a"]["abstain"]


def test_slow_and_invalid_rows_fall_back_without_being_abstains():
    rows = [row("a", "haiku", 0.99, ms=2500.0), row("b", None, None, invalid=True)]
    preds, flags = R.apply_tau(rows, 0.0, {"a": "opus", "b": "opus"})
    assert preds == {"a": "opus", "b": "opus"} and not any(f["abstain"] for f in flags.values())
    assert all(f["fallback"] for f in flags.values())


def _toy(n=40):
    """Confident answers are right; unconfident ones are wrong (model says haiku, truth opus; rules say opus)."""
    rows, truth, reff = [], {}, {}
    for k in range(n):
        i = f"i{k}"
        good = k % 4 != 0
        rows.append(row(i, "sonnet" if good else "haiku", 0.9 if good else 0.5))
        truth[i], reff[i] = ("sonnet" if good else "opus"), "opus"
    return rows, truth, reff


def test_fit_picks_the_lowest_c2_among_feasible_taus():
    rows, truth, reff = _toy()
    # abstaining the 25% wrong ones breaks the 5% bar; the only feasible tau is one that abstains <= 2 items: none here
    tau, table = R.fit_tau(rows, truth, reff)
    assert tau == 0.0
    assert [t["feasible"] for t in table if t["tau"] > 0.5 and t["tau"] <= 0.9] == [False] * 8


def test_fit_with_a_small_abstain_set_chooses_the_abstaining_tau():
    rows, truth, reff = _toy(100)
    for r in rows[:2]:                     # two wrong, low-confidence answers
        r["conf"] = 0.41
    for r in rows[2:]:
        r["conf"] = 0.95 if r["tier"] == "sonnet" else 0.9
    # wrong ones (every 4th item) have conf 0.9 except the first two; taus up to 0.9 abstain exactly 2 items (2%)
    tau, table = R.fit_tau(rows, truth, reff)
    best = {t["tau"]: t for t in table}[tau]
    assert best["feasible"] and best["abstain"] == 2 and tau > 0.41
    assert best["c2"] < {t["tau"]: t for t in table}[0.0]["c2"]


def test_fit_tie_goes_to_the_lower_tau():
    rows = [row(f"i{k}", "sonnet", 0.8) for k in range(20)]
    truth = {r["id"]: "sonnet" for r in rows}
    tau, _ = R.fit_tau(rows, truth, {r["id"]: "sonnet" for r in rows})
    assert tau == 0.0                      # taus 0.0..0.80 are all the same: the lowest wins


def test_fit_with_no_feasible_tau_is_zero():
    rows = [row(f"i{k}", "sonnet", 0.8, ms=3000.0) for k in range(20)]   # every call is slower than 2 s
    truth = {r["id"]: "sonnet" for r in rows}
    tau, table = R.fit_tau(rows, truth, {r["id"]: "sonnet" for r in rows})
    assert tau == 0.0 and not any(t["feasible"] for t in table)


def test_fit_refuses_e1_rows():
    with pytest.raises(ValueError):
        R.fit_tau([row("a", "haiku", 0.9, st="e1")], {"a": "haiku"}, {"a": "haiku"})


def test_the_fit_file_is_written_before_e1_labels_are_loaded():
    src = inspect.getsource(R.analyze)
    assert src.index("fit_path.write_text") < src.index("ER.load_e1") < src.index("rows1 =")
    tree = ast.parse(textwrap.dedent(inspect.getsource(R.fit_tau)))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not [u for u in used if "e1" in u.lower()], "fit_tau must not reference E1 data"


def test_k1_bars():
    assert R.k1(0.05, 9.0, 9.58) and not R.k1(0.051, 9.0, 9.58)
    assert not R.k1(0.0, 9.58, 9.58) and not R.k1(0.0, 10.7, 9.58)


def test_pbin():
    assert [R.pbin(p) for p in (0.333, 0.4, 0.69, 0.7, 0.999, 1.0)] == ["0.3", "0.4", "0.6", "0.7", "0.9", "0.9"]


def test_grid_is_the_pre_registered_one():
    assert R.TAU_GRID == (0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
    assert (R.BUDGET_MS, R.FALLBACK_MAX, R.RAW_TIMEOUT_S) == (2000.0, 0.05, 30.0)


def test_exactly_five_percent_fallback_is_feasible():
    rows = [row(f"i{k}", "sonnet", 0.8) for k in range(20)]
    rows[0] = row("i0", "sonnet", 0.8, ms=2500.0)             # 1/20 = 5.0% slow
    truth = {r["id"]: "sonnet" for r in rows}
    _, table = R.fit_tau(rows, truth, {r["id"]: "sonnet" for r in rows})
    assert table[0]["fallback_rate"] == 0.05 and table[0]["feasible"]
