"""Tests for the quota_savings metric.

Pins:

* Time-window math (UTC Monday start; 5h rolling).
* Calibration constant from env var with safe fallback.
* DB-missing / column-missing fallback to 0.0 (must never raise).
* Snapshot arithmetic + ``short_form`` / ``is_meaningful`` predicates.
* Formatter injection — routing notice gets the suffix when meaningful,
  unchanged otherwise.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_router.tool_surface import route_tool
from llm_router.quota_savings import (
    QuotaSavingsSnapshot,
    _start_of_5h_window_utc,
    _start_of_week_utc,
    compute_quota_savings,
)


# ── 1. Time windows ────────────────────────────────────────────────────────


def test_start_of_week_is_utc_monday_midnight() -> None:
    # Wed 2026-06-10 09:42 UTC → Mon 2026-06-08 00:00 UTC
    wed = datetime(2026, 6, 10, 9, 42, tzinfo=timezone.utc)
    monday = _start_of_week_utc(now=wed)
    assert monday == datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc)


def test_start_of_week_on_monday_returns_today_midnight() -> None:
    mon = datetime(2026, 6, 8, 15, 30, tzinfo=timezone.utc)
    monday = _start_of_week_utc(now=mon)
    assert monday == datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc)


def test_start_of_week_on_sunday_returns_previous_monday() -> None:
    sun = datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc)
    monday = _start_of_week_utc(now=sun)
    assert monday == datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc)


def test_5h_window_is_exactly_5_hours_back() -> None:
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    cutoff = _start_of_5h_window_utc(now=now)
    assert cutoff == now - timedelta(hours=5)


# ── 2. Snapshot predicates ─────────────────────────────────────────────────


def _snap(weekly_pp: float, session_pp: float) -> QuotaSavingsSnapshot:
    return QuotaSavingsSnapshot(
        weekly_current_pct=40.0,
        weekly_pp_saved=weekly_pp,
        weekly_counterfactual_pct=40.0 + weekly_pp,
        weekly_saved_usd=weekly_pp * 0.5,
        session_current_pct=1.0,
        session_pp_saved=session_pp,
        session_counterfactual_pct=1.0 + session_pp,
        session_saved_usd=session_pp * 0.5,
        calibration_usd_per_pp=0.5,
        calibration_source="configured",
    )


def test_short_form_uses_one_decimal_pp() -> None:
    s = _snap(weekly_pp=7.2, session_pp=3.41)
    assert s.short_form() == "saved 7.2pp wk / 3.4pp 5h"


def test_is_meaningful_above_threshold() -> None:
    assert _snap(weekly_pp=7.0, session_pp=0.0).is_meaningful() is True
    assert _snap(weekly_pp=0.0, session_pp=3.0).is_meaningful() is True


def test_is_not_meaningful_below_threshold() -> None:
    assert _snap(weekly_pp=0.3, session_pp=0.1).is_meaningful() is False


def test_is_meaningful_respects_custom_threshold() -> None:
    s = _snap(weekly_pp=1.0, session_pp=0.0)
    assert s.is_meaningful(threshold_pp=0.5) is True
    assert s.is_meaningful(threshold_pp=2.0) is False


# ── 3. DB query — happy path + missing-DB fallback ─────────────────────────


def _make_usage_db(db_path: Path, rows: list[tuple[str, float]]) -> None:
    """Create a minimal ``usage`` table matching cost.py's schema and
    populate with ``(timestamp, saved_usd)`` rows."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                model TEXT NOT NULL DEFAULT 'x',
                provider TEXT NOT NULL DEFAULT 'x',
                task_type TEXT NOT NULL DEFAULT 'x',
                profile TEXT NOT NULL DEFAULT 'x',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                latency_ms REAL NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                saved_usd REAL DEFAULT 0.0
            )
            """
        )
        for ts, saved in rows:
            conn.execute(
                "INSERT INTO usage (timestamp, saved_usd) VALUES (?, ?)",
                (ts, saved),
            )
        conn.commit()
    finally:
        conn.close()


class _FakeCachedUsage:
    """Stand-in for ClaudeSubscriptionUsage. Fractions, not percentages."""

    def __init__(self, weekly: float, session: float) -> None:
        self.weekly_pct = weekly
        self.session_pct = session


def test_compute_returns_none_when_no_cached_usage() -> None:
    with patch("llm_router.state.get_last_usage", return_value=None):
        assert compute_quota_savings() is None


def test_compute_handles_missing_db_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No DB file → savings = 0.0, snapshot still returned (so the
    routing notice can show the *current* % without breaking)."""
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(tmp_path / "missing.db"))
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.4, session=0.01),
    ):
        snap = compute_quota_savings()
    assert snap is not None
    assert snap.weekly_saved_usd == pytest.approx(0.0)
    assert snap.session_saved_usd == pytest.approx(0.0)
    assert snap.weekly_current_pct == pytest.approx(40.0)
    assert snap.session_current_pct == pytest.approx(1.0)


