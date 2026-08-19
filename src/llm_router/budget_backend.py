"""T2-L1: distributed-safe budget backend.

Defines the :class:`BudgetBackend` Protocol — a duck-typed contract that
the existing in-process :class:`~llm_router.budget_envelope.BudgetEnvelopeManager`
already satisfies — and a persistent :class:`SqliteBudgetBackend` that
honours the same contract with cross-process atomicity via SQLite
``BEGIN IMMEDIATE`` transactions.

Why a separate module
---------------------
``budget_envelope.py`` ships an in-process, asyncio-locked manager whose
docstring explicitly says ``"T2-L1 will replace this with a
distributed-safe backend; the in-process correctness is the shape T2-L1
has to honour."`` This file is that replacement. It is additive only —
nothing in the existing module changes — so callers can migrate one site
at a time via :func:`get_budget_backend`.

Phase 3a vs Phase 3b
--------------------
* **Phase 3a (this PR)**: single SQLite file at
  ``~/.llm-router/budgets.db``. Multiple OS processes coordinate via
  SQLite's file lock (``BEGIN IMMEDIATE``). Multiple coroutines in one
  event loop coordinate via an additional ``asyncio.Lock`` so the
  ordering is FIFO inside the event loop too.
* **Phase 3b (T2-XL1)**: multi-instance coordination via a shared
  backend (Postgres / Redis). Same :class:`BudgetBackend` Protocol;
  drop-in replacement.

Atomicity contract
------------------
For the G-002 acceptance criterion — *100 concurrent calls against
budget N → exactly N succeed* — every ``try_reserve`` /
``release`` / ``commit`` runs inside a single
``BEGIN IMMEDIATE … COMMIT`` round-trip. The transaction takes the
RESERVED lock for the duration of the read-modify-write, so any
contending writer either waits or gets ``SQLITE_BUSY`` (we retry).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from llm_router.budget_envelope import BudgetEnvelope, BudgetEnvelopeManager
from llm_router.budget_key import BudgetKey
from llm_router.logging import get_logger
from llm_router.profile import is_enterprise

log = get_logger("llm_router.budget_backend")

# Absolute tolerance for the hard-cap comparison. Budgets and costs are
# stored as SQLite REAL (IEEE 754 double), so accumulating many small
# reservations drifts: 50 × 0.001 lands at 0.05000000000000004 > 0.05 and
# wrongly rejects the last valid slot (CHZ-AUD-009). 1e-9 is far below any
# realistic USD granularity (a nano-dollar) yet larger than the double
# rounding error at these magnitudes.
_CAP_EPSILON = 1e-9


# ── Protocol ───────────────────────────────────────────────────────────────


@runtime_checkable
class BudgetBackend(Protocol):
    """Abstract budget storage + reservation contract.

    The existing :class:`~llm_router.budget_envelope.BudgetEnvelopeManager`
    satisfies this Protocol by duck typing. Any new backend
    (:class:`SqliteBudgetBackend`, future Postgres backend, future Redis
    backend) needs only to expose the same method names with the same
    behavioural contract.

    Threading / async contract
    --------------------------
    ``register`` / ``get`` / ``consumed`` / ``pending`` / ``remaining`` /
    ``tier_state`` are synchronous snapshot reads. ``try_reserve`` /
    ``release`` / ``commit`` are async because backends may need to
    coordinate via a lock (in-process) or a transaction (SQL).
    """

    def register(
        self,
        key: BudgetKey,
        cap_usd: float,
        *,
        parents: tuple[BudgetKey, ...] = (),
        soft_cap_usd: float | None = None,
    ) -> BudgetEnvelope: ...

    def get(self, key: BudgetKey) -> BudgetEnvelope | None: ...

    def consumed(self, key: BudgetKey) -> float: ...

    def pending(self, key: BudgetKey) -> float: ...

    def remaining(self, key: BudgetKey) -> float: ...

    async def try_reserve(self, key: BudgetKey, cost_usd: float) -> bool: ...

    async def release(self, key: BudgetKey, cost_usd: float) -> None: ...

    async def commit(
        self, key: BudgetKey, cost_usd: float, *, settle_pending: bool = True
    ) -> None: ...

    async def settle(
        self, key: BudgetKey, est_cost_usd: float, actual_cost_usd: float
    ) -> None: ...

    def tier_state(self, key: BudgetKey) -> dict[str, float | bool | None]: ...


# ── T2-L2 forecast tier ────────────────────────────────────────────────────


class ForecastedBudgetBreach(Exception):
    """Raised when the forecast tier refuses a reservation because the
    rolling burn rate projects the envelope hitting its cap inside the
    configured horizon — even though the immediate try_reserve would
    succeed against the hard cap.

    Carries diagnostic fields so the caller can surface why the call
    was refused without the operator having to read structured logs.
    """

    def __init__(
        self,
        message: str,
        *,
        key: "BudgetKey",
        burn_rate_usd_per_sec: float,
        seconds_to_breach: float,
        horizon_seconds: float,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.burn_rate_usd_per_sec = burn_rate_usd_per_sec
        self.seconds_to_breach = seconds_to_breach
        self.horizon_seconds = horizon_seconds


_FORECAST_MODES = frozenset({"off", "warn", "strict"})
_FORECAST_MODE_DEFAULT = "off"  # opt-in (matches the session-established three-mode pattern)
_FORECAST_WINDOW_DEFAULT_SEC = 60.0
_FORECAST_HORIZON_DEFAULT_SEC = 300.0


def _forecast_mode() -> str:
    """Read ``LLM_ROUTER_BUDGET_FORECAST_MODE`` env, normalise, default off.

    Invalid values fall back to ``off`` (fail-open). Parity with the
    policy / classification three-mode gate established this session.
    """
    normalised = os.environ.get("LLM_ROUTER_BUDGET_FORECAST_MODE", "").strip().lower()
    if not normalised:
        # G-016: unset/blank → profile default. Enterprise flips the
        # forecast tier strict-on; developer keeps the opt-in default off.
        return "strict" if is_enterprise() else _FORECAST_MODE_DEFAULT
    if normalised not in _FORECAST_MODES:
        return _FORECAST_MODE_DEFAULT
    return normalised


def _forecast_window_seconds() -> float:
    try:
        return float(
            os.environ.get("LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS")
            or _FORECAST_WINDOW_DEFAULT_SEC
        )
    except ValueError:
        return _FORECAST_WINDOW_DEFAULT_SEC


def _forecast_horizon_seconds() -> float:
    try:
        return float(
            os.environ.get("LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS")
            or _FORECAST_HORIZON_DEFAULT_SEC
        )
    except ValueError:
        return _FORECAST_HORIZON_DEFAULT_SEC


# ── SQLite backend ─────────────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
    key_blob TEXT PRIMARY KEY,
    cap_usd REAL NOT NULL,
    soft_cap_usd REAL,
    parents_json TEXT NOT NULL DEFAULT '[]',
    consumed_usd REAL NOT NULL DEFAULT 0.0,
    pending_usd REAL NOT NULL DEFAULT 0.0,
    soft_breached INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budget_spend_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_blob TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    committed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_events_key_time
    ON budget_spend_events(key_blob, committed_at DESC);
"""

