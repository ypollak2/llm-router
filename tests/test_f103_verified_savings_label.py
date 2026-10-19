"""F103: verified_usd is VERIFIED counterfactual savings, not metered spend. No surface may print it
as "measured"; the estimate and the verified part are two segments and are never merged (rv-630)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from llm_router import units
from llm_router.dashboard_data import Summary
from llm_router.ledger_view import money_fragment

SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"
ROOT = SRC.parents[1]


def _summary(**kw):
    base = dict(period="today", baseline_model="base-x", realized_usd=2.0, realized_n=3,
                unverified_usd=5.0, unverified_n=4, routed_n=9, unmeasured_n=0)
    base.update(kw)
    return Summary(**base)


def test_display_never_merges_estimate_and_verified():
    text = _summary().display()
    assert text == "est. saved $5.00 (n=4) · $2.00 verified saved (n=3) vs base-x", text
    assert "$7.00" not in text and "n=7" not in text


@pytest.mark.parametrize("kw,expect", [
    (dict(realized_usd=0.0, realized_n=0), "est. saved $5.00 (n=4) vs base-x"),
    (dict(unverified_usd=0.0, unverified_n=0), "$2.00 verified saved (n=3) vs base-x"),
    (dict(realized_usd=0.0, realized_n=0, unverified_usd=0.0, unverified_n=0), "est. saved $0.00 (n=0) vs base-x"),
])
def test_display_leaves_out_a_segment_with_no_rows(kw, expect):
    assert _summary(**kw).display() == expect


def test_compact_and_fragment_say_saved_not_measured():
    assert _summary().compact() == "~$5.00 est. saved · $2.00 verified saved"
    assert "measured" not in money_fragment(2.0, 5.0)


def test_work_hours_without_the_rate_n_is_na_not_a_number():
    assert units.render(3.0, 5, "7d", kind="estimated", unit="subscription", quantity="work_hours",
                        n_basis=None, with_n=True) == "n/a"
    assert units.render(3.0, 5, "7d", kind="estimated", unit="subscription", quantity="work_hours",
                        n_basis=10, with_n=True) == "~3 work-hours (n=5, rate n=10, 7d)"


def test_no_money_surface_prints_measured_next_to_a_dollar_figure():
    """Source scan over the files that format the verified figure. `measured` next to `$` or `usd`
    in a string literal would put the word back on a user surface."""
    files = ["ledger_view.py", "dashboard_data.py", "statusline_segments.py", "hooks/session-end.py",
             "hooks/codex-stop.py", "hooks/statusline-command.sh"]
    pat = re.compile(r'''["'`][^"'`\n]*(\$[^"'`\n]*\bmeasured\b|\bmeasured\b[^"'`\n]*\$)''')
    seen = 0
    bad = []
    for f in files:
        p = SRC / f
        assert p.is_file(), f
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            seen += 1
            if pat.search(line):
                bad.append(f"{f}:{i}: {line.strip()[:90]}")
    assert seen > 1000, "the scan found nothing to scan"
    assert not bad, bad
    # the scan catches what it claims to
    assert pat.search('parts.append(f"${v:,.2f} measured")')


def test_weekly_quota_negative_does_not_mask_the_monthly_price(monkeypatch):
    from llm_router import quota_savings as qs

    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD", "-5")
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH", "100")
    assert qs._default_weekly_quota_usd() == pytest.approx(100.0 / 4.345)
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD", "0")
    assert qs._default_weekly_quota_usd() == pytest.approx(100.0 / 4.345)
    monkeypatch.delenv("LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH")
    assert qs._default_weekly_quota_usd() is None            # invalid and nothing else: unknown, not -5


async def test_quota_saved_na_message_names_all_three_env_vars():
    from unittest.mock import MagicMock, patch

    from llm_router.tools import subscription as sub

    snap = MagicMock()
    snap.calibration_usd_per_pp = None
    snap.weekly_current_pct = snap.session_current_pct = 10.0
    with patch("llm_router.quota_savings.compute_quota_savings", return_value=snap):
        out = await sub.llm_quota_saved()
    for k in ("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "LLM_ROUTER_WEEKLY_QUOTA_USD",
              "LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH"):
        assert k in out, out
    assert "n/a" in out
