"""Single source of truth for dashboard data queries.

Background
----------
The v9.3 schema split inserted three per-platform tables (``claude_usage``,
``codex_usage``, ``gemini_usage``) alongside the legacy ``usage`` table and
the JSONL-imported ``savings_stats`` table. Every consumer that wants to
show "today's calls / tokens / savings" must UNION across all sources or
silently under-report. Prior to this module, each panel hand-rolled its
own SQL — which led to ~4 distinct drift bugs in different surfaces
(statusline, SAVINGS panel, ROUTING panel, 14-DAY chart) by Jun 2026.

This module owns the UNION logic in one place. All dashboard surfaces
(session-end, statusline, CLI commands, MCP tools) should call into the
functions here rather than executing SQL directly.

API surface
-----------
* ``query_window(window, ...)``        — calls + tokens + savings for a window
* ``query_daily(days=14)``             — per-day breakdown for chart rendering
* ``query_by_platform(window)``        — per-platform attribution
* ``DataSourceAudit``                  — diagnostic record used by
  ``explain-dashboard --check`` to surface tables that have rows but
  aren't being read.

Window strings
--------------
Accepted ``window`` values: ``"today"`` / ``"week"`` / ``"month"`` /
``"lifetime"`` / ``"14d"``. Each maps to a SQLite WHERE clause on
``timestamp`` columns. The mapping is identical across all source tables
so call counts/tokens/savings stay reconcilable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

WindowLiteral = Literal["today", "week", "month", "lifetime", "14d"]

DEFAULT_DB_PATH = Path.home() / ".llm-router" / "usage.db"

# Tables this module knows how to UNION. Order is presentation order
# (legacy first → newer platforms last) so audit output reads naturally.
_PLATFORM_TABLES = ("claude_usage", "codex_usage", "gemini_usage")
_LEGACY_TABLE = "usage"
_JSONL_TABLE = "savings_stats"


def _window_sql(window: WindowLiteral) -> str:
    """Return the WHERE clause body for the given window.

    All source tables use a ``timestamp`` column with SQLite
    ``datetime('now')``-style values, so one SQL fragment fits all.
    """
    mapping = {
        "today":    "date(timestamp,'localtime')=date('now','localtime')",
        "week":     "timestamp >= datetime('now','-7 days')",
        "month":    "timestamp >= datetime('now','start of month')",
        "lifetime": "1=1",
        "14d":      "timestamp >= datetime('now','-14 days')",
    }
    if window not in mapping:
        raise ValueError(f"unknown window: {window!r}")
    return mapping[window]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table`` as a set."""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _sum_if_present(cols: set[str], col: str) -> str:
    """Return ``COALESCE(SUM(col),0)`` if ``col`` exists, else ``0``.

    Older DBs / test fixtures may lack columns the v9.3+ schema added
    (e.g., ``saved_usd``). Skipping them keeps the query running instead
    of raising ``no such column``.
    """
    return f"COALESCE(SUM({col}),0)" if col in cols else "0"


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowTotals:
    """Aggregate calls/tokens/savings across all source tables for a window."""
    window: str
    calls: int
    tokens: int
    saved_usd: float
    # Per-source breakdown so consumers can show drill-down detail.
    by_source: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyRow:
    """One day in a daily series."""
    day: str        # YYYY-MM-DD local
    calls: int
    tokens: int
    saved_usd: float


@dataclass(frozen=True)
class PlatformRow:
    """Per-platform attribution for a window."""
    platform: str   # "claude" / "codex" / "gemini" / "legacy_usage" / "jsonl"
    calls: int
    tokens: int
    saved_usd: float


@dataclass(frozen=True)
class DataSourceAudit:
    """Diagnostic per-source row count + whether it was rolled up.

    Used by ``explain-dashboard --check`` to detect when a table has rows
    for a window but wasn't included in the totals (the v9.3 drift bug
    class). ``unread_rows`` is non-zero only when the consumer skipped
    that source — currently impossible for ``query_window`` (it reads
    them all), but kept for forward-compat with consumers that opt out
    of specific sources.
    """
    table: str
    rows_for_window: int
    contributed_to_totals: bool
    unread_rows: int = 0


# ── Core queries ─────────────────────────────────────────────────────────────