_BUSY_TIMEOUT_MS = 5000  # 5s — generous enough for contention, short enough to surface deadlocks


def _serialise_key(key: BudgetKey) -> str:
    """Canonical string form of a BudgetKey for use as a SQLite PK.

    JSON-array shape preserves ``None``/``null`` distinguishably from
    string ``"None"`` and is trivially reversible if needed. Field
    order matches the dataclass ordering.
    """
    return json.dumps(
        [key.tenant_id, key.org_id, key.user_id, key.agent_id, key.scope]
    )


@dataclass
class _EnvelopeRow:
    cap_usd: float
    soft_cap_usd: float | None
    parents: tuple[BudgetKey, ...]
    consumed_usd: float
    pending_usd: float
    soft_breached: bool


class SqliteBudgetBackend:
    """Persistent budget backend with cross-process atomic
    check-then-charge.

    Single SQLite file. Each mutating call runs inside
    ``BEGIN IMMEDIATE`` so writers serialise on the OS-level file lock.
    Inside one event loop, an additional ``asyncio.Lock`` gives
    coroutines FIFO ordering — without it, two concurrent coroutines
    could both try to BEGIN IMMEDIATE and one would hit
    ``SQLITE_BUSY`` and retry, wasting work.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or Path(
            os.environ.get("LLM_ROUTER_BUDGETS_DB_PATH")
            or (Path.home() / ".llm-router" / "budgets.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts us in autocommit / manual-transaction
        # mode so BEGIN IMMEDIATE is honoured exactly. With the default
        # ("deferred"), Python's sqlite3 wrapper may issue its own BEGIN
        # before ours.
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        # In-process coroutine fairness layer. See class docstring.
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    # ── Read helpers ──────────────────────────────────────────────────────

    def _load(self, key: BudgetKey) -> _EnvelopeRow | None:
        cur = self._conn.execute(
            "SELECT cap_usd, soft_cap_usd, parents_json, consumed_usd, "
            "pending_usd, soft_breached FROM envelopes WHERE key_blob = ?",
            (_serialise_key(key),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        parents_raw = json.loads(row[2])
        parents = tuple(
            BudgetKey(*p) if isinstance(p, list) else BudgetKey(**p)
            for p in parents_raw
        )
        return _EnvelopeRow(
            cap_usd=row[0],
            soft_cap_usd=row[1],
            parents=parents,
            consumed_usd=row[3],
            pending_usd=row[4],
            soft_breached=bool(row[5]),
        )

    def get(self, key: BudgetKey) -> BudgetEnvelope | None:
        row = self._load(key)
        if row is None:
            return None
        return BudgetEnvelope(
            key=key,
            cap_usd=row.cap_usd,
            parents=row.parents,
            soft_cap_usd=row.soft_cap_usd,
        )

    def iter_lineage_consumed(self) -> list[tuple[str, float, tuple[str, ...]]]:
        """``(key_blob, consumed_usd, parent_key_blobs)`` for every registered
        envelope — the minimal projection budget-lineage reconciliation (#70)
        needs to verify subtree spend conservation across the parent chain.

        Parent keys are re-serialised to blobs so a child's ``parents`` line up
        with the enumerated parents' own ``key_blob`` without deserialising
        either side back to a ``BudgetKey``.
        """
        cur = self._conn.execute(
            "SELECT key_blob, consumed_usd, parents_json FROM envelopes"
        )
        out: list[tuple[str, float, tuple[str, ...]]] = []
        for key_blob, consumed, parents_json in cur.fetchall():
            parents_raw = json.loads(parents_json)
            parent_blobs = tuple(
                _serialise_key(BudgetKey(*p) if isinstance(p, list) else BudgetKey(**p))
                for p in parents_raw
            )
            out.append((key_blob, float(consumed), parent_blobs))
        return out

    def consumed(self, key: BudgetKey) -> float:
        row = self._load(key)
        return row.consumed_usd if row else 0.0

    def pending(self, key: BudgetKey) -> float:
        row = self._load(key)
        return row.pending_usd if row else 0.0

    def remaining(self, key: BudgetKey) -> float:
        row = self._load(key)
        if row is None:
            return float("inf")
        return max(0.0, row.cap_usd - row.consumed_usd - row.pending_usd)

    # ── Registration ──────────────────────────────────────────────────────

    def register(
        self,
        key: BudgetKey,
        cap_usd: float,
        *,
        parents: tuple[BudgetKey, ...] = (),
        soft_cap_usd: float | None = None,
    ) -> BudgetEnvelope:
        if cap_usd <= 0:
            raise ValueError(f"cap_usd must be positive, got {cap_usd!r}")
        if soft_cap_usd is not None:
            if soft_cap_usd <= 0:
                raise ValueError(
                    f"soft_cap_usd must be positive, got {soft_cap_usd!r}"
                )
            if soft_cap_usd >= cap_usd:
                raise ValueError(
                    f"soft_cap_usd ({soft_cap_usd}) must be strictly less "
                    f"than cap_usd ({cap_usd})"
                )
        parents_json = json.dumps(
            [
                [p.tenant_id, p.org_id, p.user_id, p.agent_id, p.scope]
                for p in parents
            ]
        )
        # UPSERT preserving accumulated consumed/pending. A re-register
        # with a higher cap must not drop spend history.
        self._conn.execute(
            "INSERT INTO envelopes "
            "(key_blob, cap_usd, soft_cap_usd, parents_json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key_blob) DO UPDATE SET "
            "cap_usd=excluded.cap_usd, "
            "soft_cap_usd=excluded.soft_cap_usd, "
            "parents_json=excluded.parents_json",
            (_serialise_key(key), float(cap_usd), soft_cap_usd, parents_json),
        )
        return BudgetEnvelope(
            key=key,
            cap_usd=float(cap_usd),
            parents=tuple(parents),
            soft_cap_usd=(
                float(soft_cap_usd) if soft_cap_usd is not None else None
            ),
        )

    # ── Mutations under BEGIN IMMEDIATE ───────────────────────────────────

    def _chain_rows(self, key: BudgetKey) -> list[tuple[BudgetKey, _EnvelopeRow]]:
        """Return [(key, row), (parent_key, parent_row), ...] for every
        registered envelope in the chain. Unregistered parents are
        silently skipped — parity with the in-process manager.

        RED1-6-01: walks the parent chain TRANSITIVELY (each registered row's
        own ``parents`` are followed in turn, breadth-first with a cycle guard),
        not just one hop. A cap registered 2+ levels above a reservation key
        (e.g. org → user → agent, each pointing only at its immediate parent —
        the shape ``BudgetKey.rolls_up_to`` builds) previously never saw the
        spend and was silently unenforceable."""
        out: list[tuple[BudgetKey, _EnvelopeRow]] = []
        seen: set = set()
        queue: list[BudgetKey] = [key]
        while queue:
            k = queue.pop(0)
            sk = _serialise_key(k)
            if sk in seen:
                continue
            seen.add(sk)
            row = self._load(k)
            if row is None:
                continue
            out.append((k, row))
            for parent_key in row.parents:
                if _serialise_key(parent_key) not in seen:
                    queue.append(parent_key)
        return out

    async def try_reserve(self, key: BudgetKey, cost_usd: float) -> bool:
        if cost_usd <= 0:
            return True
        async with self._lock:
            return await asyncio.to_thread(self._try_reserve_sync, key, cost_usd)

    def _try_reserve_sync(self, key: BudgetKey, cost_usd: float) -> bool:
        self._begin_immediate_with_retry()
        try:
            chain = self._chain_rows(key)
            if not chain:
                self._conn.execute("COMMIT")
                return True
            for _, row in chain:
                projected = row.consumed_usd + row.pending_usd + cost_usd
                if projected > row.cap_usd + _CAP_EPSILON:
                    self._conn.execute("COMMIT")
                    return False
            # T2-L2 forecast gate. Applied AFTER the hard-cap check so the
            # forecast tier is a strictly additive refusal — it never
            # accepts a reservation the hard cap would reject. Runs
            # inside the transaction so the burn-rate read is consistent
            # with the chain state we just inspected.
            forecast_mode = _forecast_mode()
            forecast_breach: ForecastedBudgetBreach | None = None
            if forecast_mode != "off":
                forecast_breach = self._check_forecast_inside_tx(
                    chain, cost_usd
                )
            # CHZ-AUD-010: in strict mode a forecast breach must reject the
            # reservation WITHOUT leaving pending behind. Roll back before the
            # pending UPDATE so no orphaned reservation is committed, then
            # raise. (Previously the UPDATE + COMMIT ran first and the raise
            # landed after commit, permanently accumulating pending_usd.)
            if forecast_breach is not None and forecast_mode == "strict":
                self._conn.execute("ROLLBACK")
                raise forecast_breach
            for env_key, _ in chain:
                self._conn.execute(
                    "UPDATE envelopes SET pending_usd = pending_usd + ? "
                    "WHERE key_blob = ?",
                    (cost_usd, _serialise_key(env_key)),
                )
            self._conn.execute("COMMIT")
            # Soft-tier flip happens outside the transaction (no contention
            # risk for a state flag) so the alerting log line lands after
            # the commit is durable.
            for env_key, _ in chain:
                self._maybe_flip_soft_state(env_key)
            # Forecast handling AFTER commit: warn mode logs and proceeds
            # with the reservation committed above so callers see consistent
            # accounting. Strict mode already rolled back and raised before
            # the pending UPDATE (see CHZ-AUD-010 above), so only warn
            # reaches here.
            if forecast_breach is not None:
                log.warning(
                    "budget_forecast_warn",
                    key=str(forecast_breach.key),
                    burn_rate_usd_per_sec=forecast_breach.burn_rate_usd_per_sec,
                    seconds_to_breach=forecast_breach.seconds_to_breach,
                    horizon_seconds=forecast_breach.horizon_seconds,
                )
            return True
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # No active transaction: either the error surfaced after
                # COMMIT, or the strict-forecast path already rolled back
                # before raising (CHZ-AUD-010). Nothing left to undo.
                pass
            raise

    async def release(self, key: BudgetKey, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        async with self._lock:
            await asyncio.to_thread(self._release_sync, key, cost_usd)

    def _release_sync(self, key: BudgetKey, cost_usd: float) -> None:
        self._begin_immediate_with_retry()
        try:
            chain = self._chain_rows(key)
            for env_key, _ in chain:
                self._conn.execute(
                    "UPDATE envelopes SET pending_usd = max(0.0, pending_usd - ?) "
                    "WHERE key_blob = ?",
                    (cost_usd, _serialise_key(env_key)),
                )
            self._conn.execute("COMMIT")
            for env_key, _ in chain:
                self._maybe_flip_soft_state(env_key)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    async def commit(
        self, key: BudgetKey, cost_usd: float, *, settle_pending: bool = True
    ) -> None:
        if cost_usd <= 0:
            return
        async with self._lock:
            await asyncio.to_thread(self._commit_sync, key, cost_usd, settle_pending)

    def _commit_sync(
        self, key: BudgetKey, cost_usd: float, settle_pending: bool = True
    ) -> None:
        # RED1-5-01: when settle_pending is False the caller (commit_envelope)
        # has ALREADY released the reservation from pending_usd via release(est).
        # Decrementing pending_usd a second time here erased a concurrent
        # sibling's still-outstanding reservation on a shared key, letting a
        # third caller be admitted past the hard cap. With settle_pending=False
        # commit becomes a pure "record consumed" ledger write.
        self._begin_immediate_with_retry()
        try:
            chain = self._chain_rows(key)
            now = time.time()
            _pending_sql = (
                ", pending_usd = max(0.0, pending_usd - ?)" if settle_pending else ""
            )
            _params = (
                (cost_usd, cost_usd) if settle_pending else (cost_usd,)
            )
            for env_key, _ in chain:
                self._conn.execute(
                    "UPDATE envelopes SET "
                    "consumed_usd = consumed_usd + ?"
                    + _pending_sql
                    + " WHERE key_blob = ?",
                    (*_params, _serialise_key(env_key)),
                )
                # T2-L2: record spend event under the same transaction so
                # the burn-rate query observes consistent committed totals.
                # Only the directly-committed key emits an event; parent
                # envelopes' burn rate is derived by summing their own
                # committed events from descendants when needed (deferred).
                if env_key == key:
                    self._conn.execute(
                        "INSERT INTO budget_spend_events "
                        "(key_blob, amount_usd, committed_at) "
                        "VALUES (?, ?, ?)",
                        (_serialise_key(env_key), cost_usd, now),
                    )
            self._conn.execute("COMMIT")
            for env_key, _ in chain:
                self._maybe_flip_soft_state(env_key)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    async def settle(
        self, key: BudgetKey, est_cost_usd: float, actual_cost_usd: float
    ) -> None:
        """RED1-7-01: atomically undo the reservation (pending -= est) AND record
        the real spend (consumed += actual) in ONE lock-hold / ONE transaction.

        commit_envelope previously did release(est) then commit(actual) as two
        separate awaited, separately-transacted calls; a concurrent try_reserve
        landing in the gap saw pending already decremented but consumed not yet
        incremented, and could be admitted past a shared cap. Doing both in a
        single BEGIN IMMEDIATE removes that window."""
        if est_cost_usd <= 0 and actual_cost_usd <= 0:
            return
        async with self._lock:
            await asyncio.to_thread(
                self._settle_sync, key, float(est_cost_usd or 0.0), float(actual_cost_usd or 0.0)
            )

    def _settle_sync(self, key: BudgetKey, est: float, actual: float) -> None:
        self._begin_immediate_with_retry()
        try:
            chain = self._chain_rows(key)
            now = time.time()
            for env_key, _ in chain:
                self._conn.execute(
                    "UPDATE envelopes SET "
                    "consumed_usd = consumed_usd + ?, "
                    "pending_usd = max(0.0, pending_usd - ?) "
                    "WHERE key_blob = ?",
                    (actual, est, _serialise_key(env_key)),
                )
                if env_key == key and actual > 0:
                    self._conn.execute(
                        "INSERT INTO budget_spend_events "
                        "(key_blob, amount_usd, committed_at) "
                        "VALUES (?, ?, ?)",
                        (_serialise_key(env_key), actual, now),
                    )
            self._conn.execute("COMMIT")
            for env_key, _ in chain:
                self._maybe_flip_soft_state(env_key)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _begin_immediate_with_retry(self) -> None:
        """Begin an immediate-mode transaction, retrying briefly on
        SQLITE_BUSY. The PRAGMA busy_timeout above should usually
        absorb contention, but a defensive retry catches the case where
        another writer holds RESERVED across the timeout window."""
        deadline = time.monotonic() + (_BUSY_TIMEOUT_MS / 1000)
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as err:
                if "busy" in str(err).lower() and time.monotonic() < deadline:
                    time.sleep(0.005)
                    continue
                raise

    # ── T2-L2: forecast + spend-event helpers ─────────────────────────────

    def get_burn_rate_usd_per_second(
        self, key: BudgetKey, window_seconds: float
    ) -> float:
        """Rolling burn rate for ``key``: sum of spend events in the
        last ``window_seconds``, divided by the window.

        Synchronous snapshot. Returns ``0.0`` when no events fall
        inside the window — a fresh envelope or a long-idle one both
        produce a benign zero.
        """
        if window_seconds <= 0:
            return 0.0
        since = time.time() - window_seconds
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0.0) FROM budget_spend_events "
            "WHERE key_blob = ? AND committed_at >= ?",
            (_serialise_key(key), since),
        )
        total = float(cur.fetchone()[0])
        return total / window_seconds

    def _check_forecast_inside_tx(
        self,
        chain: list[tuple[BudgetKey, _EnvelopeRow]],
        cost_usd: float,
    ) -> "ForecastedBudgetBreach | None":
        """Inspect the leaf envelope's burn rate. If the projected
        time-to-breach is inside the configured horizon, return a
        :class:`ForecastedBudgetBreach` describing why; otherwise None.

        Only the leaf key is forecast-checked in the MVP — parent-chain
        burn-rate aggregation is deferred (parents are already protected
        by their own hard caps, and the leaf's projection is the
        most-actionable signal in practice).
        """
        window = _forecast_window_seconds()
        horizon = _forecast_horizon_seconds()
        leaf_key, leaf_row = chain[0]
        burn_rate = self.get_burn_rate_usd_per_second(leaf_key, window)
        if burn_rate <= 0:
            return None
        remaining = leaf_row.cap_usd - leaf_row.consumed_usd - leaf_row.pending_usd
        # The hard-cap check already accepted the cost, so the new
        # pending will be (pending + cost_usd); future remaining is
        # (remaining - cost_usd) — that's what the burn rate will run
        # down. We're optimistic about the immediate accommodation;
        # the question is the trajectory after this call lands.
        future_remaining = max(0.0, remaining - cost_usd)
        seconds_to_breach = future_remaining / burn_rate
        if seconds_to_breach >= horizon:
            return None
        return ForecastedBudgetBreach(
            f"Forecasted budget breach in {seconds_to_breach:.0f}s "
            f"at burn rate ${burn_rate:.4f}/s (horizon {horizon:.0f}s).",
            key=leaf_key,
            burn_rate_usd_per_sec=burn_rate,
            seconds_to_breach=seconds_to_breach,
            horizon_seconds=horizon,
        )

    def _record_spend_event_for_tests(
        self, key: BudgetKey, amount_usd: float, committed_at: float
    ) -> None:
        """Inject a spend event directly. Tests use this to set up burn-rate
        scenarios without spinning wall-clock. Production code never
        calls this — use ``commit`` to record real spend."""
        self._conn.execute(
            "INSERT INTO budget_spend_events "
            "(key_blob, amount_usd, committed_at) VALUES (?, ?, ?)",
            (_serialise_key(key), float(amount_usd), float(committed_at)),
        )

    # ── Soft tier ─────────────────────────────────────────────────────────

    def _maybe_flip_soft_state(self, key: BudgetKey) -> None:
        row = self._load(key)
        if row is None or row.soft_cap_usd is None:
            return
        total = row.consumed_usd + row.pending_usd
        is_breached = total >= row.soft_cap_usd
        if is_breached == row.soft_breached:
            return
        self._conn.execute(
            "UPDATE envelopes SET soft_breached = ? WHERE key_blob = ?",
            (1 if is_breached else 0, _serialise_key(key)),
        )
        if is_breached:
            log.warning(
                "budget_soft_cap_breached",
                key=str(key),
                soft_cap_usd=row.soft_cap_usd,
                cap_usd=row.cap_usd,
                consumed_usd=row.consumed_usd,
                pending_usd=row.pending_usd,
            )

    def tier_state(self, key: BudgetKey) -> dict[str, float | bool | None]:
        row = self._load(key)
        if row is None:
            return {
                "cap_usd": None,
                "soft_cap_usd": None,
                "consumed_usd": 0.0,
                "pending_usd": 0.0,
                "remaining_usd": float("inf"),
                "usage_pct": None,
                "soft_breached": False,
            }
        return {
            "cap_usd": row.cap_usd,
            "soft_cap_usd": row.soft_cap_usd,
            "consumed_usd": row.consumed_usd,
            "pending_usd": row.pending_usd,
            "remaining_usd": max(
                0.0, row.cap_usd - row.consumed_usd - row.pending_usd
            ),
            "usage_pct": (row.consumed_usd + row.pending_usd) / row.cap_usd,
            "soft_breached": row.soft_breached,
        }


# ── Factory ───────────────────────────────────────────────────────────────


_BACKEND_KIND_SQLITE = "sqlite"
_BACKEND_KIND_MEMORY = "memory"
_BACKEND_KIND_POSTGRES = "postgres"
_BACKEND_KIND_DEFAULT = _BACKEND_KIND_SQLITE
_KNOWN_BACKENDS = {
    _BACKEND_KIND_SQLITE,
    _BACKEND_KIND_MEMORY,
    _BACKEND_KIND_POSTGRES,
}

_backend: BudgetBackend | None = None


def get_budget_backend() -> BudgetBackend:
    """Return the module-level budget backend singleton.

    Selection: ``LLM_ROUTER_BUDGET_BACKEND`` env var, one of:

    * ``sqlite`` (default) — persistent, single-instance cross-process-safe
      via SQLite ``BEGIN IMMEDIATE``.
    * ``memory`` — in-process :class:`BudgetEnvelopeManager`, useful for
      tests and ephemeral deployments.
    * ``postgres`` (T2-XL1, Phase 3b) — multi-instance coordination via a
      shared Postgres database. Requires the ``postgres`` extra and
      ``LLM_ROUTER_BUDGET_POSTGRES_DSN`` to be set. Falls back to ``sqlite``
      if the dep / DSN is missing (fail-open posture).

    Invalid values fall back to the safer ``sqlite`` default — a
    misconfigured env var must never break boot.
    """
    global _backend
    if _backend is not None:
        return _backend
    raw = os.environ.get("LLM_ROUTER_BUDGET_BACKEND", "").strip().lower()
    kind = raw if raw in _KNOWN_BACKENDS else _BACKEND_KIND_DEFAULT
    if kind == _BACKEND_KIND_MEMORY:
        _backend = BudgetEnvelopeManager()
    elif kind == _BACKEND_KIND_POSTGRES:
        try:
            from llm_router.budget_backend_postgres import PostgresBudgetBackend
            _backend = PostgresBudgetBackend()
            # RED1-8-04: the Postgres backend has no T2-L2 forecast (burn-rate)
            # tier. When the deployment expects "strict" forecast enforcement
            # (the enterprise default), selecting Postgres silently disables that
            # pre-emptive throttle — the hard cap still applies, but the early
            # burn-rate refusal does not. Warn + alert so operators are not
            # unknowingly missing a safety tier they believe is on.
            if _forecast_mode() == "strict":
                log.warning(
                    "postgres_backend_no_forecast_tier",
                    detail="LLM_ROUTER_BUDGET_BACKEND=postgres has no burn-rate "
                    "forecast tier; strict-forecast enforcement is inactive on "
                    "this backend (hard cap still enforced). Set "
                    "LLM_ROUTER_BUDGET_FORECAST_MODE=off to acknowledge, or use the "
                    "sqlite backend for forecast enforcement.",
                )
                try:
                    from llm_router.alerts import BUDGET_POSTGRES_FALLBACK, emit_alert
                    emit_alert(
                        BUDGET_POSTGRES_FALLBACK,
                        detail={"reason": "postgres_no_forecast_tier"},
                    )
                except Exception:
                    pass
        except (ImportError, RuntimeError) as err:
            # Fail-open: a missing dep or DSN must not break boot.
            # Operators see the warning and can fix; routing continues
            # against the local SQLite backend in the meantime.
            log.warning(
                "postgres_backend_unavailable_fallback_sqlite",
                error=str(err),
            )
            # Active alert: a silent fallback to per-instance SQLite risks
            # aggregate overspend across a cluster — page, don't just log.
            from llm_router.alerts import BUDGET_POSTGRES_FALLBACK, emit_alert
            emit_alert(BUDGET_POSTGRES_FALLBACK, detail={"error": str(err)})
            _backend = SqliteBudgetBackend()
    else:
        _backend = SqliteBudgetBackend()
    return _backend


def reset_budget_backend_for_tests() -> None:
    """Drop the singleton so the next ``get_budget_backend`` starts
    fresh. Production code never calls this."""
    global _backend
    if _backend is not None and hasattr(_backend, "close"):
        try:
            _backend.close()
        except Exception:
            pass
    _backend = None


__all__ = [
    "BudgetBackend",
    "SqliteBudgetBackend",
    "get_budget_backend",
    "reset_budget_backend_for_tests",
]
