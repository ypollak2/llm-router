"""Tests for team.py's WS7/C8 enrichment: fleet-wide realized-savings and
misroute-rate context added to the team savings report and its push payloads.

Tests cover:
- _add_ws23_context(): presence of new keys, fail-open behavior, period->window mapping
- build_team_report(): new keys included alongside the existing scoped fields
- Slack/Discord/Telegram payload builders: new fields surfaced, None-safe
- Brand-leak: no "chuzom" anywhere in the module or its output
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.team import (
    _add_ws23_context,
    _discord_payload,
    _fmt_pct_or_na,
    _fmt_usd_or_na,
    _slack_payload,
    _telegram_message,
    build_team_report,
)


# ── _fmt_usd_or_na / _fmt_pct_or_na ─────────────────────────────────────────

def test_fmt_usd_or_na_formats_value():
    assert _fmt_usd_or_na(1.23456) == "$1.2346"


def test_fmt_usd_or_na_none_is_na():
    assert _fmt_usd_or_na(None) == "N/A"


def test_fmt_pct_or_na_formats_value():
    assert _fmt_pct_or_na(0.125) == "12.5%"


def test_fmt_pct_or_na_none_is_na():
    assert _fmt_pct_or_na(None) == "N/A"


# ── _add_ws23_context ────────────────────────────────────────────────────────

def test_add_ws23_context_includes_new_keys():
    """The report dict must always gain both new keys, regardless of the
    underlying data source's success (fail-open -> None otherwise)."""
    report = _add_ws23_context({}, "week")

    assert "realized_savings_usd" in report
    assert "mis_route_rate_inferred" in report


def test_add_ws23_context_fails_open_on_realized_savings_error():
    """A broken WS3 dashboard_data module must never raise — the field
    falls back to None."""
    with patch(
        "llm_router.dashboard_data.query_realized_savings",
        side_effect=RuntimeError("db unavailable"),
    ):
        report = _add_ws23_context({}, "week")

    assert report["realized_savings_usd"] is None


def test_add_ws23_context_fails_open_on_quality_baseline_error():
    """A broken WS2 routing_quality module must never raise — the field
    falls back to None."""
    with patch(
        "llm_router.routing_quality.summarize",
        side_effect=RuntimeError("ledger unavailable"),
    ):
        report = _add_ws23_context({}, "week")

    assert report["mis_route_rate_inferred"] is None


def test_add_ws23_context_maps_all_period_to_lifetime_window():
    """team.py's 'all' period has no WindowLiteral equivalent — it must be
    mapped to dashboard_data's 'lifetime' rather than raising or being
    passed through literally."""
    with patch("llm_router.dashboard_data.query_realized_savings") as mock_query:
        mock_query.return_value.realized_savings_usd = 42.0
        _add_ws23_context({}, "all")

    mock_query.assert_called_once_with("lifetime")


def test_add_ws23_context_passes_through_known_periods():
    with patch("llm_router.dashboard_data.query_realized_savings") as mock_query:
        mock_query.return_value.realized_savings_usd = 1.0
        _add_ws23_context({}, "month")

    mock_query.assert_called_once_with("month")


def test_add_ws23_context_unknown_period_defaults_to_week():
    with patch("llm_router.dashboard_data.query_realized_savings") as mock_query:
        mock_query.return_value.realized_savings_usd = 1.0
        _add_ws23_context({}, "some-unrecognized-period")

    mock_query.assert_called_once_with("week")


# ── build_team_report ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_team_report_includes_ws23_context_keys():
    """The report returned to callers (CLI + push payloads) must carry the
    new fleet-wide keys alongside the existing user/project-scoped fields."""
    fake_savings = {
        "total_calls": 10,
        "saved_usd": 1.5,
        "actual_usd": 0.5,
        "free_pct": 0.6,
        "top_models": [],
    }
    with patch("llm_router.cost.get_team_savings", new=AsyncMock(return_value=fake_savings)):
        report = await build_team_report(user_id="u1", project_id="p1", period="week")

    assert report["total_calls"] == 10
    assert report["saved_usd"] == 1.5
    assert "realized_savings_usd" in report
    assert "mis_route_rate_inferred" in report


