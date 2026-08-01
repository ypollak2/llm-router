"""Invariant tests for the centralized dashboard_data module.

These tests are the structural prevention layer for the v9.3 schema-drift
bug class. Each panel in the dashboard reads from the data module; the
tests below pin two contracts:

1. ``query_window`` totals equal the sum of its ``by_source`` breakdown
   for every supported window — there's no way for a source to be
   counted in totals but missing from the breakdown (or vice versa).

2. Cross-consumer agreement: the migrated session-end functions
   ``_query_cumulative_savings`` and ``_query_daily_14d`` produce
   totals that match ``query_window``'s output for the same window.
   A future regression in either consumer would break this.

3. ``explain-dashboard --check`` exit-non-zero canary: when a source
   table has rows for a window, the canary must NOT report drift if
   ``query_window`` includes that table.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def fake_db(tmp_path):
    """Temp usage.db with rows in every v9.3-relevant table.

    Returns the Path; tests pass it into ``query_window(db_path=...)``.
    """
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(str(db))
    # Legacy `usage` table.
    conn.execute("""CREATE TABLE usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        model TEXT NOT NULL,
        provider TEXT NOT NULL,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        saved_usd REAL DEFAULT 0.0,
        success INTEGER DEFAULT 1
    )""")
    conn.execute(
        "INSERT INTO usage (model, provider, input_tokens, output_tokens, "
        "cost_usd, saved_usd, success) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ollama/qwen2.5:7b", "ollama", 100, 50, 0.0, 0.0021, 1),
    )
    # Per-platform tables.
    for table in ("claude_usage", "codex_usage", "gemini_usage"):
        conn.execute(f"""CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            model TEXT NOT NULL,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            complexity TEXT NOT NULL DEFAULT 'auto',
            cost_saved_usd REAL NOT NULL DEFAULT 0
        )""")
    conn.execute(
        "INSERT INTO claude_usage (model, tokens_used, complexity, cost_saved_usd) "
        "VALUES (?, ?, ?, ?)",
        ("claude-haiku-4-5", 2000, "auto", 0.40),
    )
    conn.execute(
        "INSERT INTO codex_usage (model, tokens_used, complexity, cost_saved_usd) "
        "VALUES (?, ?, ?, ?)",
        ("gpt-5.4", 500, "auto", 0.05),
    )
    # savings_stats (no token columns).
    conn.execute("""CREATE TABLE savings_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        session_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        estimated_claude_cost_saved REAL NOT NULL,
        external_cost REAL NOT NULL,
        model_used TEXT NOT NULL,
        host TEXT NOT NULL DEFAULT 'claude_code'
    )""")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, "
        "estimated_claude_cost_saved, external_cost, model_used) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now, "s1", "code", 0.02, 0.0, "ollama/qwen2.5:7b"),
    )
    conn.commit()
    conn.close()
    return db


# ── 1. Internal consistency of dashboard_data ────────────────────────────────


def test_query_window_totals_equal_by_source_sum(fake_db):
    """Every source contributing to totals must appear in by_source."""
    from llm_router.dashboard_data import query_window

    for window in ("today", "week", "month", "lifetime"):
        totals = query_window(window, db_path=fake_db)
        assert totals.calls == sum(s["calls"] for s in totals.by_source.values()), (
            f"{window}: calls sum mismatch — totals={totals.calls}, "
            f"by_source={[(k,v['calls']) for k,v in totals.by_source.items()]}"
        )
        assert totals.tokens == sum(
            s.get("tokens", 0) for s in totals.by_source.values()
        ), f"{window}: tokens sum mismatch"
        assert abs(totals.saved_usd - sum(
            s["saved_usd"] for s in totals.by_source.values()
        )) < 1e-9, f"{window}: savings sum mismatch"


def test_query_daily_sum_matches_query_window_14d(fake_db):
    """Daily rollup summed across days must equal the 14d window total."""
    from llm_router.dashboard_data import query_daily, query_window

    daily = query_daily(14, db_path=fake_db)
    window = query_window("14d", db_path=fake_db)

    assert sum(r.calls for r in daily) == window.calls, (
        f"daily total calls {sum(r.calls for r in daily)} != "
        f"window 14d calls {window.calls}"
    )
    assert sum(r.tokens for r in daily) == window.tokens, (
        "daily total tokens != window 14d tokens"
    )
    assert abs(sum(r.saved_usd for r in daily) - window.saved_usd) < 1e-9, (
        "daily total savings != window 14d savings"
    )


def test_query_by_platform_attribution(fake_db):
    """Per-platform rows must sum to the same totals as query_window."""
    from llm_router.dashboard_data import query_by_platform, query_window

    totals = query_window("today", db_path=fake_db)
    rows = query_by_platform("today", db_path=fake_db)
    assert sum(r.calls for r in rows) == totals.calls
    assert sum(r.tokens for r in rows) == totals.tokens
    assert abs(sum(r.saved_usd for r in rows) - totals.saved_usd) < 1e-9


# ── 2. Cross-consumer agreement (the regression-prevention layer) ────────────


def _load_session_end_module():
    """Load session-end.py as a module despite the dash in its filename."""
    spec = importlib.util.spec_from_file_location(
        "session_end_invariant_test",
        PROJECT_ROOT / "src" / "llm_router" / "hooks" / "session-end.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_end_cumulative_matches_query_window(fake_db, monkeypatch):
    """`_query_cumulative_savings` today row ≡ `query_window('today')`.

    Regression guard for the schema-drift bug class: if either consumer
    is updated without the other (e.g., session-end starts skipping a
    table again), this test breaks.
    """
    from llm_router.dashboard_data import query_window

    se = _load_session_end_module()
    monkeypatch.setattr(se, "DB_PATH", str(fake_db))

    cumulative = se._query_cumulative_savings()
    today_row = next((r for r in cumulative if r[0] == "today"), None)
    assert today_row is not None, "today row missing from cumulative output"

    _label, calls, total_in, total_out, saved = today_row
    window = query_window("today", db_path=fake_db)

    assert calls == window.calls
    assert (total_in + total_out) == window.tokens
    assert abs(saved - window.saved_usd) < 1e-9


def test_session_end_daily_matches_query_daily(fake_db, monkeypatch):
    """`_query_daily_14d` ≡ `query_daily(14)` for the same DB."""
    from llm_router.dashboard_data import query_daily

    se = _load_session_end_module()
    monkeypatch.setattr(se, "DB_PATH", str(fake_db))

    legacy = se._query_daily_14d()
    canonical = query_daily(14, db_path=fake_db)

    assert len(legacy) == len(canonical)
    for (l_day, l_calls, l_tokens, l_saved), c in zip(legacy, canonical):
        assert l_day == c.day
        assert l_calls == c.calls
        assert l_tokens == c.tokens
        assert abs(l_saved - c.saved_usd) < 1e-9


# ── 3. CI canary ─────────────────────────────────────────────────────────────


def test_canary_returns_zero_on_clean_db(fake_db, monkeypatch):
    """explain-dashboard --check must exit 0 when all sources are read."""
    # Point the canary at our test DB by patching DEFAULT_DB_PATH.
    from llm_router import dashboard_data as dd
    monkeypatch.setattr(dd, "DEFAULT_DB_PATH", fake_db)

    from llm_router.commands.explain_dashboard import _check_mode_canary
    rc = _check_mode_canary()
    assert rc == 0, "canary failed on a clean DB"


def test_canary_via_cli(fake_db, monkeypatch):
    """Invoke `cmd_explain_dashboard(["--check"])` routes to the canary."""
    # The CLI resolves DEFAULT_DB_PATH from ~/.llm-router; we patch the
    # module constant rather than relocating $HOME for the subprocess.
    from llm_router import dashboard_data as dd
    from llm_router.commands.explain_dashboard import cmd_explain_dashboard
    monkeypatch.setattr(dd, "DEFAULT_DB_PATH", fake_db)
    rc = cmd_explain_dashboard(["--check"])
    assert rc == 0


# ── 4. Realized savings (WS3/C6) ─────────────────────────────────────────────
#
# query_realized_savings() reads execution_ledger.py's Gate 18 accounting
# (a separate `execution_events` table) rather than the usage/*_usage/
# savings_stats tables the rest of this file exercises. These tests pin:
# fail-open behavior on a missing DB/table (without side-effecting either
# into existence), the adoption-gated realized-vs-potential split, a late
# `route_realized` update being reflected, and non-interference with
# query_window's existing totals.


def test_query_realized_savings_missing_db_returns_empty_without_creating_it(tmp_path):
    from llm_router.dashboard_data import query_realized_savings

    missing_db = tmp_path / "does-not-exist.db"
    result = query_realized_savings("lifetime", db_path=missing_db)

    assert result.realized_savings_usd == 0.0
    assert result.potential_savings_usd == 0.0
    assert result.realized_routes == 0
    assert not missing_db.exists(), (
        "query_realized_savings must never create usage.db as a read side effect"
    )


def test_query_realized_savings_missing_table_returns_empty_without_creating_it(fake_db):
    """fake_db has usage/claude_usage/... but no execution_events table yet —
    query_realized_savings must not silently create it as a side effect of
    connecting into execution_ledger (unlike calling execution_ledger directly,
    whose `_connect()` always runs `CREATE TABLE IF NOT EXISTS`)."""
    from llm_router.dashboard_data import query_realized_savings

    result = query_realized_savings("lifetime", db_path=fake_db)
    assert result.realized_savings_usd == 0.0
    assert result.potential_savings_usd == 0.0

    conn = sqlite3.connect(str(fake_db))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_events'"
        )
        assert cur.fetchone() is None, (
            "query_realized_savings must not create execution_events as a side effect"
        )
    finally:
        conn.close()


def test_query_realized_savings_gates_on_adoption(fake_db):
    from llm_router import execution_ledger as el
    from llm_router.dashboard_data import query_realized_savings

    def _route(route_id, *, realization_status, adoption_method):
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-attempt",
                route_id=route_id,
                event_type="attempt_completed",
                measured_cost_usd=0.01,
                baseline_equivalent_cost_usd=0.10,
            ),
            path=fake_db,
        )
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-realized",
                route_id=route_id,
                event_type="route_realized",
                realization_status=realization_status,
                adoption_method=adoption_method,
            ),
            path=fake_db,
        )

    _route("dd-r1", realization_status="verified_used", adoption_method="door_call")
    _route("dd-r2", realization_status="verified_overridden", adoption_method=None)

    result = query_realized_savings("lifetime", db_path=fake_db)
    assert result.potential_savings_usd == pytest.approx(0.18)
    assert result.realized_savings_usd == pytest.approx(0.09)
    assert result.realized_routes == 1
    assert result.overridden_routes == 1


def test_query_realized_savings_reflects_late_route_realized_update(fake_db):
    from llm_router import execution_ledger as el
    from llm_router.dashboard_data import query_realized_savings

    el.record_event(
        el.LedgerEvent(
            event_id="dd-late-attempt",
            route_id="dd-late",
            event_type="attempt_completed",
            measured_cost_usd=0.01,
            baseline_equivalent_cost_usd=0.10,
        ),
        path=fake_db,
    )
    el.record_event(
        el.LedgerEvent(
            event_id="dd-late-realized-1",
            route_id="dd-late",
            event_type="route_realized",
            realization_status="unknown",
        ),
        path=fake_db,
    )
    before = query_realized_savings("lifetime", db_path=fake_db)
    assert before.realized_savings_usd == 0.0

    # A later route_realized event confirms the route was actually adopted.
    el.record_event(
        el.LedgerEvent(
            event_id="dd-late-realized-2",
            route_id="dd-late",
            event_type="route_realized",
            realization_status="verified_used",
            adoption_method="door_call",
        ),
        path=fake_db,
    )
    after = query_realized_savings("lifetime", db_path=fake_db)
    assert after.realized_savings_usd == pytest.approx(0.09)


def test_query_realized_savings_does_not_affect_query_window(fake_db):
    """Adding execution_events rows (and reading them via
    query_realized_savings) must never change query_window's totals — the
    two are independent, non-reconciled figures by design."""
    from llm_router import execution_ledger as el
    from llm_router.dashboard_data import query_realized_savings, query_window

    before = query_window("lifetime", db_path=fake_db)

    el.record_event(
        el.LedgerEvent(
            event_id="dd-isolation-attempt",
            route_id="dd-isolation",
            event_type="attempt_completed",
            measured_cost_usd=0.01,
            baseline_equivalent_cost_usd=0.10,
        ),
        path=fake_db,
    )
    query_realized_savings("lifetime", db_path=fake_db)

    after = query_window("lifetime", db_path=fake_db)
    assert after.calls == before.calls
    assert after.saved_usd == pytest.approx(before.saved_usd)
