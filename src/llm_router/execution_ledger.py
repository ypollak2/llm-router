# Ported from Chuzom's execution_ledger.py; env vars renamed to LLM_ROUTER_*;
# data source rewired to llm-router's layer (shares llm-router's own
# ``usage.db`` via ``get_config().llm_router_db_path`` instead of a
# chuzom-specific state directory).
"""Canonical execution ledger — the SINGLE append-only source of truth for cost.

Every provider *attempt* that consumes billable tokens or quota is recorded here
exactly once as its own event, for every outcome (accepted, rejected-by-gate,
rejected-by-quality, retry, escalation, emergency-fallback, timeout-with-known-usage,
partial-with-known-usage). Route/session/period totals are DERIVED from these
events by the aggregation layer below — no surface may keep its own cost
arithmetic.

Invariants enforced structurally here:
  * INV-COST-001 — every billable attempt is one ``attempt_*`` event.
  * INV-COST-002 — ``get_route_accounting(route_id).actual_cost_usd`` == Σ attempt costs.
  * INV-COST-003 — ``event_id`` is the PRIMARY KEY; re-recording an event is a no-op
    (``INSERT OR IGNORE``), so aggregation is idempotent and nothing is double-counted.
  * INV-COST-004 — the aggregation functions are the ONLY cost totals; surfaces delegate.
  * INV-ROUTE-004/005 — ``terminal_state`` is a first-class recorded field.

Storage: table ``execution_events`` inside the same SQLite database llm-router's
``cost.py`` already manages (``get_config().llm_router_db_path``, normally
``~/.llm-router/usage.db``). Writes are FAIL-OPEN and never raise into the routing
path — a lost metric is not a lost turn. Aggregation reads are strict: a
reconciliation mismatch is surfaced, never coerced.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from llm_router.contracts import (
    BILLABLE_EVENTS,
    CLAUDE_PROVIDERS,
    COUNTS_AS_REALIZED,
    EXECUTION_EVENTS_COLUMNS,
    EXECUTION_LEDGER_SCHEMA_VERSION,
    AdoptionMethod,
    EventType,
    RealizationStatus,
    TerminalState,
)

SCHEMA_VERSION = EXECUTION_LEDGER_SCHEMA_VERSION


@dataclass
class LedgerEvent:
    """One append-only execution event. ``event_id`` is unique (idempotent write)."""

    # Identity
    schema_version: int = SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = 0.0                       # stamped on write if 0
    session_id: str = ""
    turn_id: str = ""
    route_id: str = ""
    attempt_id: str = ""
    event_type: EventType = "route_started"

    # Classification
    task_type: str = "unknown"
    routing_profile: str = "unknown"
    host_mode: str = "unknown"            # "subscription" | "metered" | "unknown"

    # Provider / model
    provider: str = ""
    model: str = ""

    # Tokens & cost (measured; None = unknown, never fabricated)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    measured_cost_usd: float | None = None
    baseline_equivalent_cost_usd: float | None = None

    # Realized-savings ledger (Gaps 1/2/3). All optional/None-safe so
    # pre-migration rows and non-attempt event types remain valid.
    classifier_cost_usd: float | None = None       # Gap 1: classifier spend for this attempt
    failed_attempt_cost_usd: float | None = None    # Gap 1: route's running failed-attempt cost,
                                                      # carried onto the accepted attempt (no
                                                      # double count — set ONLY on the accepted row)
    baseline_tokens: int | None = None              # Gap 2: actual_proxy
    adoption_method: AdoptionMethod | None = None    # Gap 3: how verified_used was confirmed

    # Orchestration overhead (INV-COST-005)
    hook_input_tokens: int | None = None
    hook_output_tokens: int | None = None

    # Outcome
    accepted: bool | None = None
    rejected: bool | None = None
    rejection_reason: str | None = None
    escalation_reason: str | None = None
    fallback_reason: str | None = None
    provider_failure_reason: str | None = None

    # Realization / override
    used_by_host: bool | None = None
    realization_status: RealizationStatus | None = None
    override_type: str | None = None      # "native_tool" | "plain_text" | None

    # Terminal state (INV-ROUTE-004/005) — set on route_completed/route_failed events
    terminal_state: TerminalState | None = None

    metadata: dict[str, Any] = field(default_factory=dict)


# ── Storage ───────────────────────────────────────────────────────────────────
def _db_path() -> Path:
    override = os.environ.get("LLM_ROUTER_EXECUTION_LEDGER_DB")
    if override:
        return Path(override)
    try:
        from llm_router.config import get_config

        return get_config().llm_router_db_path
    except Exception:
        return Path.home() / ".llm-router" / "usage.db"


_COLUMNS: tuple[str, ...] = EXECUTION_EVENTS_COLUMNS

_DDL = """
CREATE TABLE IF NOT EXISTS execution_events (
    schema_version INTEGER NOT NULL,
    event_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    route_id TEXT,
    attempt_id TEXT,
    event_type TEXT NOT NULL,
    task_type TEXT,
    routing_profile TEXT,
    host_mode TEXT,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    measured_cost_usd REAL,
    baseline_equivalent_cost_usd REAL,
    hook_input_tokens INTEGER,
    hook_output_tokens INTEGER,
    accepted INTEGER,
    rejected INTEGER,
    rejection_reason TEXT,
    escalation_reason TEXT,
    fallback_reason TEXT,
    provider_failure_reason TEXT,
    used_by_host INTEGER,
    realization_status TEXT,
    override_type TEXT,
    terminal_state TEXT,
    metadata TEXT,
    classifier_cost_usd REAL,
    failed_attempt_cost_usd REAL,
    baseline_tokens INTEGER,
    adoption_method TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_route ON execution_events(route_id);
CREATE INDEX IF NOT EXISTS idx_exec_session ON execution_events(session_id);
CREATE INDEX IF NOT EXISTS idx_exec_ts ON execution_events(ts);
"""

# ── Migrations ───────────────────────────────────────────────────────────────
# `execution_events` is created via `CREATE TABLE IF NOT EXISTS` with no ALTER
# path, so a pre-existing `usage.db` (written only by cost.py, before this
# module ever ran) lacks these columns. Mirrors the idempotent-migration-list
# pattern in cost.py (e.g. MIGRATE_USAGE_ADD_SAVINGS): each statement is
# applied individually in `_connect`, wrapped in its own try/except so an
# already-migrated DB (or a fresh one where _DDL already created the column)
# is a silent no-op. Never breaks a pre-existing DB.
_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE execution_events ADD COLUMN classifier_cost_usd REAL",
    "ALTER TABLE execution_events ADD COLUMN failed_attempt_cost_usd REAL",
    "ALTER TABLE execution_events ADD COLUMN baseline_tokens INTEGER",
    "ALTER TABLE execution_events ADD COLUMN adoption_method TEXT",
)


def _secure_perms(path: Path) -> None:
    """Ensure *path* is mode 0600, repairing looser existing perms.

    The ledger shares ``usage.db`` with cost.py's own tables but opened it with
    no permission hardening — when execution_ledger was the FIRST writer of the
    file the OS default left it 0644. This guarantees 0600 independently of
    which module touches the shared file first.
    """
    import stat as _stat
    try:
        if _stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        try:
            p.touch(mode=0o600)
        except OSError:
            pass
    else:
        _secure_perms(p)
    # 30s busy-timeout: under pathological CI-runner load, rapid open/write/
    # close cycles can transiently hold the WAL lock long enough that a short
    # wait errors with `database is locked`. A longer wait lets the writer
    # drain instead of failing.
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    for _stmt in _MIGRATIONS:
        try:
            conn.execute(_stmt)
        except sqlite3.OperationalError:
            pass  # column already exists (fresh DB via _DDL, or already migrated)
    conn.commit()
    # WAL/SHM sidecars can carry the same rows — keep them 0600 too.
    for suffix in ("-wal", "-shm"):
        _sidecar = p.with_name(p.name + suffix)
        if _sidecar.exists():
            _secure_perms(_sidecar)
    return conn


def _row_to_value(field_name: str, ev: LedgerEvent) -> Any:
    v = getattr(ev, field_name)
    if field_name == "metadata":
        return json.dumps(v or {})
    if isinstance(v, bool):
        return int(v)
    return v


def record_event(ev: LedgerEvent, *, path: Path | None = None) -> bool:
    """Append *ev* to the canonical ledger. Idempotent on ``event_id`` (INV-COST-003).

    FAIL-OPEN: returns False on any error, never raises into the caller (routing path).
    """
    try:
        if not ev.ts:
            ev.ts = time.time()
        conn = _connect(path)
        try:
            placeholders = ",".join("?" for _ in _COLUMNS)
            values = [_row_to_value(c, ev) for c in _COLUMNS]
            conn.execute(
                f"INSERT OR IGNORE INTO execution_events ({','.join(_COLUMNS)}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001 — a ledger failure must never break routing
        return False


def _load_rows(where: str, params: tuple, path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        conn.row_factory = sqlite3.Row
        # Deterministic order. `_aggregate()`'s realization-status merge is
        # last-write-wins per route_id, so an unordered scan would let
        # identical conflicting rows flip realized_savings_usd between runs.
        # Order by the event timestamp (then the primary key) so "last write
        # wins" means the chronologically-latest event, deterministically.
        cur = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM execution_events WHERE {where} "
            "ORDER BY ts ASC, event_id ASC",
            params,
        )
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["metadata"] = json.loads(d.get("metadata") or "{}")
            out.append(d)
        return out
    finally:
        conn.close()


# ── Aggregation layer — the ONLY cost totals (INV-COST-002/004) ────────────────
@dataclass
class Accounting:
    """Derived totals for a route / turn / session / period. Read-model, never stored."""

    scope: str
    scope_id: str
    attempt_count: int = 0
    billable_attempt_count: int = 0
    accepted_attempt_count: int = 0
    rejected_attempt_count: int = 0
    actual_cost_usd: float = 0.0                # Σ measured_cost over billable attempts
    baseline_equivalent_cost_usd: float = 0.0   # Σ baseline_equivalent over billable attempts
    hook_input_tokens: int = 0
    hook_output_tokens: int = 0
    terminal_states: dict[str, int] = field(default_factory=dict)
    cost_unknown_attempts: int = 0              # billable attempts with measured_cost=None
    # ── Realization (Gate 18) ────────────────────────────────────────────────
    # A route's potential saving (baseline_equivalent − actual) is only REALIZED
    # if the routed result was verifiably used by the host. Routes whose
    # realization is `verified_overridden` (host went its own way) or `unknown`
    # (couldn't verify) must NOT be counted as realized savings.
    realized_routes: int = 0                     # realization_status == verified_used
    overridden_routes: int = 0                   # verified_overridden
    realization_unknown_routes: int = 0          # unknown (or never verified)
    potential_savings_usd: float = 0.0           # Σ max(0, baseline_eq − actual) over ALL routes
    realized_savings_usd: float = 0.0            # Σ that saving ONLY on verified_used routes,
                                                   # ADOPTION-GATED (adoption_method in
                                                   # COUNTS_AS_REALIZED; NULL on a verified_used
                                                   # row is back-compat-treated as door_call)

    # ── Realized-savings ledger (Gaps 1/2/3) ────────────────────────────────
    classifier_cost_usd_total: float = 0.0        # Σ classifier spend over accepted attempts
    failed_attempt_cost_usd_total: float = 0.0    # Σ failed-attempt cost carried onto accepted rows
    hook_overhead_usd: float = 0.0                # Σ hook token cost; $0 unless row.host_mode=="metered"
                                                    # (marginal-$0 rule on subscription)
    net_realized_savings_usd: float = 0.0         # realized_savings_usd − classifier − failed − hook_overhead
    overhead_as_pct_of_gross: float = 0.0         # overhead / realized_savings_usd; 0.0 if gross is ~0
    # "Claude tokens NOT consumed" — Σ (input+output) tokens actually served by
    # a NON-Claude model, on routes that are realized (verified_used + adoption
    # in COUNTS_AS_REALIZED) AND whose FINAL model is not Claude.
    realized_quota_tokens_saved: int = 0
    realized_savings_by_host_mode: dict[str, float] = field(default_factory=dict)
    quota_tokens_saved_by_host_mode: dict[str, int] = field(default_factory=dict)
    realized_by_adoption_method: dict[str, float] = field(default_factory=dict)
    likely_used_routes: int = 0                   # verified_used + adoption_method == content_match

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _host_opus_rates() -> tuple[float, float]:
    """Lazy import of cost.py's host-Opus per-token rates for `hook_overhead_usd`
    pricing. Lazy (not module-level) so execution_ledger.py stays importable even
    if cost.py's heavier dependency surface fails to load, and fail-open to
    ``(0.0, 0.0)`` — the conservative "never fabricate a cost" default. No
    circular import: cost.py has no reference to execution_ledger.py.
    """
    try:
        from llm_router.cost import _HOST_INPUT_PER_M, _HOST_OUTPUT_PER_M
        return float(_HOST_INPUT_PER_M), float(_HOST_OUTPUT_PER_M)
    except Exception:
        return 0.0, 0.0


def _aggregate(scope: str, scope_id: str, rows: list[dict[str, Any]]) -> Accounting:
    acc = Accounting(scope=scope, scope_id=scope_id)
    # Gate 18: potential saving is per-ROUTE (baseline_eq − actual), and it's only
    # REALIZED when that route's realization is verified_used. Accumulate per route
    # so a single unknown/overridden route can't inflate the realized total.
    route_potential: dict[str, float] = {}       # route_id → Σ(baseline_eq − actual)
    route_realization: dict[str, str] = {}       # route_id → last realization_status seen
    # adoption_method is written on the SAME row that carries realization_status
    # (a separate `route_realized` event, not the original attempt_completed
    # row), so it's captured alongside route_realization below, not in the
    # billable block.
    route_adoption: dict[str, str | None] = {}    # route_id → last adoption_method seen
    route_host_mode: dict[str, str] = {}          # route_id → last known non-"unknown" host_mode
    route_actual_tokens: dict[str, int] = {}      # route_id → Σ(input+output) over billable rows
    route_baseline_tokens: dict[str, int] = {}    # route_id → Σ baseline_tokens (actual_proxy;
                                                    # kept for back-compat/debugging, no longer
                                                    # used to derive quota — see route_final_provider)
    route_final_provider: dict[str, str] = {}      # route_id → provider of the LAST billable
                                                    # (attempt_completed/rejected/failed) row seen —
                                                    # i.e. the model that actually ran on this route
    _host_in_pm, _host_out_pm = _host_opus_rates()
    for r in rows:
        et = r["event_type"]
        rid = r.get("route_id") or ""
        if et in BILLABLE_EVENTS:
            acc.attempt_count += 1
            acc.billable_attempt_count += 1
            if et == "attempt_completed":
                acc.accepted_attempt_count += 1
            elif et == "attempt_rejected":
                acc.rejected_attempt_count += 1
            cost = r.get("measured_cost_usd")
            if cost is None:
                acc.cost_unknown_attempts += 1
            else:
                acc.actual_cost_usd += float(cost)
                route_potential[rid] = route_potential.get(rid, 0.0) - float(cost)
            base = r.get("baseline_equivalent_cost_usd")
            if base is not None:
                acc.baseline_equivalent_cost_usd += float(base)
                route_potential[rid] = route_potential.get(rid, 0.0) + float(base)
            # Gap 1: classifier + carried failed-attempt cost, scope-wide totals
            # (net_realized_savings_usd is a single scope-level P&L, not per-route).
            ctok = r.get("classifier_cost_usd")
            if ctok is not None:
                acc.classifier_cost_usd_total += float(ctok)
            ftok = r.get("failed_attempt_cost_usd")
            if ftok is not None:
                acc.failed_attempt_cost_usd_total += float(ftok)
            # Gap 2: actual + baseline tokens, per route. `route_actual_tokens`
            # feeds the quota derivation below (served-off-Claude tokens);
            # `route_baseline_tokens` is retained for back-compat / debugging
            # only and is no longer part of that derivation.
            itok = r.get("input_tokens") or 0
            otok = r.get("output_tokens") or 0
            if itok or otok:
                route_actual_tokens[rid] = route_actual_tokens.get(rid, 0) + int(itok) + int(otok)
            btok = r.get("baseline_tokens")
            if btok is not None:
                route_baseline_tokens[rid] = route_baseline_tokens.get(rid, 0) + int(btok)
            # Track the provider of the LAST billable row for this route (rows
            # are loaded ts ASC — see `_load_rows`), so we know which model
            # actually served the route's final/accepted attempt.
            prov = r.get("provider")
            if prov:
                route_final_provider[rid] = prov
        # host_mode: track the last known CONFIRMED value (never let a row with no
        # host_mode/"unknown" clobber a previously-seen subscription/metered value).
        hm = r.get("host_mode")
        if hm and hm != "unknown":
            route_host_mode[rid] = hm
        # Realization tracking: the explicit status field wins; the
        # `realization_unknown` event type is a fallback signal for unknown.
        rs = r.get("realization_status")
        if rs:
            route_realization[rid] = rs
            route_adoption[rid] = r.get("adoption_method")
        elif et == "realization_unknown":
            route_realization.setdefault(rid, "unknown")
        if r.get("hook_input_tokens"):
            acc.hook_input_tokens += int(r["hook_input_tokens"])
        if r.get("hook_output_tokens"):
            acc.hook_output_tokens += int(r["hook_output_tokens"])
        # hook_overhead_usd: marginal-$0 rule — only a CONFIRMED metered row
        # prices the hook tokens; subscription/unknown never fabricate a cost.
        # Row-level host_mode, not a global check, since the ledger already
        # carries host_mode per-event and results must be bucketed by it.
        hook_in = r.get("hook_input_tokens") or 0
        hook_out = r.get("hook_output_tokens") or 0
        if (hook_in or hook_out) and hm == "metered":
            acc.hook_overhead_usd += (
                int(hook_in) * _host_in_pm + int(hook_out) * _host_out_pm
            ) / 1_000_000
        ts = r.get("terminal_state")
        if ts:
            acc.terminal_states[ts] = acc.terminal_states.get(ts, 0) + 1
    acc.actual_cost_usd = round(acc.actual_cost_usd, 6)
    acc.baseline_equivalent_cost_usd = round(acc.baseline_equivalent_cost_usd, 6)
    acc.classifier_cost_usd_total = round(acc.classifier_cost_usd_total, 6)
    acc.failed_attempt_cost_usd_total = round(acc.failed_attempt_cost_usd_total, 6)
    acc.hook_overhead_usd = round(acc.hook_overhead_usd, 6)

    # ── Realization-gated savings (Gate 18) ──────────────────────────────────
    # potential = every route's positive saving; realized = ONLY verified_used
    # routes. A route with realization `unknown` or `verified_overridden` — or no
    # realization event at all — contributes to potential but NEVER to realized,
    # so an unverified saving can never be reported as realized.
    #
    # Adoption gating: within verified_used routes, realized_savings_usd
    # additionally requires adoption_method in COUNTS_AS_REALIZED (door_call or
    # agent_marked). A verified_used row with NO adoption_method (NULL) predates
    # this gating and is back-compat-treated as door_call — the strongest signal —
    # rather than silently dropping every pre-migration verified_used route from
    # realized savings. content_match is evidence but not proof: it lands in
    # likely_used_routes instead of realized_savings_usd.
    for rid, delta in route_potential.items():
        saving = max(0.0, delta)
        acc.potential_savings_usd += saving
        if route_realization.get(rid) != "verified_used":
            continue
        adoption = route_adoption.get(rid)
        if adoption is None:
            adoption = "door_call"  # pre-migration back-compat ONLY
        host_mode = route_host_mode.get(rid, "unknown")
        if adoption in COUNTS_AS_REALIZED:
            acc.realized_savings_usd += saving
            acc.realized_savings_by_host_mode[host_mode] = (
                acc.realized_savings_by_host_mode.get(host_mode, 0.0) + saving
            )
            acc.realized_by_adoption_method[adoption] = (
                acc.realized_by_adoption_method.get(adoption, 0.0) + saving
            )
            # quota-tokens-saved = "Claude tokens NOT consumed" on routes that
            # count as realized. Every token actually served by a NON-Claude
            # model on an adopted route is a Claude/quota token that would
            # otherwise have been spent; if the route's FINAL model IS Claude
            # (e.g. escalated to frontier), Claude ran, so the route saved 0
            # quota. This avoids a self-subtracting `baseline_tokens −
            # actual_tokens` formula that would be a structural tautology
            # (baseline_tokens is written as the SAME accepted attempt's own
            # token count, so the delta is always 0 by construction) — see
            # `CLAUDE_PROVIDERS` above.
            # Never fabricate: an UNKNOWN provider (no attempt row carried one —
            # e.g. a pre-migration route) does NOT count as "non-Claude"; it
            # counts as 0 quota saved rather than assuming a saving we can't
            # actually verify.
            final_provider = route_final_provider.get(rid, "")
            quota = (
                route_actual_tokens.get(rid, 0)
                if final_provider and final_provider.lower() not in CLAUDE_PROVIDERS
                else 0
            )
            if quota:
                acc.realized_quota_tokens_saved += quota
                acc.quota_tokens_saved_by_host_mode[host_mode] = (
                    acc.quota_tokens_saved_by_host_mode.get(host_mode, 0) + quota
                )
        elif adoption == "content_match":
            acc.likely_used_routes += 1
    for rs in route_realization.values():
        if rs == "verified_used":
            acc.realized_routes += 1
        elif rs == "verified_overridden":
            acc.overridden_routes += 1
        elif rs == "unknown":
            acc.realization_unknown_routes += 1
    acc.potential_savings_usd = round(acc.potential_savings_usd, 6)
    acc.realized_savings_usd = round(acc.realized_savings_usd, 6)
    acc.realized_savings_by_host_mode = {
        k: round(v, 6) for k, v in acc.realized_savings_by_host_mode.items()
    }
    acc.realized_by_adoption_method = {
        k: round(v, 6) for k, v in acc.realized_by_adoption_method.items()
    }

    # ── Net realized savings + overhead ratio ────────────────────────────────
    acc.net_realized_savings_usd = round(
        acc.realized_savings_usd
        - acc.classifier_cost_usd_total
        - acc.failed_attempt_cost_usd_total
        - acc.hook_overhead_usd,
        6,
    )
    _overhead = (
        acc.classifier_cost_usd_total
        + acc.failed_attempt_cost_usd_total
        + acc.hook_overhead_usd
    )
    acc.overhead_as_pct_of_gross = (
        round(_overhead / acc.realized_savings_usd, 6)
        if acc.realized_savings_usd > 1e-9
        else 0.0
    )
    return acc


def get_route_accounting(route_id: str, *, path: Path | None = None) -> Accounting:
    """INV-COST-002: actual cost == Σ measured cost over billable attempt events."""
    return _aggregate("route", route_id,
                      _load_rows("route_id = ?", (route_id,), path))


def get_turn_accounting(turn_id: str, *, path: Path | None = None) -> Accounting:
    return _aggregate("turn", turn_id, _load_rows("turn_id = ?", (turn_id,), path))


def get_session_accounting(session_id: str, *, path: Path | None = None) -> Accounting:
    return _aggregate("session", session_id,
                      _load_rows("session_id = ?", (session_id,), path))


def get_period_accounting(
    start_ts: float, end_ts: float, *, path: Path | None = None
) -> Accounting:
    return _aggregate(
        "period", f"{start_ts:.0f}-{end_ts:.0f}",
        _load_rows("ts >= ? AND ts < ?", (start_ts, end_ts), path),
    )


# ── Reconciliation (INV-COST-004) ──────────────────────────────────────────────
@dataclass
class Reconciliation:
    """Result of checking a surface's reported actual-cost against the canonical
    ledger total. INV-COST-004: no user-facing spend surface may report an
    actual-cost total different from the aggregation layer. A surface (or its test)
    calls ``reconcile_session`` with the number it displays; ``reconciled`` is False
    when it drifts from the ledger, or when any billable attempt has unknown cost
    (so "exact" would be a lie — see INV-COST-005 fail-behavior)."""

    scope_id: str
    canonical_actual_usd: float
    reported_actual_usd: float | None
    reconciled: bool
    cost_unknown_attempts: int
    delta_usd: float


def reconcile_session(
    session_id: str,
    reported_actual_usd: float | None = None,
    *,
    tol: float = 1e-6,
    path: Path | None = None,
) -> Reconciliation:
    """Reconcile a surface's ``reported_actual_usd`` against the canonical session
    total. With ``reported_actual_usd=None`` it reports only whether the ledger's own
    total is fully known (no cost_unknown attempts) — the self-consistency check."""
    acc = get_session_accounting(session_id, path=path)
    canonical = acc.actual_cost_usd
    reconciled = acc.cost_unknown_attempts == 0
    delta = 0.0
    if reported_actual_usd is not None:
        delta = round(float(reported_actual_usd) - canonical, 6)
        reconciled = reconciled and abs(delta) <= tol
    return Reconciliation(
        scope_id=session_id,
        canonical_actual_usd=canonical,
        reported_actual_usd=reported_actual_usd,
        reconciled=reconciled,
        cost_unknown_attempts=acc.cost_unknown_attempts,
        delta_usd=delta,
    )
