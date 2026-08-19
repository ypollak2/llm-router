"""#24 / RED2-02 — a broken ledger must not broadcast "$0.0000" to Slack.

RED2-02 is "failure reads as zero". The remediation taught `llm_router.provenance`
that unknown is NOT zero, and applied it to several surfaces — `cost.py` carries
five `"provenance"` keys. It was never applied to `get_team_savings`, which has
**none** on either path, and whose result `team.py` broadcasts to Slack/Discord.

The chain, measured:

1. `cost.get_team_savings` — on a DB error returns
   ``{"total_calls": 0, "saved_usd": 0.0, "actual_usd": 0.0, "free_pct": 0.0,
   "top_models": []}``. Its own comment already says: *"Zeroes here render as
   'you routed nothing and saved nothing' — a working install that looks idle."*
   WP-13 records the failure via `failopen`, so it is COUNTED — but the counter
   is internal and the caller learns nothing.
2. `team.build_team_report` — copies fields with `.get(key, 0.0)`, so any
   extra key is silently dropped.
3. `team._savings_label` — renders ``"$0.0000 vs baseline (≈$0 cash on
   subscription)"``.

A team whose database is unreadable gets the same message as a team that had a
quiet week. That is the defect, and it is in a surface that PUBLISHES.

THESE TESTS ASSERT THE BROADCAST TEXT, not just the dict. Adding a provenance key
that no renderer reads would repeat the display-layer half of the same mistake —
the half that made RED2-02 user-visible in the first place.
"""

from __future__ import annotations

import pytest


# ── layer 1: the source must admit it does not know ──────────────────────────

@pytest.mark.asyncio
async def test_unreadable_ledger_reports_unknown_not_zero(monkeypatch, tmp_path):
    """Force the except path and assert the result says UNKNOWN.

    Points the DB at a tmpdir so this can never touch the developer's real
    ~/.llm-router/usage.db.
    """
    from llm_router import cost

    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "broken.db"))

    async def _boom(*a, **k):
        raise RuntimeError("simulated unreadable ledger")

    monkeypatch.setattr(cost, "_get_db", _boom)

    data = await cost.get_team_savings(period="week")
    assert data.get("provenance") == "unknown", (
        "a failed ledger read returns bare zeros with no marker — indistinguishable "
        "from a period in which nothing was routed"
    )


@pytest.mark.asyncio
async def test_a_successful_read_is_marked_measured(temp_db):
    """The tag must DISTINGUISH. If everything is 'unknown', or the key only
    appears on the error path, the field carries no information."""
    from llm_router import cost

    data = await cost.get_team_savings(period="week")
    assert data.get("provenance") == "measured"


# ── layer 3: the broadcast text is what a human actually reads ───────────────

def test_broadcast_line_never_shows_a_dollar_figure_when_unknown():
    from llm_router.team import _savings_label

    line = _savings_label({"provenance": "unknown"})
    assert "unknown" in line.lower(), f"broadcast line hides the failure: {line!r}"
    assert "$0.0000" not in line, (
        f"a failed read still renders as a dollar amount: {line!r}"
    )


def test_broadcast_line_is_unchanged_for_a_real_zero():
    """A genuinely quiet period must still read as $0 — the fix must not turn
    every quiet week into an alarm, or it gets ignored and the signal is lost."""
    from llm_router.team import _savings_label

    line = _savings_label({
        "provenance": "measured",
        "baseline_equivalent_avoided_usd": 0.0,
        "real_dollars_avoided_usd": 0.0,
    })
    assert "unknown" not in line.lower()
    assert "$0.0000" in line


def test_report_propagates_provenance():
    """build_team_report copies fields explicitly with .get(key, default), so
    a new key is DROPPED unless it is added there too. This is the step that
    turns a correct source into a silent display."""
    import inspect

    from llm_router import team

    src = inspect.getsource(team.build_team_report)
    assert "provenance" in src, (
        "build_team_report does not forward provenance, so the broadcast "
        "cannot distinguish unknown from zero however honest cost.py becomes"
    )
