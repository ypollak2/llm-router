"""Canonical execution ledger — the SINGLE append-only source of truth for cost.

Correctness-reset Phase 2. Every provider *attempt* that consumes billable tokens or
quota is recorded here exactly once as its own event, for every outcome (accepted,
rejected-by-gate, rejected-by-quality, retry, escalation, emergency-fallback,
timeout-with-known-usage, partial-with-known-usage). Route/session/period totals are
DERIVED from these events by the aggregation layer below — no surface may keep its own
cost arithmetic (see ``Docs/correctness-reset/01_FINAL_ACCEPTANCE_CONTRACT.md``).

Invariants enforced structurally here:
  * INV-COST-001 — every billable attempt is one ``attempt_*`` event.
  * INV-COST-002 — ``get_route_accounting(route_id).actual_cost_usd`` == Σ attempt costs.
  * INV-COST-003 — ``event_id`` is the PRIMARY KEY; re-recording an event is a no-op
    (``INSERT OR IGNORE``), so aggregation is idempotent and nothing is double-counted.
  * INV-COST-004 — the aggregation functions are the ONLY cost totals; surfaces delegate.
  * INV-ROUTE-004/005 — ``terminal_state`` is a first-class recorded field.

Storage: table ``execution_events`` inside ``~/.llm-router/usage.db`` (the existing SoT DB).
Writes are FAIL-OPEN and never raise into the routing path — a lost metric is not a lost
turn. Aggregation reads are strict: a reconciliation mismatch is surfaced, never coerced.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import logging

from llm_router.sqlite_wal import enable_wal

_log = logging.getLogger("llm_router.execution_ledger")

SCHEMA_VERSION = 1

# ── Event taxonomy ────────────────────────────────────────────────────────────
EventType = Literal[
    "route_started",
    "directive_injected",
    "attempt_started",
    "attempt_completed",      # billable, accepted (won the route)
    "attempt_rejected",       # billable, rejected by gate/quality — STILL a cost
    "attempt_failed",         # provider error; cost only if usage is known
    "escalation_started",
    "fallback_started",
    "route_completed",
    "route_failed",
    "native_tool_override",
    "plain_text_override",
    "result_used",
    "result_discarded",
    "realization_unknown",
    "provider_health_changed",
    # Phase 0 (realized-savings ledger): written at runtime by
    # enforce-route.py::_record_realization_used / _record_agent_marked but was
    # missing from this type declaration until now. Non-billable — it records
    # adoption evidence for an already-billed route, not a new billable attempt.
    "route_realized",
]

# Event types that carry billable token/quota cost and therefore contribute to
# route/session actual-cost totals. An attempt that consumed tokens is billable
# whether or not its answer was kept.
_BILLABLE_EVENTS: frozenset[str] = frozenset(
    {"attempt_completed", "attempt_rejected", "attempt_failed"}
)

TerminalState = Literal[
    "accepted", "rejected", "failed", "cancelled", "bypassed", "overridden", "unknown",
]
RealizationStatus = Literal["verified_used", "verified_overridden", "unknown"]
# Phase 0: how a `verified_used` row's usage was actually confirmed.
# door_call = host called back through the enforcement door (strongest signal);
# agent_marked = the (currently soak-only) agent-side adoption marker; content_match
# = output-similarity heuristic (enum value only in Phase 0, not implemented as a
# writer yet — see soak/replay.py); unknown = no adoption evidence. Only door_call
# and agent_marked count toward realized savings (`_COUNTS_AS_REALIZED` below).
AdoptionMethod = Literal["door_call", "agent_marked", "content_match", "unknown"]

# Adoption methods strong enough to count a verified_used route's saving as
# REALIZED (as opposed to merely "likely" — see Accounting.likely_used_routes).
# content_match is corroborating evidence, not proof, so it does NOT count here.
_COUNTS_AS_REALIZED: frozenset[str] = frozenset({"door_call", "agent_marked"})

# Phase 0.1: provider strings that identify a response as served by Claude on
# the user's subscription quota (as opposed to a metered/external model).
# Mirrors the identical classification set already used at the response-level
# in router.py (`response.provider in {"claude_subscription", "subscription",
# "anthropic", "claude"}`, ~line 1918) so the ledger's notion of "this attempt
# ran on Claude" stays consistent with the router's own accounting.
_CLAUDE_PROVIDERS: frozenset[str] = frozenset(
    {"claude_subscription", "subscription", "anthropic", "claude"}
)


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

    # Phase 0 (realized-savings ledger — Gaps 1/2/3). All optional/None-safe so
    # pre-migration rows and non-attempt event types remain valid.
    classifier_cost_usd: float | None = None       # Gap 1: classifier spend for this attempt
    failed_attempt_cost_usd: float | None = None    # Gap 1: route's running failed-attempt cost,
                                                      # carried onto the accepted attempt (R6: no
                                                      # double count — set ONLY on the accepted row)
    baseline_tokens: int | None = None              # Gap 2: actual_proxy (see soak/report.py)
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
    return Path.home() / ".llm-router" / "usage.db"


_COLUMNS: tuple[str, ...] = (
    "schema_version", "event_id", "ts", "session_id", "turn_id", "route_id",
    "attempt_id", "event_type", "task_type", "routing_profile", "host_mode",
    "provider", "model", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "measured_cost_usd", "baseline_equivalent_cost_usd",
    "hook_input_tokens", "hook_output_tokens", "accepted", "rejected",
    "rejection_reason", "escalation_reason", "fallback_reason",
    "provider_failure_reason", "used_by_host", "realization_status",
    "override_type", "terminal_state", "metadata",
    # Phase 0 additions — appended at the end so a fresh CREATE TABLE and an
    # ALTER-migrated old DB end up with the same column SET (order doesn't need
    # to match _DDL's declared order; INSERT/SELECT are always by explicit name).
    "classifier_cost_usd", "failed_attempt_cost_usd", "baseline_tokens",
    "adoption_method",
)

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
# path, so a pre-Phase-0 `~/.llm-router/usage.db` lacks these columns. Mirrors the
# idempotent-migration-list pattern in cost.py (e.g. MIGRATE_USAGE_ADD_SAVINGS):
# each statement is applied individually in `_connect`, wrapped in its own
# try/except so an already-migrated DB (or a fresh one where _DDL already
# created the column) is a silent no-op. Never breaks a pre-existing DB.
_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE execution_events ADD COLUMN classifier_cost_usd REAL",
    "ALTER TABLE execution_events ADD COLUMN failed_attempt_cost_usd REAL",
    "ALTER TABLE execution_events ADD COLUMN baseline_tokens INTEGER",
    "ALTER TABLE execution_events ADD COLUMN adoption_method TEXT",
)


def _secure_perms(path: Path) -> None:
    """Ensure *path* is mode 0600, repairing looser existing perms.

    CHZ-AUD-D-02 (RED-2 re-audit): the ledger shares ~/.llm-router/usage.db with the
    cost + session-summary sinks but opened it with no permission hardening — when
    execution_ledger was the FIRST writer of the file (any route in a session that
    never reaches a session-summary save) the OS default left it 0644. This
    guarantees 0600 independently of which module touches the shared file first.
    """
    import stat as _stat
    try:
        if _stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


#: SQLite busy-timeout, seconds. MUST stay strictly below pytest-timeout's
#: `timeout` in pyproject.toml — see _connect() and
#: tests/test_sqlite_timeout_below_test_timeout.py for why equality is a defect.
_BUSY_TIMEOUT_S = 20.0


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
    # Busy-timeout: raised from 5s because under pathological CI-runner load,
    # rapid open/write/close cycles held the WAL lock longer than 5s and the wait
    # errored with `database is locked`. A longer wait lets the writer drain.
    #
    # IT MUST STAY STRICTLY BELOW THE TEST TIMEOUT, and that is why it is 20 and
    # not 30. It was raised to exactly 30.0 while pyproject.toml sets
    # `timeout = 30` for pytest-timeout — the same number. Equal values mean a
    # test that enters the busy-wait is killed at the precise instant SQLite
    # would still be waiting, so the wait can never complete and the test can
    # never recover. It does not fail with a useful error; it dies mid-wait:
    #
    #     execution_ledger.py record_event -> conn.commit()
    #     Failed: Timeout (>30.0s) from pytest-timeout
    #
    # Ten soak tests died that way, identically, at setup. Two settings each
    # sensible in isolation, never checked against each other.
    #
    # 20s keeps 4x the original 5s headroom the CI-load fix was for, and leaves a
    # 10s margin for the test to proceed after the lock clears. The relationship
    # is enforced by tests/test_sqlite_timeout_below_test_timeout.py — change one
    # of these numbers and that test tells you about the other.
    conn = sqlite3.connect(str(p), timeout=_BUSY_TIMEOUT_S)
    # RED5-01 (P0): guarded. Second of three copies of the same cold-start bug —
    # see llm_router/sqlite_wal.py for why the bare form fails both by raising AND,
    # more dangerously, by returning a mode nobody checks.
    enable_wal(conn, label="execution_ledger")
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


#: RED5-02 (P0): how many events this process failed to persist.
#:
#: ``record_event`` is fail-open by design — a ledger write must never break the
#: routing path — but fail-open only works if the failure is *counted*. It was
#: not: the function returned False, all seven call sites discarded the value,
#: and nothing logged. 66 events vanished across 2400 concurrent writes and the
#: only evidence was a total that did not add up. A success signal nobody reads
#: is not a signal, and a fail-open path with no counter is indistinguishable
#: from a path that works.
_dropped_events = 0


def dropped_event_count() -> int:
    """Events this process failed to persist. Surfaced by `doctor` and telemetry."""
    return _dropped_events


def reset_dropped_event_count() -> None:
    """Test seam. Never call this to make a number look better."""
    global _dropped_events
    _dropped_events = 0


def record_event(ev: LedgerEvent, *, path: Path | None = None) -> bool:
    """Append *ev* to the canonical ledger. Idempotent on ``event_id`` (INV-COST-003).

    FAIL-OPEN: returns False on any error, never raises into the caller (routing
    path). A False return is also LOGGED and COUNTED here, so a caller that
    ignores the boolean still cannot make the loss invisible — see
    :func:`dropped_event_count`.
    """
    global _dropped_events
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
    except Exception as exc:  # noqa: BLE001 — a ledger failure must never break routing
        _dropped_events += 1
        _log.warning(
            "LEDGER_EVENT_DROPPED event_id=%s type=%s: %s (dropped this process: %d)",
            getattr(ev, "event_id", "?"),
            getattr(ev, "event_type", "?"),
            exc,
            _dropped_events,
        )
        return False


#: Comparison operators a filter may use. Anything else is rejected rather than
#: passed through, so the operator can never carry SQL.
_ALLOWED_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})

#: Column names a filter may name. `_COLUMNS` is the table's own definition, so
#: an identifier is either a real column or an error — never arbitrary text.
_COLUMN_SET = frozenset(_COLUMNS)

#: One filter: (column, operator, value). The value is always parameterised.
LedgerFilter = tuple[str, str, Any]


def _load_rows(
    filters: Sequence[LedgerFilter],
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Load ledger rows matching every filter, ANDed together.

    WHY THIS TAKES FILTERS AND NOT A `where` STRING
    ------------------------------------------------
    The previous signature was ``_load_rows(where: str, params: tuple, …)`` and
    interpolated ``where`` straight into the query. Every one of its four callers
    passed a string literal with ``?`` placeholders, so it was safe — but the
    safety was a property of the callers, re-established by inspection each time
    anyone asked, and the signature actively invites an f-string from the fifth.

    32_BANDIT_TRIAGE §3 recorded it as "the fragile spot… safe today", which is
    the kind of note that ages badly: it documents a hazard instead of removing
    one, and doc 32 then had to be corrected for a different claim made on the
    same basis.

    So the unsafe call is now unrepresentable rather than merely unused. Callers
    supply data — a column name, an operator, a value — and this function is the
    only place that writes SQL. The column is checked against the table's own
    definition and the operator against a fixed set, so both sides of every
    predicate are constrained and the value is parameterised.

    Args:
        filters: ``(column, operator, value)`` triples, ANDed. Empty means all
            rows.
        path: Optional ledger path; defaults to the standard location.

    Raises:
        ValueError: on an unknown column or operator. Loudly, because a silently
            dropped filter would return MORE rows than asked for, and callers
            aggregate what they get.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for column, op, value in filters:
        if column not in _COLUMN_SET:
            raise ValueError(
                f"unknown ledger column {column!r} — filters may only name "
                f"columns in _COLUMNS, not arbitrary SQL"
            )
        if op not in _ALLOWED_OPS:
            raise ValueError(
                f"unsupported operator {op!r} — allowed: {sorted(_ALLOWED_OPS)}"
            )
        clauses.append(f"{column} {op} ?")
        params.append(value)

    where = " AND ".join(clauses) if clauses else "1=1"

    conn = _connect(path)
    try:
        conn.row_factory = sqlite3.Row
        # RED1-3-05: deterministic order. _aggregate()'s realization-status merge
        # is last-write-wins per route_id, so an unordered scan let identical
        # conflicting rows flip realized_savings_usd between runs. Order by the
        # event timestamp (then the primary key) so "last write wins" means the
        # chronologically-latest event, deterministically.
        cur = conn.execute(
            # nosec B608 — every fragment of `where` is built above from a
            # column checked against _COLUMNS and an operator checked against
            # _ALLOWED_OPS. No caller-supplied text reaches this string.
            f"SELECT {','.join(_COLUMNS)} FROM execution_events WHERE {where} "
            "ORDER BY ts ASC, event_id ASC",
            tuple(params),
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
                                                   # _COUNTS_AS_REALIZED; NULL on a verified_used
                                                   # row is back-compat-treated as door_call)

    # ── Phase 0 (realized-savings ledger: Gaps 1/2/3) ────────────────────────
    classifier_cost_usd_total: float = 0.0        # Σ classifier spend over accepted attempts
    failed_attempt_cost_usd_total: float = 0.0    # Σ failed-attempt cost carried onto accepted rows
    hook_overhead_usd: float = 0.0                # Σ hook token cost; $0 unless row.host_mode=="metered"
                                                    # (marginal-$0 rule on subscription, cost.py:2803)
    net_realized_savings_usd: float = 0.0         # realized_savings_usd − classifier − failed − hook_overhead
    overhead_as_pct_of_gross: float = 0.0         # overhead / realized_savings_usd; 0.0 if gross is ~0
    # Phase 0.1: "Claude tokens NOT consumed" — Σ (input+output) tokens actually
    # served by a NON-Claude model, on routes that are realized (verified_used +
    # adoption in _COUNTS_AS_REALIZED) AND whose FINAL model is not Claude. This
    # replaced a self-subtracting `baseline_tokens − actual_tokens` formula that
    # was a structural tautology (baseline_tokens was written as the SAME
    # accepted attempt's own token count, so the delta was always 0) — see
    # `_aggregate`'s Gap 2 block and `_CLAUDE_PROVIDERS` above.
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
    except Exception as exc:
        # Falling back to (0.0, 0.0) is the conservative default -- but it makes
        # every baseline read as free, so savings compute as zero and the ledger
        # reports a quiet, plausible nothing. Counted so that "savings collapsed
        # to 0" has a cause an operator can find.
        from llm_router import failopen
        failopen.record("CHZ-FO-LEDGER-HOST-RATES", exc)
        return 0.0, 0.0


def _aggregate(scope: str, scope_id: str, rows: list[dict[str, Any]]) -> Accounting:
    acc = Accounting(scope=scope, scope_id=scope_id)
    # Gate 18: potential saving is per-ROUTE (baseline_eq − actual), and it's only
    # REALIZED when that route's realization is verified_used. Accumulate per route
    # so a single unknown/overridden route can't inflate the realized total.
    route_potential: dict[str, float] = {}       # route_id → Σ(baseline_eq − actual)
    route_realization: dict[str, str] = {}       # route_id → last realization_status seen
    # Phase 0 (Gaps 1/2/3): adoption_method is written on the SAME row that carries
    # realization_status (a separate `route_realized` event, not the original
    # attempt_completed row — see enforce-route.py::_record_realization_used), so
    # it's captured alongside route_realization below, not in the billable block.
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
        if et in _BILLABLE_EVENTS:
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
            # feeds the Phase 0.1 quota derivation below (served-off-Claude
            # tokens); `route_baseline_tokens` is retained for back-compat /
            # debugging only and is no longer part of that derivation.
            itok = r.get("input_tokens") or 0
            otok = r.get("output_tokens") or 0
            if itok or otok:
                route_actual_tokens[rid] = route_actual_tokens.get(rid, 0) + int(itok) + int(otok)
            btok = r.get("baseline_tokens")
            if btok is not None:
                route_baseline_tokens[rid] = route_baseline_tokens.get(rid, 0) + int(btok)
            # Phase 0.1: track the provider of the LAST billable row for this
            # route (rows are loaded ts ASC — see `_load_rows`), so we know
            # which model actually served the route's final/accepted attempt.
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
        # hook_overhead_usd: marginal-$0 rule (cost.py:2803) — only a CONFIRMED
        # metered row prices the hook tokens; subscription/unknown never fabricate
        # a cost. Row-level host_mode, not the global cost.py env check, since the
        # ledger already carries host_mode per-event and results must be
        # bucketed by it.
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
    # Phase 0 adoption gating: within verified_used routes, realized_savings_usd
    # additionally requires adoption_method in _COUNTS_AS_REALIZED (door_call or
    # agent_marked). A verified_used row with NO adoption_method (NULL) predates
    # Phase 0 and is back-compat-treated as door_call — the strongest signal —
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
        if adoption in _COUNTS_AS_REALIZED:
            acc.realized_savings_usd += saving
            acc.realized_savings_by_host_mode[host_mode] = (
                acc.realized_savings_by_host_mode.get(host_mode, 0.0) + saving
            )
            acc.realized_by_adoption_method[adoption] = (
                acc.realized_by_adoption_method.get(adoption, 0.0) + saving
            )
            # Gap 2 (Phase 0.1 reframe): quota-tokens-saved = "Claude tokens NOT
            # consumed" on routes that count as realized. Every token actually
            # served by a NON-Claude model on an adopted route is a Claude/quota
            # token that would otherwise have been spent; if the route's FINAL
            # model IS Claude (e.g. escalated to frontier), Claude ran, so the
            # route saved 0 quota. This replaces the old self-subtracting
            # `baseline_tokens − actual_tokens` formula, which was a structural
            # tautology (baseline_tokens was written as the SAME accepted
            # attempt's own token count, so the delta was always 0 by
            # construction) — see `_CLAUDE_PROVIDERS` above.
            # Never fabricate: an UNKNOWN provider (no attempt row carried one —
            # e.g. a pre-migration route) does NOT count as "non-Claude"; it
            # counts as 0 quota saved rather than assuming a saving we can't
            # actually verify.
            final_provider = route_final_provider.get(rid, "")
            quota = (
                route_actual_tokens.get(rid, 0)
                if final_provider and final_provider.lower() not in _CLAUDE_PROVIDERS
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
                      _load_rows([("route_id", "=", route_id)], path))


def get_turn_accounting(turn_id: str, *, path: Path | None = None) -> Accounting:
    return _aggregate("turn", turn_id, _load_rows([("turn_id", "=", turn_id)], path))


def get_session_accounting(session_id: str, *, path: Path | None = None) -> Accounting:
    return _aggregate("session", session_id,
                      _load_rows([("session_id", "=", session_id)], path))


def get_period_accounting(
    start_ts: float, end_ts: float, *, path: Path | None = None
) -> Accounting:
    return _aggregate(
        "period", f"{start_ts:.0f}-{end_ts:.0f}",
        _load_rows([("ts", ">=", start_ts), ("ts", "<", end_ts)], path),
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