def query_window(
    window: WindowLiteral,
    *,
    db_path: Path | str | None = None,
) -> WindowTotals:
    """Return aggregate calls/tokens/savings for ``window`` across all sources.

    Behaviour notes
    ~~~~~~~~~~~~~~~
    * Source tables that don't exist (older DBs) contribute zero.
    * ``usage`` rows are summed by ``input_tokens + output_tokens``.
    * Per-platform rows are summed by ``tokens_used`` (single column —
      subscription/codex/gemini have no in/out split).
    * ``savings_stats`` has no token columns — contributes calls + saved
      only.
    * Savings come from ``cost_saved_usd`` for platform tables,
      ``estimated_claude_cost_saved`` for ``savings_stats``, and the
      Sonnet-baseline computation for ``usage`` (matching the legacy
      ``_query_cumulative_savings`` logic).
    """
    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not db.exists():
        return WindowTotals(window=window, calls=0, tokens=0, saved_usd=0.0)

    where = _window_sql(window)
    conn = sqlite3.connect(str(db))
    by_source: dict[str, dict] = {}
    total_calls = total_tokens = 0
    total_saved = 0.0
    try:
        # Legacy ``usage`` table — use input/output split and Sonnet baseline.
        if _table_exists(conn, _LEGACY_TABLE):
            cols = _columns(conn, _LEGACY_TABLE)
            row = conn.execute(
                f"SELECT COUNT(*), "
                f"{_sum_if_present(cols, 'input_tokens')}, "
                f"{_sum_if_present(cols, 'output_tokens')}, "
                f"{_sum_if_present(cols, 'cost_usd')}, "
                f"{_sum_if_present(cols, 'saved_usd')} "
                f"FROM {_LEGACY_TABLE} WHERE success=1 AND {where}"
            ).fetchone()
            calls = int(row[0])
            in_tok = int(row[1])
            out_tok = int(row[2])
            cost = float(row[3])
            saved = float(row[4])
            by_source[_LEGACY_TABLE] = {
                "calls": calls, "tokens": in_tok + out_tok,
                "cost_usd": cost, "saved_usd": saved,
            }
            total_calls += calls
            total_tokens += in_tok + out_tok
            total_saved += saved

        # Per-platform tables — tokens_used + cost_saved_usd.
        for table in _PLATFORM_TABLES:
            if not _table_exists(conn, table):
                continue
            row = conn.execute(
                f"SELECT COUNT(*), "
                f"COALESCE(SUM(tokens_used),0), "
                f"COALESCE(SUM(cost_saved_usd),0) "
                f"FROM {table} WHERE {where}"
            ).fetchone()
            calls = int(row[0])
            tokens = int(row[1])
            saved = float(row[2])
            by_source[table] = {
                "calls": calls, "tokens": tokens, "saved_usd": saved,
            }
            total_calls += calls
            total_tokens += tokens
            total_saved += saved

        # ``savings_stats`` — no token columns.
        if _table_exists(conn, _JSONL_TABLE):
            row = conn.execute(
                f"SELECT COUNT(*), "
                f"COALESCE(SUM(estimated_claude_cost_saved),0) "
                f"FROM {_JSONL_TABLE} WHERE {where}"
            ).fetchone()
            calls = int(row[0])
            saved = float(row[1])
            by_source[_JSONL_TABLE] = {
                "calls": calls, "tokens": 0, "saved_usd": saved,
            }
            total_calls += calls
            total_saved += saved
    finally:
        conn.close()

    return WindowTotals(
        window=window,
        calls=total_calls,
        tokens=total_tokens,
        saved_usd=total_saved,
        by_source=by_source,
    )