@pytest.mark.asyncio
async def test_build_team_report_context_fails_open_end_to_end():
    """Even if both WS2 and WS3 sources fail, build_team_report() must still
    return a usable report with the base scoped fields intact."""
    fake_savings = {
        "total_calls": 0,
        "saved_usd": 0.0,
        "actual_usd": 0.0,
        "free_pct": 0.0,
        "top_models": [],
    }
    with patch("llm_router.cost.get_team_savings", new=AsyncMock(return_value=fake_savings)):
        with patch(
            "llm_router.dashboard_data.query_realized_savings",
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "llm_router.routing_quality.summarize",
                side_effect=RuntimeError("boom"),
            ):
                report = await build_team_report(user_id="u1", project_id="p1", period="today")

    assert report["realized_savings_usd"] is None
    assert report["mis_route_rate_inferred"] is None
    assert report["total_calls"] == 0


# ── Payload builders surface the new fields ─────────────────────────────────

def _report_with_context(**overrides):
    report = {
        "user_id": "u1",
        "project_id": "p1",
        "period": "week",
        "total_calls": 5,
        "saved_usd": 1.0,
        "actual_usd": 0.5,
        "free_pct": 0.5,
        "top_models": [],
        "realized_savings_usd": 3.14,
        "mis_route_rate_inferred": 0.05,
    }
    report.update(overrides)
    return report


def test_slack_payload_surfaces_context_fields():
    payload = _slack_payload(_report_with_context())
    text = str(payload)
    assert "$3.1400" in text
    assert "5.0%" in text


def test_slack_payload_handles_missing_context_fields():
    """Payload builders must not raise when the context fields are absent
    (e.g. a report built before WS7's enrichment landed)."""
    report = {
        "user_id": "u1", "project_id": "p1", "period": "week",
        "total_calls": 5, "saved_usd": 1.0, "actual_usd": 0.5,
        "free_pct": 0.5, "top_models": [],
    }
    payload = _slack_payload(report)
    assert "N/A" in str(payload)


def test_discord_payload_surfaces_context_fields():
    payload = _discord_payload(_report_with_context())
    text = str(payload)
    assert "$3.1400" in text
    assert "5.0%" in text


def test_discord_payload_handles_missing_context_fields():
    report = {
        "user_id": "u1", "project_id": "p1", "period": "week",
        "total_calls": 5, "saved_usd": 1.0, "actual_usd": 0.5,
        "free_pct": 0.5, "top_models": [],
    }
    payload = _discord_payload(report)
    assert "N/A" in str(payload)


def test_telegram_message_surfaces_context_fields():
    payload = _telegram_message(_report_with_context(), chat_id="-100123")
    assert "3\\.1400" in payload["text"] or "3.1400" in payload["text"]


def test_telegram_message_handles_missing_context_fields():
    report = {
        "user_id": "u1", "project_id": "p1", "period": "week",
        "total_calls": 5, "saved_usd": 1.0, "actual_usd": 0.5,
        "free_pct": 0.5, "top_models": [],
    }
    payload = _telegram_message(report, chat_id="-100123")
    assert "N/A" in payload["text"]


# ── Brand Leak Tests (WS7) ──────────────────────────────────────────────────

def test_team_module_has_no_unallowed_brand_leak():
    """team.py's C8 enrichment is new design (Chuzom's own team.py has no
    equivalent to port literally) — no provenance header is used, so no
    'chuzom' substring is allowed anywhere in the module's names."""
    import llm_router.team as team

    for name in dir(team):
        assert "chuzom" not in name.lower(), f"brand leak in name: {name}"


def test_team_payloads_never_leak_brand():
    report = _report_with_context()
    assert "chuzom" not in str(_slack_payload(report)).lower()
    assert "chuzom" not in str(_discord_payload(report)).lower()
    assert "chuzom" not in str(_telegram_message(report, chat_id="-100123")).lower()