def test_compute_aggregates_weekly_and_5h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: rows inside the 5h window contribute to both 5h
    and weekly totals; rows older than 5h but within the week
    contribute to weekly only."""
    db = tmp_path / "usage.db"
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    inside_5h = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    inside_week_outside_5h = (
        now - timedelta(hours=24)
    ).strftime("%Y-%m-%d %H:%M:%S")
    last_week = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    _make_usage_db(
        db,
        [
            (inside_5h, 1.0),
            (inside_week_outside_5h, 2.0),
            (last_week, 99.0),  # must NOT contribute
        ],
    )

    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(db))
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "50")
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.40, session=0.01),
    ):
        snap = compute_quota_savings(now=now)

    assert snap is not None
    assert snap.weekly_saved_usd == pytest.approx(3.0)  # 1.0 + 2.0
    assert snap.session_saved_usd == pytest.approx(1.0)
    # This test PINS the calibration via the env var set above, so it measures
    # the aggregation arithmetic rather than the default. Left at 0.50 on
    # purpose: swapping in the derived default here would couple an arithmetic
    # test to the default's derivation and make both harder to read.
    # $50/week → $0.50/pp → $3 = 6.0 pp weekly, $1 = 2.0 pp session
    assert snap.calibration_usd_per_pp == pytest.approx(0.50)
    assert snap.weekly_pp_saved == pytest.approx(6.0)
    assert snap.session_pp_saved == pytest.approx(2.0)
    assert snap.weekly_counterfactual_pct == pytest.approx(46.0)
    assert snap.session_counterfactual_pct == pytest.approx(3.0)


# ── 4. Calibration env var ────────────────────────────────────────────────


def test_calibration_default_tracks_the_subscription_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", raising=False)
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.0, session=0.0),
    ):
        snap = compute_quota_savings()
    assert snap is not None
    # RED8-05 / WP-05: was a flat $50/week -> $0.50/pp, and the $50 was
    # justified in-comment as an Opus equivalent at $15/$75 per million —
    # the RETIRED Opus 3 rate. The test name even froze the number.
    #
    # Quota value is anchored to what the SUBSCRIPTION COSTS, not to a token
    # rate: $200/month over 4.345 weeks. Under the old reasoning this figure
    # would have had to fall by two thirds when Opus repriced, and nothing
    # here would have noticed.
    assert snap.calibration_usd_per_pp == pytest.approx(200.0 / 4.345 / 100.0, abs=1e-4)


def test_calibration_respects_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "100")
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.0, session=0.0),
    ):
        snap = compute_quota_savings()
    assert snap is not None
    assert snap.calibration_usd_per_pp == pytest.approx(1.00)


def test_calibration_invalid_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "not-a-number")
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.0, session=0.0),
    ):
        snap = compute_quota_savings()
    assert snap is not None
    assert snap.calibration_usd_per_pp == pytest.approx(200.0 / 4.345 / 100.0, abs=1e-4)


def test_calibration_negative_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "-5")
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.0, session=0.0),
    ):
        snap = compute_quota_savings()
    assert snap is not None
    assert snap.calibration_usd_per_pp == pytest.approx(200.0 / 4.345 / 100.0, abs=1e-4)


# ── 5. llm_quota_saved tool surface ───────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_quota_saved_when_no_cached_usage() -> None:
    from llm_router.tools.subscription import llm_quota_saved
    with patch("llm_router.state.get_last_usage", return_value=None):
        out = await llm_quota_saved()
    assert "No cached Claude subscription usage" in out
    assert route_tool("llm_check_usage") in out


@pytest.mark.asyncio
async def test_llm_quota_saved_renders_full_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_router.tools.subscription import llm_quota_saved
    db = tmp_path / "usage.db"
    # Use a timestamp 2h ago so the record is always inside the current
    # week window regardless of when the test runs (avoids hardcoded dates
    # falling outside _start_of_week_utc when the real clock advances).
    inside_week = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    _make_usage_db(db, [(inside_week, 3.5)])
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(db))
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "50")
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.89, session=0.01),
    ):
        out = await llm_quota_saved()
    # Must surface both windows + both percentage and dollar framing.
    assert "Weekly" in out
    assert "5h" in out
    assert "89.0%" in out
    assert "would be" in out
    assert "$3.50" in out
    # Calibration provenance is documented so the user knows it's
    # configured, not magic.
    assert "Calibration" in out
    assert "configured" in out


# ── 6. Formatter integration ───────────────────────────────────────────────


def test_route_prefix_includes_savings_when_meaningful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: response_formatter.format_echo_context picks up the
    snapshot and appends the short form."""
    from types import SimpleNamespace
    from llm_router.hooks.response_formatter import format_echo_context

    db = tmp_path / "usage.db"
    # Use a timestamp 2h ago so the record is always inside the current
    # week window regardless of when the test runs (avoids hardcoded dates
    # falling outside _start_of_week_utc when the real clock advances).
    inside_week = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    _make_usage_db(db, [(inside_week, 5.0)])  # 5.0 / 0.5 = 10 pp wk
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(db))
    monkeypatch.setenv("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "50")

    result = SimpleNamespace(
        model=SimpleNamespace(provider="gemini", model="gemini-2.5-flash"),
        latency_ms=850,
        input_tokens=10,
        output_tokens=20,
        text="OK",
    )
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.40, session=0.01),
    ):
        ctx = format_echo_context(result, "analyze", "moderate")
    # The route_prefix carrying the savings short-form appears in the
    # injected directive text.
    assert "saved" in ctx
    assert "pp wk" in ctx


