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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from llm_router import pricing as _pricing

#: Counterfactual model these savings are computed against. WP-05: projected
#: from the ONE policy in llm_router.pricing rather than restated here, so this
#: surface cannot drift from the ledger writer or the session-end hook.
_BASELINE_MODEL = _pricing.savings_baseline_model()

WindowLiteral = Literal["today", "week", "month", "lifetime", "14d"]

DEFAULT_DB_PATH = Path.home() / ".llm-router" / "usage.db"

# Tables this module knows how to UNION. Order is presentation order
# (legacy first → newer platforms last) so audit output reads naturally.
_PLATFORM_TABLES = ("claude_usage", "codex_usage", "gemini_usage")
_LEGACY_TABLE = "usage"
_JSONL_TABLE = "savings_stats"


def _coverage_fields() -> dict:
    """Observed/unobserved counts for WindowTotals. Never raises.

    A dashboard that cannot render because coverage telemetry is unavailable
    would trade a known blind spot for an outage. Unreadable coverage is
    reported AS unreadable (-> "Unknown"), not as zero traffic.
    """
    try:
        from llm_router import coverage as _coverage

        snap = _coverage.snapshot()
        return {
            "observed_n": snap.observed_n,
            "unobserved_n": snap.unobserved_n,
            "coverage_readable": snap.readable,
        }
    except Exception as exc:  # noqa: BLE001
        # coverage_readable=False already renders "Unknown" downstream, so the
        # user is not misled -- but nothing said WHY, and a permanently unknown
        # coverage figure looks like a missing feature rather than a broken one.
        from llm_router import failopen
        failopen.record("CHZ-FO-DASHBOARD-COVERAGE", exc)
        return {"observed_n": 0, "unobserved_n": 0, "coverage_readable": False}


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

    # Real money leaving the account, as distinct from money this router
    # believes it avoided. There was no such field for a long time: every
    # surface could say what was SAVED and none could say what anything COST,
    # which is why an unlabelled savings figure on the statusline was read as
    # spend — the reader was looking for a number that did not exist.
    #
    # Only sources carrying a genuine cost column contribute. The per-platform
    # tables store `cost_saved_usd`, which is a savings field; counting it here
    # would double the confusion rather than resolve it. `uncosted_sources`
    # names the tables that contributed calls but no cost, so "0.00" is never
    # mistaken for "free" when it means "not measured" — the same distinction
    # the quota placeholder draws.
    cost_usd: float = 0.0
    uncosted_sources: tuple[str, ...] = ()

    # WP-07 / I-1: `calls` counts traffic LLM Router OBSERVED. Without a count of
    # what it missed, every rate derived from `calls` silently redefines its own
    # denominator -- "100% of the calls we saw" is not "100% of the calls", and
    # the gap is invisible exactly when routing is broken. These carry the
    # denominator alongside the numerator so no consumer has to assume one.
    #
    # Rolling, not windowed: the coverage store is a capped append-only log with
    # no per-window partition, so this describes recent routing health rather
    # than this window specifically. Labelled that way wherever it is rendered --
    # quietly presenting it as window-scoped would be its own false precision.
    observed_n: int = 0
    unobserved_n: int = 0
    coverage_readable: bool = True

    @property
    def coverage_pct(self) -> float | None:
        """Observed share of routed traffic, or ``None`` when unknowable."""
        total = self.observed_n + self.unobserved_n
        if not self.coverage_readable or total == 0:
            return None
        return 100.0 * self.observed_n / total

    @property
    def coverage_is_degraded(self) -> bool:
        pct = self.coverage_pct
        return pct is not None and pct < 90.0

    def render_coverage(self) -> str:
        """``Unknown`` when the denominator is -- never a fabricated number."""
        pct = self.coverage_pct
        return "Unknown" if pct is None else f"{pct:.1f}%"


@dataclass(frozen=True)
class DailyRow:
    """One day in a daily series."""
    day: str        # YYYY-MM-DD local
    calls: int
    tokens: int
    saved_usd: float
    tokens_saved: int = 0   # tokens handled by cheap providers (not burned on premium)


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


