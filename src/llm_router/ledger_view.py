"""One definition per ledger metric (PLAN v16 P3.4-A, D-194).

Evidence: the P3.4 survey of 2026-10-17 found one question ("what did the router do today?")
answered by up to twenty code paths with different windows (UTC day / local day / rolling 6 h /
24 h / 7 d / since-Monday), different provenance filters (``production_only`` / ``is_simulated=0`` /
``provenance='runtime'`` / none), different row exclusions, different sources (``usage``,
``routing_decisions``, ``lineage.db`` capped at 5000 rows, ``proxy_calls.jsonl`` only in kpi) and
two session filters. Surfaces that print the same words disagreed on the same rows.

This module is the only place a surface module reads the ``usage`` and ``routing_decisions``
tables (``tests/test_p34a_ledger_view_lint.py`` greps for it). It is stdlib-only (``sqlite3``,
``json``, ``datetime``) and imports no pydantic, so a hook may import it later (part B).

NAMED WINDOWS (``WINDOWS``), all bounds computed once in LOCAL time and compared as UTC instants:

=========  ====================================================================================
session    rows of one session id; with none, since ``session_start.txt`` (label says so)
today      since local midnight
6h         rolling 6 hours (declared, no longer the statusline's private window)
24h        rolling 24 hours
7d         rolling 7 days (what the dashboard called ``week``)
14d        rolling 14 days
month      since local midnight of the 1st
lifetime   no lower bound
=========  ====================================================================================

Every window is closed at both ends: ``since <= timestamp <= as_of`` (as_of = the call's ``now``).

EXECUTED BY PROVIDER counts, per window, exactly these rows once each:

* ``routing_decisions`` rows with ``provenance = 'runtime'`` (when the column exists) that are a
  served call (``provider_classes.real_decision_sql``: not error / cache_hit / breaker_open /
  refused). ``final_provider`` is the label.
* ``proxy_calls.jsonl`` rows (one per ``POST /v1/messages``): ``decision = served`` is a local
  backend answer (label ``ollama``); ``forwarded`` / ``fallback`` is a Claude call (label
  ``anthropic``). A refused ``local:`` prefix (``prefix_outcome`` ``refused:*``) and an upstream
  HTTP error (>= 400) are not served calls and are left out, like an error row.
* DEDUPE KEY: ``trace_id`` (the call's own uuid4 hex, ``call_identity``). A proxy row whose
  ``trace_id`` is already a ``routing_decisions`` row in the window is counted once, as the
  ``routing_decisions`` row. A proxy row without a ``trace_id`` (written before P1.10) cannot be
  proven a duplicate and is counted; that is the documented over-count risk, bounded by the
  pre-P1.10 rows in the window.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from llm_router import paths
from llm_router.provider_classes import not_error_row_sql, real_decision_sql

#: ``types.LOCAL_PROVIDERS`` copy (that module is not stdlib-only); a test pins equality.
LOCAL_PROVIDERS = frozenset({"ollama", "lm_studio", "vllm", "llamacpp"})
#: ``cost._FREE_PROVIDERS`` minus local: included in a subscription, no per-token bill.
SUBSCRIPTION_PROVIDERS = frozenset({"codex", "gemini_cli", "subscription"})
PROXY_LOCAL_LABEL = "ollama"
PROXY_CLAUDE_LABEL = "anthropic"

#: The one window table; :func:`resolve` holds the arithmetic.
WINDOWS = ("session", "today", "6h", "24h", "7d", "14d", "month", "lifetime")
_ROLLING_HOURS = {"6h": 6.0, "24h": 24.0, "7d": 168.0, "14d": 336.0}
#: Spellings older callers use.
ALIASES = {"day": "today", "week": "7d", "all": "lifetime", "all_time": "lifetime"}

_FMT = "%Y-%m-%d %H:%M:%S"


def _utc_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(_FMT)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Bounds:
    """A resolved window. ``since_ts`` None = unbounded below. ``session_id`` narrows rows to
    one session (``scope`` ``session_id``); ``scope`` ``timestamp`` marks the fallback."""

    name: str
    label: str
    since_ts: float | None
    until_ts: float
    scope: str = "all"          # all | session_id | timestamp
    session_id: str | None = None

    @property
    def since_sql(self) -> str | None:
        return None if self.since_ts is None else _utc_str(self.since_ts)

    @property
    def until_sql(self) -> str:
        return _utc_str(self.until_ts)

    def sql(self, cols: set[str], column: str = "timestamp") -> tuple[str, list]:
        """``(predicate, params)`` over ``column`` (UTC text, ``T`` or space separated:
        ``datetime()`` normalises both) plus the session predicate when the table carries it."""
        parts = [f"datetime({column}) <= ?"]
        params: list = [self.until_sql]
        if self.since_ts is not None:
            parts.append(f"datetime({column}) >= ?")
            params.append(self.since_sql)
        if self.session_id is not None and "session_id" in cols:
            parts.append("session_id = ?")
            params.append(self.session_id)
        return " AND ".join(parts), params

    def covers(self, ts: float) -> bool:
        return (self.since_ts is None or ts >= self.since_ts) and ts <= self.until_ts


def _now_ts(now) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def read_session_start(state_dir: Path | None = None) -> float | None:
    """Epoch in ``session_start.txt`` (what the SessionStart hook wrote), or None."""
    try:
        return float((state_dir or paths.llm_router_home()).joinpath("session_start.txt")
                     .read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_session_id(state_dir: Path | None = None) -> str | None:
    try:
        sid = (state_dir or paths.llm_router_home()).joinpath("session_id.txt") \
            .read_text(encoding="utf-8").strip()
        return sid or None
    except OSError:
        return None


def resolve(window: str, *, now=None, session_start: float | None = None,
            session_id: str | None = None) -> Bounds:
    """Bounds of a named window. ``session`` prefers ``session_id`` (every row of that id, any
    time); with none it falls back to ``timestamp >= session_start`` and the label says so."""
    window = ALIASES.get(window, window)
    if window not in WINDOWS:
        raise ValueError(f"unknown window: {window!r}")
    until = _now_ts(now)
    if window == "session":
        return session_scope(session_id=session_id, session_start=session_start, now=until)
    if window == "lifetime":
        return Bounds(window, "lifetime", None, until)
    if window in _ROLLING_HOURS:
        return Bounds(window, window, until - _ROLLING_HOURS[window] * 3600.0, until)
    local = datetime.fromtimestamp(until)          # naive local wall clock (honours TZ)
    if window == "today":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return Bounds(window, window, start.timestamp(), until)


def rolling(hours: float, *, now=None) -> Bounds:
    """A caller-chosen rolling window (``llm-router summary --since 6``). A size the table
    holds keeps its table name, so its label matches the named window."""
    until = _now_ts(now)
    for name, h in _ROLLING_HOURS.items():
        if abs(h - hours) < 1e-9:
            return Bounds(name, name, until - h * 3600.0, until)
    return Bounds(f"{hours:g}h", f"{hours:g}h", until - hours * 3600.0, until)


def session_scope(*, session_id: str | None = None, session_start: float | None = None,
                  now=None) -> Bounds:
    """The session window. With a ``session_id``: that id's rows, any time (hooks and
    ``llm()`` stamp it on ``usage``, ``routing_decisions`` and proxy rows). Without one: rows
    since ``session_start`` (a file's epoch), labelled ``session (by timestamp: no session_id)``
    so a reader can tell the weaker filter. With neither: the last hour, labelled likewise."""
    until = _now_ts(now)
    if session_id:
        return Bounds("session", f"session {session_id[:8]}", None, until, "session_id", session_id)
    if session_start is None:
        session_start = until - 3600.0
    return Bounds("session", "session (by timestamp: no session_id)", session_start, until,
                  "timestamp")


# ── connections ───────────────────────────────────────────────────────────────

def _db_path(db_path) -> Path:
    if db_path is not None:
        return Path(db_path)
    override = os.environ.get("LLM_ROUTER_DB_PATH", "").strip()
    return Path(override) if override else paths.state_path("usage.db")


def _connect(db_path, timeout_s: float = 1.0):
    """Read-only connection or None. Never creates the database."""
    p = _db_path(db_path)
    if not p.is_file():
        return None
    try:
        return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_s)
    except sqlite3.Error:
        return None


def _columns(con, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


# ── executed by provider ──────────────────────────────────────────────────────

def _proxy_ledger_path(proxy_path) -> Path:
    if proxy_path is not None:
        return Path(proxy_path)
    return paths.state_path("proxy_calls.jsonl")


def _proxy_label(row: dict) -> str | None:
    """Provider label of a proxy ledger row that is an executed call, else None."""
    if str(row.get("prefix_outcome") or "").startswith("refused"):
        return None
    status = row.get("upstream_status")
    if isinstance(status, (int, float)) and not isinstance(status, bool) and status >= 400:
        return None
    decision = row.get("decision")
    if decision == "served":
        return PROXY_LOCAL_LABEL
    if decision in ("forwarded", "fallback"):
        return PROXY_CLAUDE_LABEL
    return None


def _proxy_rows(bounds: Bounds, proxy_path) -> list[dict]:
    try:
        text = _proxy_ledger_path(proxy_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ts = row.get("ts")
        # A row without a usable timestamp cannot be placed in a window: it is out.
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not bounds.covers(ts):
            continue
        if bounds.session_id is not None and row.get("session_id") != bounds.session_id:
            continue
        out.append(row)
    return out


def executed_by_provider(bounds: Bounds, *, db_path=None, proxy_path=None,
                         timeout_s: float = 0.25) -> dict[str, int] | None:
    """``{provider: executed calls}`` in the window (see the module docstring for the rows and
    the dedupe key). ``None`` only when neither source exists, so "no data" is not "0 calls"."""
    counts: dict[str, int] = {}
    seen_trace: set[str] = set()
    have_data = False
    con = _connect(db_path, timeout_s)
    if con is not None:
        try:
            cols = _columns(con, "routing_decisions")
            if cols:
                have_data = True
                where, params = bounds.sql(cols)
                prov = "AND provenance = 'runtime' " if "provenance" in cols else ""
                trace = ", trace_id" if "trace_id" in cols else ", NULL"
                for provider, tid in con.execute(
                    f"SELECT COALESCE(NULLIF(final_provider, ''), 'unknown'){trace} "
                    f"FROM routing_decisions WHERE {where} {prov}"
                    f"AND ({real_decision_sql(con)})", params):
                    counts[provider] = counts.get(provider, 0) + 1
                    if tid:
                        seen_trace.add(tid)
        except sqlite3.Error:
            return None
        finally:
            con.close()
    if _proxy_ledger_path(proxy_path).is_file():
        have_data = True
        for row in _proxy_rows(bounds, proxy_path):
            label = _proxy_label(row)
            if label is None:
                continue
            tid = row.get("trace_id")
            if tid and tid in seen_trace:
                continue                      # already counted as a routing_decisions row
            counts[label] = counts.get(label, 0) + 1
    return counts if have_data else None


#: ``qa_policy.QA_STRIP_PROVIDERS`` copy (that module is not stdlib-only); a test pins equality.
LOCAL_BY_CAUSE_PROVIDERS = LOCAL_PROVIDERS | {"openai_compat"}


def local_by_cause(bounds: Bounds, *, db_path=None, timeout_s: float = 0.25) -> dict[str, int] | None:
    """F23: the served-local count split by why local served (``floor`` (F26, wins over the
    others) / ``bench`` / ``last_resort`` / ``other_skip`` / ``policy`` / ``unknown``; one cause per row, first match wins in that order).
    Same window, provenance and real-decision filter as :func:`executed_by_provider`'s
    ``routing_decisions`` half (proxy rows carry no skip list). ``None`` without a database."""
    from llm_router import skip_causes as sc

    con = _connect(db_path, timeout_s)
    if con is None:
        return None
    out = {c: 0 for c in sc.CAUSES}
    try:
        cols = _columns(con, "routing_decisions")
        if not cols:
            return out
        where, params = bounds.sql(cols)
        prov = "AND provenance = 'runtime' " if "provenance" in cols else ""
        local = sorted(LOCAL_BY_CAUSE_PROVIDERS)
        marks = ",".join("?" for _ in local)
        if "skipped_json" in cols and "served_by_last_resort" in cols:
            case = ("CASE WHEN skipped_json IS NULL THEN 'unknown' "
                    "WHEN skipped_json LIKE '%\"reason\":\"floor\"%' THEN 'floor' "
                    "WHEN skipped_json LIKE '%\"reason\":\"bench\"%' THEN 'bench' "
                    "WHEN served_by_last_resort = 1 THEN 'last_resort' "
                    "WHEN skipped_json != '[]' THEN 'other_skip' ELSE 'policy' END")
        else:
            case = "'unknown'"
        for cause, n in con.execute(
            f"SELECT {case}, COUNT(*) FROM routing_decisions WHERE {where} {prov}"
            f"AND ({real_decision_sql(con)}) AND COALESCE(final_provider, '') IN ({marks}) "
            "GROUP BY 1", [*params, *local]):
            out[cause] = int(n)
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return out


def provider_class(provider: str) -> str:
    """``local`` | ``subscription`` | ``paid``. Everything that is not provably local or
    subscription is paid: an unknown provider is never silently called free."""
    if provider in LOCAL_PROVIDERS:
        return "local"
    if provider in SUBSCRIPTION_PROVIDERS:
        return "subscription"
    return "paid"


def local_paid_mix(executed: dict[str, int] | None) -> dict[str, int]:
    """``{local, subscription, paid}`` from :func:`executed_by_provider`'s counts."""
    mix = {"local": 0, "subscription": 0, "paid": 0}
    for provider, n in (executed or {}).items():
        mix[provider_class(provider)] += int(n)
    return mix


# ── saved dollars ─────────────────────────────────────────────────────────────

_PLATFORM_TABLES = ("claude_usage", "codex_usage", "gemini_usage")
_LEGACY_TABLE = "usage"
_JSONL_TABLE = "savings_stats"


@dataclass
class MoneyWindow:
    calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    verified_usd: float = 0.0
    verified_calls: int = 0
    unverified_usd: float = 0.0
    unverified_calls: int = 0
    by_source: dict = field(default_factory=dict)
    uncosted: list = field(default_factory=list)

    @property
    def saved_usd(self) -> float:
        return self.verified_usd + self.unverified_usd

    @property
    def saved_n(self) -> int:
        return self.verified_calls + self.unverified_calls


def _table_exists(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def _sum_if_present(cols: set[str], col: str) -> str:
    return f"COALESCE(SUM({col}),0)" if col in cols else "0"


def _production_pred(cols: set[str]) -> str:
    """ACC-01: ``savings.production_only`` (fail-closed ``is_simulated = 0``), or constant false
    when the table has no provenance column. The one caller of it for every money table."""
    if "is_simulated" not in cols:
        return "0"
    from llm_router.savings import production_only
    return production_only(prefix="")


def _sum_where(cols: set[str], col: str, pred: str) -> str:
    return f"COALESCE(SUM(CASE WHEN {pred} THEN {col} ELSE 0 END),0)" if col in cols else "0"


def money_window(bounds: Bounds, *, db_path=None, timeout_s: float = 1.0) -> MoneyWindow:
    """Calls / tokens / saved dollars across ``usage``, the platform tables and ``savings_stats``
    (the body ``dashboard_data.query_window`` had), over ``bounds``. A missing database or table
    contributes zero. ``usage`` rows leave out failures, ``subscription`` provider, error rows
    and cache rows (provider ``cache``: 0 tokens, $0, not a call anyone made)."""
    from llm_router import pricing
    from llm_router.savings import VERIFIED_CALLS_SQL, savings_split_sql

    out = MoneyWindow()
    con = _connect(db_path, timeout_s)
    if con is None:
        return out
    try:
        baseline = pricing.savings_baseline_model()
        in_rate, out_rate = pricing.input_rate(baseline), pricing.output_rate(baseline)
        if _table_exists(con, _LEGACY_TABLE):
            cols = _columns(con, _LEGACY_TABLE)
            prod = _production_pred(cols)
            where, params = bounds.sql(cols)
            row = con.execute(  # nosec B608 -- table/where are module constants and bound params
                f"SELECT COUNT(*), {_sum_if_present(cols, 'input_tokens')}, "
                f"{_sum_if_present(cols, 'output_tokens')}, {_sum_if_present(cols, 'cost_usd')}, "
                f"{_sum_where(cols, 'input_tokens', prod)}, {_sum_where(cols, 'output_tokens', prod)}, "
                f"{_sum_where(cols, 'cost_usd', prod)}, "
                f"COALESCE(SUM(CASE WHEN {prod} THEN 1 ELSE 0 END),0) "
                f"FROM {_LEGACY_TABLE} WHERE success=1 "
                f"AND (provider IS NULL OR provider NOT IN ('subscription', 'cache')) "
                f"AND {not_error_row_sql(con)} AND {where}", params).fetchone()
            in_tok, out_tok, cost = int(row[1]), int(row[2]), float(row[3])
            saved = (int(row[4]) * in_rate + int(row[5]) * out_rate) / 1_000_000 - float(row[6])
            out.unverified_usd += saved
            out.unverified_calls += int(row[7])
            out.by_source[_LEGACY_TABLE] = {
                "calls": int(row[0]), "tokens": in_tok + out_tok, "cost_usd": cost,
                "saved_usd": 0.0,
                # the production-provenance rows behind the unverified figure
                "production_calls": int(row[7]), "production_saved_usd": saved}
            out.calls += int(row[0])
            out.tokens += in_tok + out_tok
            out.cost_usd += cost
        for table in _PLATFORM_TABLES:
            if not _table_exists(con, table):
                continue
            cols = _columns(con, table)
            prod = _production_pred(cols)
            where, params = bounds.sql(cols)
            row = con.execute(  # nosec B608
                f"SELECT COUNT(*), COALESCE(SUM(tokens_used),0), "
                f"COALESCE(SUM(CASE WHEN {prod} THEN cost_saved_usd ELSE 0 END),0), "
                f"COALESCE(SUM(CASE WHEN {prod} THEN 1 ELSE 0 END),0) "
                f"FROM {table} WHERE {where}", params).fetchone()
            out.unverified_usd += float(row[2])
            out.unverified_calls += int(row[3])
            out.by_source[table] = {"calls": int(row[0]), "tokens": int(row[1]), "saved_usd": 0.0}
            out.calls += int(row[0])
            out.tokens += int(row[1])
            if int(row[0]):
                out.uncosted.append(table)
        if _table_exists(con, _JSONL_TABLE):
            cols = _columns(con, _JSONL_TABLE)
            verified_sql, unverified_sql, unverified_n_sql = savings_split_sql(cols)
            verified_n_sql = (VERIFIED_CALLS_SQL
                              if {"host", "model_used", "timestamp", "mode"} <= cols else "0")
            prod = _production_pred(cols)
            verified_sql = f"(CASE WHEN {prod} THEN ({verified_sql}) ELSE 0 END)"
            unverified_sql = f"(CASE WHEN {prod} THEN ({unverified_sql}) ELSE 0 END)"
            unverified_n_sql = f"(CASE WHEN {prod} THEN ({unverified_n_sql}) ELSE 0 END)"
            verified_n_sql = f"(CASE WHEN {prod} THEN ({verified_n_sql}) ELSE 0 END)"
            where, params = bounds.sql(cols)
            row = con.execute(  # nosec B608
                f"SELECT COUNT(*), COALESCE(SUM({verified_sql}),0), "
                f"{_sum_if_present(cols, 'input_tokens')}, {_sum_if_present(cols, 'output_tokens')}, "
                f"COALESCE(SUM({unverified_sql}),0), COALESCE(SUM({unverified_n_sql}),0), "
                f"COALESCE(SUM({verified_n_sql}),0) FROM {_JSONL_TABLE} WHERE {where}",
                params).fetchone()
            tokens = int(row[2]) + int(row[3])
            out.unverified_usd += float(row[4])
            out.unverified_calls += int(row[5])
            out.verified_calls += int(row[6])
            out.verified_usd += float(row[1])
            out.by_source[_JSONL_TABLE] = {
                "calls": int(row[0]), "tokens": tokens, "saved_usd": float(row[1]),
                "unverified_saved_usd": float(row[4]), "unverified_calls": int(row[5]),
                "verified_calls": int(row[6])}
            out.calls += int(row[0])
            out.tokens += tokens
    except sqlite3.Error:
        return MoneyWindow()
    finally:
        con.close()
    return out


def saved_usd(bounds: Bounds, *, db_path=None) -> tuple[float, int]:
    """The ONE user-facing saved figure: ``(realized + unverified dollars, n rows behind it)``,
    always an estimate (``LedgerWindow.estimated``). Same figure as ``Summary.estimated_usd``."""
    m = money_window(bounds, db_path=db_path)
    return m.saved_usd, m.saved_n


def usage_period_rows(bounds: Bounds, *, db_path=None,
                      include_simulated: bool = False) -> list[tuple] | None:
    """Served ``usage`` rows ``(provider, input_tokens, output_tokens, cost_usd)`` for
    ``cost.get_savings_by_period``: production provenance, success only, error and cache rows
    out (subscription rows stay: that caller counts them apart). ``include_simulated`` is the
    named test hatch (pytest stamps its own rows synthetic). ``None`` without a database."""
    con = _connect(db_path)
    if con is None:
        return None
    try:
        cols = _columns(con, _LEGACY_TABLE)
        if not cols:
            return []
        where, params = bounds.sql(cols)
        return con.execute(  # nosec B608
            f"SELECT provider, input_tokens, output_tokens, cost_usd FROM {_LEGACY_TABLE} "
            f"WHERE success = 1 AND COALESCE(provider, '') != 'cache' AND {not_error_row_sql(con)} "
            f"AND {'1' if include_simulated else _production_pred(cols)} AND {where}",
            params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()


def ns_strict(days: int = 7, *, include_research: bool = False) -> dict:
    """NS (strict-used share) over the last ``days``: the kpi strict unit stream, not a second
    definition. Delegates to ``commands.kpi._ns_d1_d2`` (northstar units joined to session kind)."""
    from llm_router import session_kind as sk
    from llm_router.commands import kpi
    from llm_router.proxy import ledger as pl

    index = sk.KindIndex(pl.read_rows())
    return kpi._ns_d1_d2(days, kpi._allowed_kinds(include_research), index)[0]


# ── the window summary every surface prints ───────────────────────────────────

@dataclass(frozen=True)
class LedgerWindow:
    as_of: str
    n: int                       # executed calls in the window (sum of ``executed``)
    window: str                  # label
    source: str
    estimated: bool              # the saved figure is an estimate, never realized money
    executed: tuple = ()         # ((provider, n), ...) sorted by -n, provider
    local_n: int = 0
    subscription_n: int = 0
    paid_n: int = 0
    saved_usd: float = 0.0
    saved_n: int = 0
    verified_usd: float = 0.0
    unverified_usd: float = 0.0
    scope: str = "all"
    has_data: bool = True

    def line(self) -> str:
        return format_line(self)


def window_summary(window: "str | Bounds" = "24h", *, now=None, db_path=None, proxy_path=None,
                   session_start: float | None = None,
                   session_id: str | None = None) -> LedgerWindow:
    bounds = window if isinstance(window, Bounds) else resolve(
        window, now=now, session_start=session_start, session_id=session_id)
    counts = executed_by_provider(bounds, db_path=db_path, proxy_path=proxy_path)
    money = money_window(bounds, db_path=db_path)
    mix = local_paid_mix(counts)
    items = tuple(sorted((counts or {}).items(), key=lambda kv: (-kv[1], kv[0])))
    return LedgerWindow(
        as_of=_iso(bounds.until_ts), n=sum(n for _p, n in items), window=bounds.label,
        source="routing_decisions(runtime)+proxy_calls.jsonl; usage+savings_stats (saved)",
        estimated=True, executed=items, local_n=mix["local"], subscription_n=mix["subscription"],
        paid_n=mix["paid"], saved_usd=money.saved_usd, saved_n=money.saved_n,
        verified_usd=money.verified_usd, unverified_usd=money.unverified_usd,
        scope=bounds.scope, has_data=counts is not None)


def format_line(lw: LedgerWindow) -> str:
    """The one printed form. Identity tests compare this string across surfaces."""
    if not lw.has_data:
        return f"ledger {lw.window}: no execution data (usage.db and proxy ledger missing)"
    by = " ".join(f"{p} {n}" for p, n in lw.executed) or "none"
    return (f"ledger {lw.window}: {lw.n} executed ({by}) · mix local {lw.local_n} / "
            f"subscription {lw.subscription_n} / paid {lw.paid_n} · "
            f"est. saved ${lw.saved_usd:,.2f} (n={lw.saved_n:,})")