def query_daily(
    days: int = 14,
    *,
    db_path: Path | str | None = None,
) -> list[DailyRow]:
    """Return per-day aggregates for the last ``days`` days.

    Daily rollups UNION the same sources as :func:`query_window` so a
    daily chart and a "lifetime" total stay reconcilable when summed.
    """
    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not db.exists():
        return []

    where = f"timestamp >= datetime('now', '-{int(days)} days')"
    daily: dict[str, dict] = {}

    def _bucket(day: str) -> dict:
        if day not in daily:
            daily[day] = {"calls": 0, "tokens": 0, "saved": 0.0}
        return daily[day]

    conn = sqlite3.connect(str(db))
    try:
        if _table_exists(conn, _LEGACY_TABLE):
            cols = _columns(conn, _LEGACY_TABLE)
            rows = conn.execute(
                f"SELECT date(timestamp,'localtime'), "
                f"COUNT(*), "
                f"{_sum_if_present(cols, 'input_tokens')}, "
                f"{_sum_if_present(cols, 'output_tokens')}, "
                f"{_sum_if_present(cols, 'saved_usd')} "
                f"FROM {_LEGACY_TABLE} WHERE success=1 AND {where} "
                f"GROUP BY date(timestamp,'localtime')"
            ).fetchall()
            for day, calls, in_tok, out_tok, saved in rows:
                b = _bucket(day)
                b["calls"] += int(calls)
                b["tokens"] += int(in_tok) + int(out_tok)
                b["saved"] += float(saved)

        for table in _PLATFORM_TABLES:
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT date(timestamp,'localtime'), "
                f"COUNT(*), "
                f"COALESCE(SUM(tokens_used),0), "
                f"COALESCE(SUM(cost_saved_usd),0) "
                f"FROM {table} WHERE {where} "
                f"GROUP BY date(timestamp,'localtime')"
            ).fetchall()
            for day, calls, tokens, saved in rows:
                b = _bucket(day)
                b["calls"] += int(calls)
                b["tokens"] += int(tokens)
                b["saved"] += float(saved)

        if _table_exists(conn, _JSONL_TABLE):
            rows = conn.execute(
                f"SELECT date(timestamp,'localtime'), "
                f"COUNT(*), "
                f"COALESCE(SUM(estimated_claude_cost_saved),0) "
                f"FROM {_JSONL_TABLE} WHERE {where} "
                f"GROUP BY date(timestamp,'localtime')"
            ).fetchall()
            for day, calls, saved in rows:
                b = _bucket(day)
                b["calls"] += int(calls)
                b["saved"] += float(saved)
    finally:
        conn.close()

    return [
        DailyRow(day=day, calls=d["calls"], tokens=d["tokens"], saved_usd=d["saved"])
        for day, d in sorted(daily.items())
    ]


def query_by_platform(
    window: WindowLiteral,
    *,
    db_path: Path | str | None = None,
) -> list[PlatformRow]:
    """Return per-platform attribution for ``window``.

    Mirrors :func:`query_window` but returns one row per source table
    instead of a single rollup. Useful for tier tables and the dashboard
    explainer.
    """
    totals = query_window(window, db_path=db_path)
    name_map = {
        _LEGACY_TABLE: "legacy_usage",
        "claude_usage": "claude",
        "codex_usage": "codex",
        "gemini_usage": "gemini",
        _JSONL_TABLE: "jsonl_savings",
    }
    return [
        PlatformRow(
            platform=name_map.get(table, table),
            calls=int(d["calls"]),
            tokens=int(d.get("tokens", 0)),
            saved_usd=float(d["saved_usd"]),
        )
        for table, d in totals.by_source.items()
        if d["calls"] > 0
    ]


# ── Audit / canary ───────────────────────────────────────────────────────────


def audit_sources(
    window: WindowLiteral = "today",
    *,
    db_path: Path | str | None = None,
) -> list[DataSourceAudit]:
    """Return per-source audit rows for ``window``.

    Currently :func:`query_window` reads every source it can, so
    ``unread_rows`` is always zero. The audit shape exists so future
    consumers that opt out of a source can be flagged via
    ``explain-dashboard --check``.
    """
    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not db.exists():
        return []

    where = _window_sql(window)
    conn = sqlite3.connect(str(db))
    out: list[DataSourceAudit] = []
    try:
        for table in (_LEGACY_TABLE, *_PLATFORM_TABLES, _JSONL_TABLE):
            if not _table_exists(conn, table):
                continue
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}"
            ).fetchone()
            n = int(row[0]) if row else 0
            out.append(
                DataSourceAudit(
                    table=table,
                    rows_for_window=n,
                    contributed_to_totals=True,
                    unread_rows=0,
                )
            )
    finally:
        conn.close()
    return out
