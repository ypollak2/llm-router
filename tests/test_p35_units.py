"""P35-1 (D-220, D-205): the units table, the quota rate and "no plan prices in product code".

Evidence: ``quota_savings.py`` carried ``$200/month`` as a default plan price (the RED8-05 rewrite of
a stale $50) and ``Summary.compact()`` summed measured and estimated dollars (2026-09-27). The
oracle rows are in tests/test_p35_fixtures.py; this file holds the edge cases and the source scan.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from llm_router import quota_rate, units
from llm_router.ledger_view import Figure, money_fragment, show

SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"


def _fig(value=3.0, n=5, window="7d", kind="measured", n_basis=None):
    return Figure(value, n, window, "t", "s", kind, None, n_basis)


# -- units ----------------------------------------------------------------------------------------

def test_unknown_figure_prints_nothing_never_zero():
    assert show(_fig(value=None), unit="api", quantity="usd") is None
    assert show(_fig(n=None), unit="api", quantity="usd") is None
    assert show(_fig(window=""), unit="api", quantity="usd") is None
    assert show(_fig(value=0.0), unit="api", quantity="usd") == "$0.00"      # a measured zero is shown


def test_an_estimated_figure_is_always_marked_whatever_the_unit():
    assert show(_fig(kind="estimated"), unit="api", quantity="usd") == "~$3.00"
    assert show(_fig(kind="estimated"), unit="subscription", quantity="pct") == "~3% of window"
    assert show(_fig(kind="measured"), unit="subscription", quantity="work_hours") == "~3 work-hours"
    assert not show(_fig(kind="measured"), unit="api", quantity="usd").startswith("~")


@pytest.mark.parametrize("unit,quantity", [("api", "pct"), ("subscription", "usd"), ("estimate", "pct"),
                                           ("plan", "usd"), ("api", None)])
def test_a_combination_outside_the_table_raises(unit, quantity):
    with pytest.raises(ValueError):
        show(_fig(), unit=unit, quantity=quantity)


def test_legacy_format_path_is_unchanged():
    assert show(_fig(value=3.5, n=4, window="today"), "${:,.2f}") == "$3.50"


def test_figure_dict_round_trip_keeps_the_second_n_only_when_present():
    assert "n_basis" not in _fig().to_dict()
    f = _fig(kind="estimated", n_basis=10)
    assert Figure.from_dict(f.to_dict()) == f


def test_money_fragment_never_sums_measured_and_estimated():
    assert money_fragment(2.0, 5.0) == "~$5.00 est · $2.00 measured"
    assert money_fragment(0.0, 5.0) == "~$5.00 est"
    assert money_fragment(2.0, 0.0) == "$2.00 measured"
    assert money_fragment(0.0, 0.0) == "~$0.00 est"
    assert "$7.00" not in money_fragment(2.0, 5.0)


# -- quota rate -----------------------------------------------------------------------------------

def _rows(points, source="measured"):
    return [{"ts": t, "weekly_pct": w, "source": source} for t, w in points]


def test_active_hours_sum_rises_per_utc_hour_and_ignore_resets_and_idle():
    h = 3600
    rows = _rows([(0, 10.0), (300, 12.0), (600, 13.0),          # hour 0: +3
                  (h, 13.0), (h + 300, 13.0),                    # hour 1: idle
                  (2 * h, 4.0), (2 * h + 300, 6.0)])             # hour 2: reset (-9) ignored, then +2
    assert quota_rate.active_hour_pps(rows) == [3.0, 2.0]


def test_stale_null_and_wide_gap_rows_are_not_burn():
    h = 3600
    assert quota_rate.active_hour_pps(_rows([(0, 1.0), (300, 9.0)], source="stale")) == []
    assert quota_rate.active_hour_pps([{"ts": 0, "weekly_pct": 1.0, "source": "measured"},
                                       {"ts": 300, "weekly_pct": None, "source": "measured"}]) == []
    assert quota_rate.active_hour_pps(_rows([(0, 1.0), (2 * h, 9.0)])) == []     # 2 h gap: not one hour


def test_rate_missing_history_is_na_with_a_reason(tmp_path):
    r = quota_rate.median_pp_per_active_hour(now=1e9, path=tmp_path / "none.jsonl")
    assert r.value is None and r.n == 0 and r.na_reason == "n/a (no quota history)"


def test_rate_counts_malformed_lines_instead_of_hiding_them(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"ts": 1e9, "weekly_pct": 1, "source": "measured"}\nnot json\n[1]\n')
    r = quota_rate.median_pp_per_active_hour(now=1e9, path=p)
    assert r.bad_rows == 2 and r.value is None


def test_rate_window_excludes_older_rows(tmp_path):
    import json

    now = 1_800_000_000.0
    rows = []
    for i in range(12):                                           # 12 active hours, 20 days ago
        t = now - 20 * 86400 + i * 3600
        rows += _rows([(t, 10.0), (t + 300, 12.0)])
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    assert quota_rate.median_pp_per_active_hour(now=now, days=14, path=p).value is None
    assert quota_rate.median_pp_per_active_hour(now=now, days=30, path=p).value == 2.0


def test_work_hours_guards_unknowns_and_a_zero_rate():
    assert quota_rate.work_hours(None, quota_rate.Rate(3.0, 12, "14d")) is None
    assert quota_rate.work_hours(6.0, quota_rate.Rate(None, 3, "14d", "n/a (3 of 10 active hours)")) is None
    assert quota_rate.work_hours(6.0, quota_rate.Rate(0.0, 12, "14d")) is None
    assert quota_rate.work_hours(6.0, quota_rate.Rate(3.0, 12, "14d")) == 2.0


# -- no plan prices in product code (D-205) -------------------------------------------------------

#: A dollar plan price: "$200" or "200/month" in product code.
_PRICE = re.compile(r"\$\s?200\b|\b200(\.0)?\s*/\s*(month|mo)\b|_USD_PER_MONTH_DEFAULT")
#: Plan names. Allowed in prose elsewhere (onboarding questions, docs); never in the modules that
#: turn quota into numbers.
_PLAN = re.compile(r"Pro Max|Max ?(5x|20x)|Claude (Pro|Max)\b|ChatGPT (Plus|Pro)\b")
_MONEY_PATH = ("units.py", "quota_rate.py", "quota_savings.py", "ledger_view.py", "dashboard_data.py",
               "statusline_segments.py")


def test_no_plan_price_or_plan_name_in_the_money_path_of_product_code():
    files = [p for p in SRC.rglob("*.py")]
    assert len(files) > 100 and all((SRC / m).is_file() for m in _MONEY_PATH), "the scan found nothing to scan"
    bad = [f"{p.relative_to(SRC)}:{i}: {line.strip()[:80]}"
           for p in files for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           if _PRICE.search(line)]
    bad += [f"{m}:{i}: {line.strip()[:80]}" for m in _MONEY_PATH
            for i, line in enumerate((SRC / m).read_text(encoding="utf-8").splitlines(), 1)
            if _PLAN.search(line)]
    assert not bad, f"a plan price or plan name in product code (D-205: plan and price are user config): {bad}"


def test_the_scan_catches_what_it_claims_to():
    assert _PRICE.search("_SUBSCRIPTION_USD_PER_MONTH_DEFAULT = 200.0")
    assert _PRICE.search("# $200/month plan") and _PRICE.search("price 200 / month")
    assert _PLAN.search("one week of Claude Pro Max") and _PLAN.search("a ChatGPT Plus account")
    assert not _PRICE.search("a $2000 budget") and not _PLAN.search("a Max and a Pro tier")


def test_quota_savings_with_no_plan_configured_is_na_not_a_default(monkeypatch):
    from llm_router import quota_savings as qs

    for k in ("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "LLM_ROUTER_WEEKLY_QUOTA_USD",
              "LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH"):
        monkeypatch.delenv(k, raising=False)
    assert qs._default_weekly_quota_usd() is None
    assert qs._calibration_usd_per_pp() == (None, "unconfigured")
    assert units.UNITS  # the table module imports without any plan data
