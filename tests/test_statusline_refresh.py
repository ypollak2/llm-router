"""The status line's background refresh: what it caches, and what it calls unknown."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from llm_router import paths, statusline_refresh as sr

NOW = 1_791_234_000.0


@pytest.fixture
def no_kpi(monkeypatch):
    """The scorecard stands in as a fixed reading; this file tests the cache, not the KPI."""
    card = {"kpis": {
        "NS": {"value": "0.0% (n=3599)", "n": 3599, "measurable": True, "numerator": 0, "denominator": 3599},
        "G1_hook": {"hooks": {
            "auto-route": {"n": 93, "budget_ms": 60000, "p50_ms": 189.1, "p95_ms": 3002.3},
            "session-end": {"n": 59, "budget_ms": 10000, "p95_ms": 2716.5},
            "status-bar": {"n": 93, "budget_ms": 2000, "p95_ms": 66.8},
            "cc-usage-track": {"n": 26, "budget_ms": 2000},
        }},
    }}
    import llm_router.commands.kpi as kpi

    monkeypatch.setattr(kpi, "compute_scorecard", lambda days, now=None, **_: card)
    return card


def _usage(**fields):
    p = paths.state_path("usage.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fields))


def test_empty_home_is_all_unknown(monkeypatch):
    import llm_router.commands.kpi as kpi

    def boom(**_):
        raise RuntimeError("no ledgers")

    monkeypatch.setattr(kpi, "compute_scorecard", boom)
    data = sr.build(NOW)
    assert data["ns"] is None and data["claude_weekly_pct"] is None
    assert data["codex"] is None and data["hooks_slow"] is None
    assert data["written_at"] == NOW


def test_ns_and_hooks_from_the_kpi_reading(no_kpi):
    data = sr.build(NOW)
    assert data["ns"] == {"pct": 0.0, "n": 3599}
    # auto-route is over the 1 s per-prompt threshold; session-end is not counted
    # against it (once a session) and is within its own budget.
    assert data["hooks_slow"] == {"hook": "auto-route", "p95_ms": 3002.3}


def test_ns_too_few_keeps_n_without_a_share(no_kpi):
    no_kpi["kpis"]["NS"] = {"value": "too few (n=12)", "n": 12, "measurable": False}
    assert sr.build(NOW)["ns"] == {"pct": None, "n": 12}


def test_ns_not_measurable_is_unknown(no_kpi):
    no_kpi["kpis"]["NS"] = {"value": "not measurable: x", "n": None, "measurable": False}
    assert sr.build(NOW)["ns"] is None


def test_hooks_over_budget_marks_even_session_hooks(no_kpi, monkeypatch):
    no_kpi["kpis"]["G1_hook"]["hooks"] = {"session-end": {"budget_ms": 1000, "p95_ms": 2500.0}}
    assert sr.build(NOW)["hooks_slow"] == {"hook": "session-end", "p95_ms": 2500.0}
    no_kpi["kpis"]["G1_hook"]["hooks"] = {"auto-route": {"budget_ms": 60000, "p95_ms": 800.0}}
    assert sr.build(NOW)["hooks_slow"] is None
    monkeypatch.setenv("LLM_ROUTER_STATUSLINE_SLOW_MS", "500")
    assert sr.build(NOW)["hooks_slow"] == {"hook": "auto-route", "p95_ms": 800.0}


@pytest.mark.parametrize("fields,expect", [
    ({"weekly_pct": 41.0, "updated_at": NOW - 60}, 41.0),
    ({"weekly_pct": 0.0, "updated_at": NOW - 60}, 0.0),            # a measured zero stays
    ({"weekly_pct": 50.0, "updated_at": NOW - 60, "is_fallback": True}, None),
    ({"weekly_pct": 41.0, "updated_at": NOW - 7200}, None),        # stale
    ({"weekly_pct": True, "updated_at": NOW - 60}, None),
    ({"updated_at": NOW - 60}, None),
])
def test_quota(no_kpi, fields, expect):
    _usage(**fields)
    assert sr.build(NOW)["claude_weekly_pct"] == expect


def test_codex_unknown_until_counted(no_kpi):
    assert sr.build(NOW)["codex"] is None
    p = paths.state_path("codex_window.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"delegations": [NOW - 100, NOW - 50]}))
    codex = sr.build(NOW)["codex"]
    assert codex["used"] == 2 and codex["budget"] >= 1
    assert codex["resets_at"] == pytest.approx(NOW - 100 + 5 * 3600)


def test_refresh_writes_private_cache_the_tick_reads(no_kpi):
    from llm_router import statusline_tick as tick

    assert sr.refresh(now=time.time()) is True
    p = sr.cache_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    line = tick.render(tick.read_cache(str(paths.llm_router_home())), now=time.time())
    assert "NS 0.0% n=3599" in line


def test_second_refresher_backs_off(no_kpi):
    from llm_router.file_lock import exclusive_lock

    with exclusive_lock(paths.state_path(sr.LOCK_NAME), timeout=0.0) as held:
        assert held
        assert sr.refresh(now=NOW) is False
    assert sr.refresh(now=NOW) is True


def test_refresh_command_arms_a_watchdog(no_kpi, monkeypatch):
    import signal

    armed = []
    monkeypatch.setattr(signal, "alarm", lambda s: armed.append(s))
    assert sr.cmd_statusline(["--refresh"]) == 0
    assert armed == [sr.REFRESH_TIMEOUT_S]
