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
#: Router provider name -> the attribute of ``seats.Seats`` that would make it a subscription
#: call (D-205: the seat is discovered on this machine; no plan or model name is written here).
SEAT_ALIASES = {"anthropic": "claude", "claude": "claude", "codex": "codex", "gemini_cli": "gemini"}
#: Row evidence words (a proxy row's ``auth`` kind; a ``usage`` row's provider / reason).
AUTH_SUBSCRIPTION, AUTH_PAID = "oauth", "api_key"
PROVIDER_SUBSCRIPTION = "subscription"
REASON_SUBSCRIPTION = "claude_code_subscription"
CLASSES = ("local", "subscription", "paid", "unclassified")
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


def executed_rows(bounds: Bounds, *, db_path=None, proxy_path=None,
                  timeout_s: float = 0.25) -> list[tuple] | None:
    """The executed calls in the window, once each: ``(provider, auth, reason)``. ``auth`` is a
    forwarded proxy row's auth kind (``oauth`` / ``api_key``), ``reason`` a decision row's
    ``reason_code``; both None when the row carries no such evidence. ``None`` only when neither
    source exists, so "no data" is not "0 calls". See the module docstring for the rows and the
    dedupe key."""
    rows: list[tuple] = []
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
                reason = ", reason_code" if "reason_code" in cols else ", NULL"
                for provider, tid, why in con.execute(
                    f"SELECT COALESCE(NULLIF(final_provider, ''), 'unknown'){trace}{reason} "
                    f"FROM routing_decisions WHERE {where} {prov}"
                    f"AND ({real_decision_sql(con)})", params):
                    rows.append((provider, None, why))
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
            # the client's auth kind says who pays for a forwarded call; a locally served call
            # is local whatever header the client sent
            auth = row.get("auth") if label == PROXY_CLAUDE_LABEL else None
            rows.append((label, auth if isinstance(auth, str) else None, None))
    return rows if have_data else None


def executed_by_provider(bounds: Bounds, *, db_path=None, proxy_path=None,
                         timeout_s: float = 0.25) -> dict[str, int] | None:
    """``{provider: executed calls}`` in the window (:func:`executed_rows`); ``None`` = no data."""
    rows = executed_rows(bounds, db_path=db_path, proxy_path=proxy_path, timeout_s=timeout_s)
    if rows is None:
        return None
    counts: dict[str, int] = {}
    for provider, _auth, _why in rows:
        counts[provider] = counts.get(provider, 0) + 1
    return counts


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


def _alias_seat(seats, provider: str):
    attr = SEAT_ALIASES.get(provider)
    return getattr(seats, attr, None) if attr else None


def provider_class(provider: str, *, auth: str | None = None, reason: str | None = None,
                   seats=None) -> str:
    """``local`` | ``subscription`` | ``paid`` | ``unclassified`` (D-205: first match wins, and
    no plan or model name is written in this module).

    1. Row evidence: a proxy row's ``auth`` (``oauth`` = subscription, ``api_key`` = paid), or a
       ``usage`` row's provider ``subscription`` / reason ``claude_code_subscription``.
    2. The router's ``LOCAL_PROVIDERS`` vocabulary.
    3. Discovered seats (``seats.Seats``): a present seat for the provider (``SEAT_ALIASES``) =
       subscription; an API-key login, or ``<PROVIDER>_API_KEY`` set = paid.
    4. Otherwise ``unclassified``: shown on its own, never folded into paid or subscription."""
    if auth == AUTH_SUBSCRIPTION:
        return "subscription"
    if auth == AUTH_PAID:
        return "paid"
    if provider == PROVIDER_SUBSCRIPTION or reason == REASON_SUBSCRIPTION:
        return "subscription"
    if provider in LOCAL_PROVIDERS:
        return "local"
    if seats is not None:
        seat = _alias_seat(seats, provider)
        if seat is not None and seat.present:
            return "subscription"
        if seat is not None and seat.kind == "api-key":
            return "paid"
        if (getattr(seats, "api_keys", None) or {}).get(f"{provider.upper()}_API_KEY"):
            return "paid"
    return "unclassified"


def classify_rows(rows: "list[tuple] | None", seats=None) -> dict[str, int] | None:
    """``{local, subscription, paid, unclassified}`` from :func:`executed_rows`; None = no data."""
    if rows is None:
        return None
    mix = dict.fromkeys(CLASSES, 0)
    for provider, auth, why in rows:
        mix[provider_class(provider, auth=auth, reason=why, seats=seats)] += 1
    return mix