@dataclass(frozen=True)
class RealizedSavingsTotals:
    """Adoption-gated realized-savings split for a window.

    Deliberately NOT the same number as ``WindowTotals.saved_usd``, and
    deliberately never reconciled against it. That figure is a per-surface
    estimate (a Sonnet-baseline calculation for the legacy ``usage`` table,
    ``cost_saved_usd`` for the per-platform tables). This one reads
    ``execution_ledger``'s accounting, where a route's *potential* saving only
    becomes *realized* when its ``realization_status`` is ``verified_used``
    AND its ``adoption_method`` is in ``COUNTS_AS_REALIZED``.

    Routes that were verified as overridden (the host went its own way) or
    never verified at all contribute to ``potential_savings_usd`` only. Keeping
    both numbers on the same object is the point: the gap between them is the
    adoption gap, and collapsing them into one figure would hide exactly the
    thing worth knowing.

    This is the third savings number in the codebase, so INV-COST-004 applies
    with force: this is a SURFACE, not an aggregation. It delegates every
    figure to ``execution_ledger.get_period_accounting`` and computes nothing
    itself. Three hand-rolled savings queries once reported $73.97, $102.31 and
    $205.19 for the same day; a fourth independent implementation is how that
    happens again.
    """

    window: str
    potential_savings_usd: float
    realized_savings_usd: float
    net_realized_savings_usd: float
    realized_routes: int
    overridden_routes: int
    realization_unknown_routes: int
    likely_used_routes: int
    cost_unknown_attempts: int


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
        return WindowTotals(
            window=window, calls=0, tokens=0, saved_usd=0.0, **_coverage_fields()
        )

    where = _window_sql(window)
    conn = sqlite3.connect(str(db))
    by_source: dict[str, dict] = {}
    total_calls = total_tokens = 0
    total_saved = 0.0
    total_cost = 0.0
    uncosted: list[str] = []
    try:
        # Legacy ``usage`` table — recalculate savings from in/out at Opus rates.
        # Opus: $15/M input, $75/M output.  Stored saved_usd used a blended
        # $0.045/1K estimate that is inaccurate for input-heavy calls.
        # Subscription provider rows have no meaningful token cost so exclude them.
        # RED8-01: these were hardcoded at $15/$75 — the retired Opus 3 rate,
        # 3x the real one — and this read path feeds ~26 reporting surfaces, so
        # every savings figure downstream was overstated by the same factor.
        # Sourced from llm_router.pricing now; a literal here fails scripts/lint_pricing.py.
        _OPUS_IN_PER_M = _pricing.input_rate(_BASELINE_MODEL)
        _OPUS_OUT_PER_M = _pricing.output_rate(_BASELINE_MODEL)
        if _table_exists(conn, _LEGACY_TABLE):
            cols = _columns(conn, _LEGACY_TABLE)
            row = conn.execute(  # nosec B608 — table/where are module constants & validated enum, not user input
                f"SELECT COUNT(*), "
                f"{_sum_if_present(cols, 'input_tokens')}, "
                f"{_sum_if_present(cols, 'output_tokens')}, "
                f"{_sum_if_present(cols, 'cost_usd')} "
                f"FROM {_LEGACY_TABLE} "
                f"WHERE success=1 AND (provider IS NULL OR provider != 'subscription') AND {where}"
            ).fetchone()
            calls = int(row[0])
            in_tok = int(row[1])
            out_tok = int(row[2])
            cost = float(row[3])
            opus_baseline = (in_tok * _OPUS_IN_PER_M + out_tok * _OPUS_OUT_PER_M) / 1_000_000
            # AUD-06: signed. Clamping here made the aggregate a sum of wins.
            saved = opus_baseline - cost
            by_source[_LEGACY_TABLE] = {
                "calls": calls, "tokens": in_tok + out_tok,
                "cost_usd": cost, "saved_usd": saved,
            }
            total_calls += calls
            total_tokens += in_tok + out_tok
            total_saved += saved
            total_cost += cost  # the one table with a genuine cost column

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
            # `cost_saved_usd` is a SAVINGS column. This table records no spend,
            # so it contributes none — and says so rather than implying $0.00.
            if calls:
                uncosted.append(table)

        # ``savings_stats`` — DIRECT-routed (free-provider) calls. Token columns
        # added in v7.4; older DBs lack them, so sum defensively.
        if _table_exists(conn, _JSONL_TABLE):
            cols = _columns(conn, _JSONL_TABLE)
            row = conn.execute(  # nosec B608 — table/where are module constants & validated enum, not user input
                f"SELECT COUNT(*), "
                f"COALESCE(SUM(estimated_claude_cost_saved),0), "
                f"{_sum_if_present(cols, 'input_tokens')}, "
                f"{_sum_if_present(cols, 'output_tokens')} "
                f"FROM {_JSONL_TABLE} WHERE {where}"
            ).fetchone()
            calls = int(row[0])
            saved = float(row[1])
            tokens = int(row[2]) + int(row[3])
            by_source[_JSONL_TABLE] = {
                "calls": calls, "tokens": tokens, "saved_usd": saved,
            }
            total_calls += calls
            total_tokens += tokens
            total_saved += saved
    finally:
        conn.close()

    return WindowTotals(
        window=window,
        calls=total_calls,
        tokens=total_tokens,
        saved_usd=total_saved,
        cost_usd=total_cost,
        uncosted_sources=tuple(uncosted),
        by_source=by_source,
        **_coverage_fields(),
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

    _CHEAP_PROVIDERS = frozenset({"ollama", "codex", "gemini_cli", "subscription", "gemini"})

    def _bucket(day: str) -> dict:
        if day not in daily:
            daily[day] = {"calls": 0, "tokens": 0, "saved": 0.0, "tokens_saved": 0}
        return daily[day]

    conn = sqlite3.connect(str(db))
    try:
        # RED8-01: these were hardcoded at $15/$75 — the retired Opus 3 rate,
        # 3x the real one — and this read path feeds ~26 reporting surfaces, so
        # every savings figure downstream was overstated by the same factor.
        # Sourced from llm_router.pricing now; a literal here fails scripts/lint_pricing.py.
        _OPUS_IN_PER_M = _pricing.input_rate(_BASELINE_MODEL)
        _OPUS_OUT_PER_M = _pricing.output_rate(_BASELINE_MODEL)
        if _table_exists(conn, _LEGACY_TABLE):
            cols = _columns(conn, _LEGACY_TABLE)
            rows = conn.execute(
                f"SELECT date(timestamp,'localtime'), "
                f"COUNT(*), "
                f"{_sum_if_present(cols, 'input_tokens')}, "
                f"{_sum_if_present(cols, 'output_tokens')}, "
                f"{_sum_if_present(cols, 'cost_usd')} "
                f"FROM {_LEGACY_TABLE} "
                f"WHERE success=1 "
                f"AND (provider IS NULL OR provider != 'subscription') "
                f"AND {where} "
                f"GROUP BY date(timestamp,'localtime')"
            ).fetchall()
            for day, calls, in_tok, out_tok, cost in rows:
                b = _bucket(day)
                b["calls"] += int(calls)
                b["tokens"] += int(in_tok) + int(out_tok)
                opus_baseline = (int(in_tok) * _OPUS_IN_PER_M + int(out_tok) * _OPUS_OUT_PER_M) / 1_000_000
                # AUD-06: `+=` on a clamped term is the exact defect — a loss
                # on one item could never offset a gain on another.
                b["saved"] += opus_baseline - float(cost)

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

        # Tokens routed to cheap providers per day (not burned on premium quota).
        if _table_exists(conn, _LEGACY_TABLE):
            cols = _columns(conn, _LEGACY_TABLE)
            if "provider" in cols and "input_tokens" in cols:
                cheap_placeholders = ",".join("?" * len(_CHEAP_PROVIDERS))
                rows = conn.execute(  # nosec B608 — table/where are module constants & validated enum; IN uses ? placeholders
                    f"SELECT date(timestamp,'localtime'), "
                    f"COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0) "
                    f"FROM {_LEGACY_TABLE} "
                    f"WHERE success=1 AND {where} AND provider IN ({cheap_placeholders}) "
                    f"GROUP BY date(timestamp,'localtime')",
                    list(_CHEAP_PROVIDERS),
                ).fetchall()
                for day, tok_saved in rows:
                    _bucket(day)["tokens_saved"] += int(tok_saved)
    finally:
        conn.close()

    return [
        DailyRow(
            day=day,
            calls=d["calls"],
            tokens=d["tokens"],
            saved_usd=d["saved"],
            tokens_saved=d["tokens_saved"],
        )
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


def query_model_distribution(
    days: int = 14,
    *,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Return model usage counts for the last ``days`` days.

    Aggregates model calls across all source tables (usage, claude_usage, etc.)
    for dashboard visualization of which models are being used most.

    Returns a dict mapping model names to call counts.
    """
    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not db.exists():
        return {}

    where = f"timestamp >= datetime('now', '-{int(days)} days')"
    model_counts: dict[str, int] = {}

    conn = sqlite3.connect(str(db))
    try:
        # Legacy usage table — keyed by 'model'
        if _table_exists(conn, _LEGACY_TABLE):
            rows = conn.execute(
                f"SELECT model, COUNT(*) FROM {_LEGACY_TABLE} "
                f"WHERE success=1 AND {where} GROUP BY model"
            ).fetchall()
            for model, count in rows:
                model_counts[model] = model_counts.get(model, 0) + int(count)

        # Platform tables — keyed by 'model'
        for table in _PLATFORM_TABLES:
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT model, COUNT(*) FROM {table} "
                f"WHERE {where} GROUP BY model"
            ).fetchall()
            for model, count in rows:
                model_counts[model] = model_counts.get(model, 0) + int(count)
    finally:
        conn.close()

    return model_counts


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


# ── Realized savings (adoption-gated) ────────────────────────────────────────


def _window_epoch_bounds(window: WindowLiteral) -> tuple[float, float]:
    """Epoch-second ``(start_ts, end_ts)`` bounds for ``window``.

    ``_window_sql`` above emits a SQL WHERE fragment that SQLite evaluates
    against the ``timestamp`` columns of the usage tables.
    ``execution_ledger.get_period_accounting`` instead takes Python float
    Unix-epoch bounds, so this maps window names into that second shape.

    The two are NOT guaranteed to select an identical row set at day
    boundaries, and that is deliberate rather than an oversight: ``"today"``
    uses local time here because ``_window_sql`` uses an explicit
    ``'localtime'`` modifier for the same window, while the other windows use
    UTC arithmetic because ``_window_sql`` uses a bare ``datetime('now')``,
    which SQLite evaluates as UTC. Making both windows agree on one timezone
    would make this helper self-consistent and put it out of step with the SQL
    it is meant to parallel.
    """
    now_utc = datetime.now(timezone.utc)
    if window == "today":
        local_now = datetime.now().astimezone()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.timestamp(), local_now.timestamp()
    if window == "week":
        return (now_utc - timedelta(days=7)).timestamp(), now_utc.timestamp()
    if window == "month":
        start_of_month = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start_of_month.timestamp(), now_utc.timestamp()
    if window == "14d":
        return (now_utc - timedelta(days=14)).timestamp(), now_utc.timestamp()
    if window == "lifetime":
        return 0.0, now_utc.timestamp()
    raise ValueError(f"unknown window: {window!r}")


def query_realized_savings(
    window: WindowLiteral,
    *,
    db_path: Path | str | None = None,
) -> RealizedSavingsTotals:
    """The adoption-gated realized-savings split for ``window``.

    A SURFACE over ``execution_ledger.get_period_accounting`` (INV-COST-004):
    every figure below is copied from the accounting object, none is computed
    here. See ``RealizedSavingsTotals`` for why this must never become a
    fourth independent savings calculation.

    Fails open — a missing database, a missing ``execution_events`` table, or
    any error inside the accounting yields all zeros rather than raising,
    matching this module's existing ``if not db.exists(): return <empty>``
    convention. A dashboard panel must not be able to take the dashboard down.

    One subtlety worth keeping: the existence check runs against a plain
    ``sqlite3`` connection FIRST, and returns before ``execution_ledger`` is
    imported. Calling into the ledger directly would CREATE the database and
    the table as a side effect of connecting — so reading a figure would
    materialise the thing it was reading. Reading must not have side effects.
    """
    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    empty = RealizedSavingsTotals(
        window=window,
        potential_savings_usd=0.0,
        realized_savings_usd=0.0,
        net_realized_savings_usd=0.0,
        realized_routes=0,
        overridden_routes=0,
        realization_unknown_routes=0,
        likely_used_routes=0,
        cost_unknown_attempts=0,
    )
    if not db.exists():
        return empty

    try:
        conn = sqlite3.connect(str(db))
        try:
            if not _table_exists(conn, "execution_events"):
                return empty
        finally:
            conn.close()

        from llm_router.execution_ledger import get_period_accounting

        start_ts, end_ts = _window_epoch_bounds(window)
        accounting = get_period_accounting(start_ts, end_ts, path=db)
    except Exception as exc:  # noqa: BLE001 - a savings panel must never break the dashboard
        # CHZ-FO-02: a fail-open path that returns live-looking data must say
        # so. Zeros here are indistinguishable from "genuinely no realized
        # savings this window", which is the more dangerous of the two —
        # silently reporting $0 realized reads as an adoption problem rather
        # than a broken read.
        from llm_router import failopen

        failopen.record("CHZ-FO-DASHBOARD-REALIZED", exc)
        return empty

    return RealizedSavingsTotals(
        window=window,
        potential_savings_usd=accounting.potential_savings_usd,
        realized_savings_usd=accounting.realized_savings_usd,
        net_realized_savings_usd=accounting.net_realized_savings_usd,
        realized_routes=accounting.realized_routes,
        overridden_routes=accounting.overridden_routes,
        realization_unknown_routes=accounting.realization_unknown_routes,
        likely_used_routes=accounting.likely_used_routes,
        cost_unknown_attempts=accounting.cost_unknown_attempts,
    )


# ── The money line ────────────────────────────────────────────────────────────
#
# Every surface that prints money renders it through here. INV-COST-004 already
# said the aggregation functions are the only cost totals and surfaces delegate;
# it said so in a comment, and the statusline spent a year violating its spirit
# by resolving no interpreter at all and printing nothing. A shared renderer
# makes the rule mechanical instead of aspirational.

#: Below this, external spend is noise on a subscription seat and the segment is
#: omitted entirely. A user on a Max plan is anxious about quota, not about a
#: third of a cent, and a $0.00 segment spends pixels to say nothing.
SPEND_FLOOR_USD = 0.01

#: Coverage below this makes the savings estimate soft enough that the figure
#: has to admit it. `coverage_is_degraded` uses 90% for dashboards; the
#: statusline has one line and only flags genuinely poor coverage.
COVERAGE_CALLOUT_PCT = 60.0


def session_spend_usd(state_dir: Path | None = None) -> float | None:
    """Measured external spend for the CURRENT session, or None if unknown.

    ``session_spend.json`` was written by the routing path and read by no
    surface at all -- the only genuinely measured money figure on disk, and it
    reached no user. Returning None rather than 0.0 keeps "no session yet"
    distinct from "this session spent nothing".
    """
    base = state_dir or (Path.home() / ".llm-router")
    try:
        import json

        data = json.loads((base / "session_spend.json").read_text())
    except (OSError, ValueError):
        return None
    total = data.get("total_usd")
    return float(total) if isinstance(total, (int, float)) else None


def render_money(
    totals: "WindowTotals",
    session_usd: float | None = None,
    scope: str = "today",
    show_spend: bool = True,
    show_coverage: bool = True,
) -> str:
    """The one-line money summary, in the only format any surface should use.

    Two quantities that are NOT the same kind of number, formatted so they
    cannot be confused:

      * spend is MEASURED -- exact, to the cent, and shown only when it clears
        SPEND_FLOOR_USD;
      * savings are MODELLED against a counterfactual baseline -- prefixed with
        a tilde and rounded to the dollar, because precision is itself a claim
        and 255 of 382 recent decisions were never observed.

    Both carry a verb. A bare dollar amount beside a quota percentage is read as
    money spent, which is how this whole line was misread in the first place.

    `show_spend=False` drops the spend clause entirely, for a surface with no
    room for it. It has to be its own flag: passing session_usd=None does not
    suppress spend, it falls back to totals.cost_usd, so the only way to hide it
    without this was to pass a number below the floor and hope.

    `show_coverage=False` drops the "(NN% observed)" note. The tilde still marks
    the figure as modelled, which is the part that stops it being read as billed
    spend; the percentage quantifies how modelled, which an ambient one-line
    surface has no room to act on. Note also that coverage is ROLLING, not
    window-scoped -- see WindowTotals.observed_n -- so inside a phrase ending
    "saved today" it invites the reading that the percentage is today's. A
    caller that keeps it should be somewhere that has room to say otherwise.
    """
    parts: list[str] = []

    spend = session_usd if session_usd is not None else totals.cost_usd
    if show_spend and spend is not None and spend >= SPEND_FLOOR_USD:
        parts.append(f"${spend:,.2f} spent")

    saved = totals.saved_usd
    if saved and saved >= 0.01:
        # Rounding scales with magnitude. Whole dollars suit $34, where the
        # cents are noise against a counterfactual baseline -- but they overstate
        # $0.70 as "~$1", a 43% exaggeration in the direction that flatters the
        # product. Below $10 the cents are the number.
        amount = f"{saved:,.2f}" if saved < 10 else f"{saved:,.0f}"
        # Scope belongs INSIDE the phrase. Appended by the caller it landed
        # after the coverage note -- "~$34 saved (33% observed) today" -- which
        # reads as though the coverage, not the saving, was today's.
        chunk = f"~${amount} saved{' ' + scope if scope else ''}"
        pct = totals.coverage_pct
        if show_coverage and pct is not None and pct < COVERAGE_CALLOUT_PCT:
            # Say how much of the estimate is actually observed rather than
            # asserting a soft number with a hard face.
            chunk += f" ({pct:.0f}% observed)"
        parts.append(chunk)

    return " · ".join(parts)