def test_route_prefix_omits_savings_when_not_meaningful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below-threshold deltas don't pollute the routing notice."""
    from types import SimpleNamespace
    from llm_router.hooks.response_formatter import format_echo_context

    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", "/nonexistent/usage.db")
    result = SimpleNamespace(
        model=SimpleNamespace(provider="gemini", model="gemini-2.5-flash"),
        latency_ms=850,
        input_tokens=10,
        output_tokens=20,
        text="OK",
    )
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.40, session=0.01),
    ):
        ctx = format_echo_context(result, "analyze", "moderate")
    # No DB → 0 savings → routing notice has no `saved … pp` suffix.
    assert "saved" not in ctx.split("🎯 LLM Router routed →")[1].split("\n")[0]


# ── 7. provider_route_hint — subscription vs API tier ─────────────────────


def _make_usage_db_with_costs(
    db_path: Path, rows: list[tuple[str, str, float]]
) -> None:
    """Create the usage table and populate (timestamp, provider, cost_usd)."""
    import sqlite3 as _sql
    conn = _sql.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                model TEXT NOT NULL DEFAULT 'x',
                provider TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT 'x',
                profile TEXT NOT NULL DEFAULT 'x',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL,
                latency_ms REAL NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                saved_usd REAL DEFAULT 0.0
            )
            """
        )
        for ts, prov, cost in rows:
            conn.execute(
                "INSERT INTO usage (timestamp, provider, cost_usd) VALUES (?, ?, ?)",
                (ts, prov, cost),
            )
        conn.commit()
    finally:
        conn.close()


def test_provider_hint_for_subscription_shows_remaining_quota() -> None:
    from llm_router.quota_savings import provider_route_hint
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.89, session=0.01),
    ):
        hint = provider_route_hint("anthropic")
    assert hint is not None
    assert "11%" in hint
    assert "99%" in hint
    assert "wk left" in hint
    assert "5h left" in hint


def test_provider_hint_for_cc_alias_also_works() -> None:
    """'cc' is the Claude Code subscription provider — same as anthropic."""
    from llm_router.quota_savings import provider_route_hint
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.50, session=0.10),
    ):
        hint = provider_route_hint("cc")
    assert hint is not None
    assert "50%" in hint


def test_provider_hint_for_subscription_without_cached_usage_returns_none() -> None:
    from llm_router.quota_savings import provider_route_hint
    with patch("llm_router.state.get_last_usage", return_value=None):
        assert provider_route_hint("anthropic") is None


def test_provider_hint_for_api_shows_30d_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_router.quota_savings import provider_route_hint
    db = tmp_path / "usage.db"
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    inside = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    outside = (now - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    _make_usage_db_with_costs(
        db,
        [
            (inside, "gemini", 0.50),
            (inside, "gemini", 0.73),
            (inside, "openai", 99.0),  # different provider — must not contribute
            (outside, "gemini", 88.0),  # outside window — must not contribute
        ],
    )
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(db))
    hint = provider_route_hint("gemini", now=now)
    assert hint is not None
    assert "30d on gemini" in hint
    assert "$1.23" in hint


def test_provider_hint_for_codex_uses_subscription_auth_path() -> None:
    """Codex authenticates via a ChatGPT subscription — actual_cost is
    always $0 in the usage table, so the original API path silently
    suppressed the hint. Codex now routes through the subscription-auth
    path and surfaces the Claude wk/5h numbers as a proxy for overall
    AI-routing pressure, prefixed with 'codex sub'."""
    from llm_router.quota_savings import provider_route_hint
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.89, session=0.01),
    ):
        hint = provider_route_hint("codex")
    assert hint is not None
    assert "codex sub" in hint
    assert "wk left" in hint
    assert "5h left" in hint
    assert "11%" in hint  # 100 - 89
    assert "99%" in hint  # 100 - 1


def test_provider_hint_for_codex_returns_none_without_cached_usage() -> None:
    """No cached snapshot → no proxy numbers to anchor on → None."""
    from llm_router.quota_savings import provider_route_hint
    with patch("llm_router.state.get_last_usage", return_value=None):
        assert provider_route_hint("codex") is None


def test_provider_hint_for_ollama_returns_none() -> None:
    """Free/local providers carry no metric."""
    from llm_router.quota_savings import provider_route_hint
    assert provider_route_hint("ollama") is None


def test_provider_hint_for_unknown_provider_returns_none() -> None:
    from llm_router.quota_savings import provider_route_hint
    assert provider_route_hint("some-future-provider") is None


def test_provider_hint_for_api_with_zero_spend_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No spend yet → no metric to show (avoid '30d on gemini: $0.00' noise)."""
    from llm_router.quota_savings import provider_route_hint
    db = tmp_path / "usage.db"
    _make_usage_db_with_costs(db, [])
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(db))
    assert provider_route_hint("gemini") is None