def local_paid_mix(executed: dict[str, int] | None, seats=None) -> dict[str, int]:
    """The four-class mix from per-provider counts (no row evidence: use :func:`classify_rows`
    for that). ``unclassified`` is its own bucket."""
    mix = dict.fromkeys(CLASSES, 0)
    for provider, n in (executed or {}).items():
        mix[provider_class(provider, seats=seats)] += int(n)
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


# ── Figure: a number with its n, window and source ────────────────────────────

@dataclass(frozen=True)
class Figure:
    """One printed number. ``value`` None = unknown (never 0); ``n`` = rows behind it."""

    value: float | None
    n: int | None
    window: str
    as_of: str
    source: str
    kind: str = "measured"             # measured | estimated
    na_reason: str | None = None

    def to_dict(self) -> dict:
        return {"value": self.value, "n": self.n, "window": self.window, "as_of": self.as_of,
                "source": self.source, "kind": self.kind, "na_reason": self.na_reason}

    @classmethod
    def from_dict(cls, d: dict) -> "Figure":
        return cls(d.get("value"), d.get("n"), str(d.get("window") or ""),
                   str(d.get("as_of") or ""), str(d.get("source") or ""),
                   str(d.get("kind") or "measured"), d.get("na_reason"))


def show(fig: "Figure | None", fmt: str = "{}") -> str | None:
    """The one formatter. None (print ``n/a`` or nothing) when the value or n is unknown or the
    window is empty: a number without its n and window is not shown (strict NS rule)."""
    if fig is None or fig.value is None or fig.n is None or not fig.window:
        return None
    return fmt.format(fig.value)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty sorted list."""
    rank = max(1, -(-int(pct * len(sorted_vals)) // 100))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


LATENCY_PCTS = (("p50", 50), ("p95", 95), ("p99", 99))


def latency_figures(bounds: Bounds, *, db_path=None, timeout_s: float = 0.5) -> tuple[dict, int | None, int | None]:
    """``({p50, p95, p99: Figure}, n_recorded, n_total)`` over the executed ``routing_decisions``
    rows (the same rows as :func:`executed_by_provider`). ``usage.latency_ms`` is NOT NULL so it
    cannot say "not recorded"; ``routing_decisions.latency_ms`` is nullable. A NULL, or a 0 (the
    writers' ``latency_ms or 0.0`` default for a missing value: no real call takes 0 ms), is not
    recorded and is left out of the percentiles. ``n_total`` = rows that could have had one."""
    as_of, window = _iso(bounds.until_ts), bounds.label
    source = "routing_decisions.latency_ms (recorded rows only)"

    def unknown(why: str, n: int | None) -> dict:
        return {k: Figure(None, n, window, as_of, source, "measured", why) for k, _p in LATENCY_PCTS}

    con = _connect(db_path, timeout_s)
    if con is None:
        return unknown("source missing", None), None, None
    try:
        cols = _columns(con, "routing_decisions")
        if not cols:
            return unknown("source missing", None), None, None
        where, params = bounds.sql(cols)
        prov = "AND provenance = 'runtime' " if "provenance" in cols else ""
        base = (f"FROM routing_decisions WHERE {where} {prov}AND ({real_decision_sql(con)})")
        total = int(con.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])  # nosec B608
        vals: list[float] = []
        if "latency_ms" in cols:
            vals = sorted(float(r[0]) for r in con.execute(  # nosec B608
                f"SELECT latency_ms {base} AND latency_ms IS NOT NULL AND latency_ms > 0", params))
    except sqlite3.Error:
        return unknown("source unreadable", None), None, None
    finally:
        con.close()
    if not vals:
        return unknown(f"no latency recorded (0 of {total} rows)", 0), 0, total
    return ({k: Figure(_percentile(vals, p), len(vals), window, as_of, source)
             for k, p in LATENCY_PCTS}, len(vals), total)


# ── which sources exist (presence and counts only) ────────────────────────────

_JSONL_SOURCES = ("proxy_calls.jsonl", "technical_ops.jsonl", "quota_history.jsonl")
_DB_SOURCES = ("usage", "routing_decisions", *_PLATFORM_TABLES)


def _absent() -> dict:
    return {"present": False, "rows": None, "newest_ts": None}


def _jsonl_stat(path: Path) -> dict:
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except OSError:
        return _absent()
    newest = None
    for ln in reversed(lines):
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        ts = row.get("ts", row.get("timestamp")) if isinstance(row, dict) else None
        if ts is not None:
            newest = ts
            break
    return {"present": True, "rows": len(lines), "newest_ts": newest}


def _db_sources(db_path) -> dict:
    """Presence / rows / newest timestamp of each database table. An unreadable database reports
    every table absent (the answer is "cannot see it", which a surface prints as n/a)."""
    out = {name: _absent() for name in _DB_SOURCES}
    con = _connect(db_path, 0.5)
    if con is None:
        return out
    try:
        for name in _DB_SOURCES:
            if not _table_exists(con, name):
                continue
            col = "timestamp" if "timestamp" in _columns(con, name) else None
            row = con.execute(  # nosec B608 -- table names are module constants
                f"SELECT COUNT(*), {'MAX(' + col + ')' if col else 'NULL'} FROM {name}").fetchone()
            out[name] = {"present": True, "rows": int(row[0]), "newest_ts": row[1]}
    except sqlite3.Error:
        return {name: _absent() for name in _DB_SOURCES}
    finally:
        con.close()
    return out


def sources_map(*, db_path=None, proxy_path=None, seats=None) -> dict:
    """``{name: {present, rows, newest_ts}}`` for each source a surface may print from. Rows are
    the source's whole row count (not the window's). ``seats.json`` also carries ``stale``."""
    out = _db_sources(db_path)
    for name in _JSONL_SOURCES:
        path = _proxy_ledger_path(proxy_path) if name == "proxy_calls.jsonl" else paths.state_path(name)
        out[name] = _jsonl_stat(path)
    if seats is None:
        out["seats.json"] = {**_absent(), "stale": None}
    else:
        out["seats.json"] = {"present": True, "rows": 1, "newest_ts": seats.detected_at or None,
                             "stale": bool(seats.is_stale())}
    return out


# ── the window summary every surface prints ───────────────────────────────────

_LOAD_SEATS = object()          # sentinel: read seats.json (the default)


def _load_seats():
    from llm_router import seats as _seats          # stdlib-only module

    return _seats.load_seats()


@dataclass(frozen=True)
class LedgerWindow:
    """Schema v2. Unknown is ``None``, never 0: ``n`` and the class counts are None without any
    execution source, ``saved_*`` without a usage database."""

    as_of: str
    window: str                  # label
    source: str
    estimated: bool              # the saved figure is an estimate, never realized money
    n: int | None = None         # executed calls in the window (sum of ``executed``)
    executed: tuple = ()         # ((provider, n), ...) sorted by -n, provider
    local_n: int | None = None
    subscription_n: int | None = None
    paid_n: int | None = None
    unclassified_n: int | None = None
    saved_usd: float | None = None
    saved_n: int | None = None
    verified_usd: float | None = None
    unverified_usd: float | None = None
    latency_p50: Figure | None = None
    latency_p95: Figure | None = None
    latency_p99: Figure | None = None
    latency_n_recorded: int | None = None
    latency_n_total: int | None = None
    sources: dict = field(default_factory=dict)
    scope: str = "all"
    has_data: bool = True
    v: int = 2

    def line(self) -> str:
        return format_line(self)

    def figures(self) -> dict:
        """The printed fields as :class:`Figure` s: ``executed``, ``mix_<class>``, ``saved``,
        ``latency_p50/p95/p99``."""
        def fig(value, n, source, kind="measured", why=None):
            return Figure(value, n, self.window, self.as_of, source, kind,
                          None if value is not None else (why or "source missing"))

        out = {"executed": fig(self.n, self.n, self.source),
               "saved": fig(self.saved_usd, self.saved_n, self.source, "estimated")}
        for cls in CLASSES:
            out[f"mix_{cls}"] = fig(getattr(self, f"{cls}_n" if cls != "local" else "local_n"),
                                    self.n, self.source)
        for key in ("latency_p50", "latency_p95", "latency_p99"):
            lat = getattr(self, key)
            if lat is not None:
                out[key] = lat
        return out

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__
             if k not in ("executed", "latency_p50", "latency_p95", "latency_p99")}
        d["executed"] = [[p, n] for p, n in self.executed]
        for key in ("latency_p50", "latency_p95", "latency_p99"):
            lat = getattr(self, key)
            d[key] = lat.to_dict() if lat is not None else None
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "LedgerWindow":
        d = json.loads(text)
        if d.get("v") != 2:
            raise ValueError(f"unsupported LedgerWindow version: {d.get('v')!r}")
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        known["executed"] = tuple((p, n) for p, n in d.get("executed") or ())
        for key in ("latency_p50", "latency_p95", "latency_p99"):
            known[key] = Figure.from_dict(d[key]) if d.get(key) else None
        return cls(**known)


_SCOPE_WINDOWS = {"all": "lifetime", "session": "session", "day": "today", "7d": "7d"}


def _is_number(x) -> bool:
    return isinstance(x, (int, float, datetime)) and not isinstance(x, bool)


def window_summary(since: "str | Bounds | float | datetime | None" = "24h",
                   until: "float | datetime | None" = None, scope: str = "all", *,
                   session_id: str | None = None, name: str | None = None, db_path=None,
                   proxy_path=None, now=None, session_start: float | None = None,
                   seats=_LOAD_SEATS) -> LedgerWindow:
    """Everything a surface prints for one window, from one call.

    * ``window_summary(since, until=None, scope="all"|"session"|"day"|"7d", *, session_id, name)``:
      ``since`` an epoch (or datetime), ``until`` default now, closed bounds; ``name`` labels it.
      ``since=None`` takes the named window for ``scope`` (``day`` = today, ``7d``, ``all`` =
      lifetime, ``session``). ``scope="session"`` with a ``session_id`` narrows to that id.
    * Legacy: ``window_summary("24h", now=...)`` (a named window string or a ``Bounds``).

    ``seats`` is a ``seats.Seats`` or None (no seats file); by default ``seats.json`` is read, so
    call this only from a refresher or a CLI, never a hook's synchronous path."""
    if isinstance(since, Bounds):
        bounds = since
    elif _is_number(since):
        until_ts = _now_ts(now if until is None else until)
        since_ts = _now_ts(since)
        sid = session_id if scope == "session" else None
        label = name or ("custom" if sid is None else f"session {sid[:8]}")
        bounds = Bounds(name or "custom", label, since_ts, until_ts,
                        "session_id" if sid else "all", sid)
    else:
        if since is None:
            if scope not in _SCOPE_WINDOWS:
                raise ValueError(f"unknown scope: {scope!r}")
            since = _SCOPE_WINDOWS[scope]
        bounds = resolve(since, now=now if until is None else until,
                         session_start=session_start, session_id=session_id)
    seat_obj = _load_seats() if seats is _LOAD_SEATS else seats
    rows = executed_rows(bounds, db_path=db_path, proxy_path=proxy_path)
    sources = sources_map(db_path=db_path, proxy_path=proxy_path, seats=seat_obj)
    lat, n_rec, n_tot = latency_figures(bounds, db_path=db_path)
    money = money_window(bounds, db_path=db_path) if _db_path(db_path).is_file() else None
    classes = classify_rows(rows, seat_obj)
    counts: dict[str, int] = {}
    for provider, _a, _w in rows or ():
        counts[provider] = counts.get(provider, 0) + 1
    items = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return LedgerWindow(
        as_of=_iso(bounds.until_ts), window=bounds.label,
        source="routing_decisions(runtime)+proxy_calls.jsonl; usage+savings_stats (saved)",
        estimated=True, n=None if rows is None else len(rows), executed=items,
        local_n=None if classes is None else classes["local"],
        subscription_n=None if classes is None else classes["subscription"],
        paid_n=None if classes is None else classes["paid"],
        unclassified_n=None if classes is None else classes["unclassified"],
        saved_usd=None if money is None else money.saved_usd,
        saved_n=None if money is None else money.saved_n,
        verified_usd=None if money is None else money.verified_usd,
        unverified_usd=None if money is None else money.unverified_usd,
        latency_p50=lat["p50"], latency_p95=lat["p95"], latency_p99=lat["p99"],
        latency_n_recorded=n_rec, latency_n_total=n_tot, sources=sources,
        scope=bounds.scope, has_data=rows is not None)


def format_line(lw: LedgerWindow) -> str:
    """The one printed form. Identity tests compare this string across surfaces. Unknown parts
    print ``n/a``; ``unclassified`` appears only when non-zero."""
    if not lw.has_data:
        return f"ledger {lw.window}: no execution data (usage.db and proxy ledger missing)"
    na = lambda v: "n/a" if v is None else f"{v}"                      # noqa: E731
    by = " ".join(f"{p} {n}" for p, n in lw.executed) or "none"
    mix = (f"local {na(lw.local_n)} / subscription {na(lw.subscription_n)} / paid {na(lw.paid_n)}"
           + (f" / unclassified {lw.unclassified_n}" if lw.unclassified_n else ""))
    saved = (f"est. saved ${lw.saved_usd:,.2f} (n={lw.saved_n:,})"
             if lw.saved_usd is not None and lw.saved_n is not None else "est. saved n/a")
    return f"ledger {lw.window}: {na(lw.n)} executed ({by}) · mix {mix} · {saved}"