def test_route_prefix_appends_subscription_hint_for_anthropic_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from llm_router.hooks.response_formatter import format_echo_context
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", "/nonexistent/usage.db")
    result = SimpleNamespace(
        model=SimpleNamespace(provider="anthropic", model="claude-haiku-4-5"),
        latency_ms=300,
        input_tokens=10,
        output_tokens=20,
        text="OK",
    )
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.89, session=0.01),
    ):
        ctx = format_echo_context(result, "query", "simple")
    prefix_line = next(
        line for line in ctx.splitlines() if "🎯 LLM Router routed" in line
    )
    assert "wk left" in prefix_line
    assert "5h left" in prefix_line


def test_route_prefix_appends_api_hint_for_gemini_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from llm_router.hooks.response_formatter import format_echo_context
    db = tmp_path / "usage.db"
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _make_usage_db_with_costs(db, [(now_ts, "gemini", 1.23)])
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", str(db))
    result = SimpleNamespace(
        model=SimpleNamespace(provider="gemini", model="gemini-2.5-flash"),
        latency_ms=850,
        input_tokens=10,
        output_tokens=20,
        text="OK",
    )
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.40, session=0.01),
    ):
        ctx = format_echo_context(result, "analyze", "moderate")
    prefix_line = next(
        line for line in ctx.splitlines() if "🎯 LLM Router routed" in line
    )
    assert "30d on gemini" in prefix_line
    assert "$1.23" in prefix_line


def test_route_prefix_omits_hint_for_ollama_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from llm_router.hooks.response_formatter import format_echo_context
    monkeypatch.setenv("LLM_ROUTER_USAGE_DB_PATH", "/nonexistent/usage.db")
    result = SimpleNamespace(
        model=SimpleNamespace(provider="ollama", model="llama3"),
        latency_ms=120,
        input_tokens=10,
        output_tokens=20,
        text="OK",
    )
    with patch(
        "llm_router.state.get_last_usage",
        return_value=_FakeCachedUsage(weekly=0.40, session=0.01),
    ):
        ctx = format_echo_context(result, "query", "simple")
    prefix_line = next(
        line for line in ctx.splitlines() if "🎯 LLM Router routed" in line
    )
    assert "wk left" not in prefix_line
    assert "30d on" not in prefix_line
