"""T2-XL1: multi-instance budget coordination via Postgres.

.. warning::

   **EXPERIMENTAL (P0-3 scope decision).** Budget-envelope enforcement is wired
   into the routing path and CI-proven against the **SQLite** backend
   (single-instance, cross-process-safe). This Postgres backend's
   *cross-instance* coordination is **not yet covered by a real-Postgres CI
   test** — the atomicity contract below is verified only against a mock. Do not
   rely on multi-instance envelope enforcement in production until a real
   Postgres integration test lands (Phase D). ``get_budget_backend`` fails open
   to SQLite when the ``postgres`` extra or DSN is missing.

Ships :class:`PostgresBudgetBackend`, a drop-in implementation of the
:class:`~llm_router.budget_backend.BudgetBackend` Protocol that coordinates
budget reservations across N llm_router daemon processes (potentially on
different hosts) sharing one Postgres database.

Atomicity contract
------------------
For the G-002 cross-instance acceptance criterion — *100 concurrent
``try_reserve`` calls across 4 processes against a $5 cap with $0.10
each → exactly 50 succeed in total* — every ``try_reserve`` runs as a
single ``UPDATE`` whose ``WHERE`` clause encodes the cap check:

    UPDATE llm_router_envelopes
       SET pending_usd = pending_usd + :cost
     WHERE key_blob = :key
       AND consumed_usd + pending_usd + :cost <= cap_usd
    RETURNING pending_usd;

If the row's cap would be breached, the predicate fails, no row is
updated, and ``RETURNING`` is empty → reservation refused. The row's
implicit lock during the UPDATE serialises all concurrent writers in
Postgres — no application-level lock or advisory-lock dance is needed
for the single-key case.

Parent-chain atomicity uses one transaction with ``SELECT … FOR UPDATE``
on every envelope in the chain, then a single ``UPDATE`` per envelope,
so the whole chain either all advances or none does.

Optional dep
------------
``psycopg[binary]>=3.2`` is installed via the ``postgres`` extra.
Import is lazy inside ``__init__`` so the bare llm_router install with the
default SQLite backend never imports psycopg.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

from llm_router.budget_envelope import BudgetEnvelope
from llm_router.budget_key import BudgetKey
from llm_router.logging import get_logger

if TYPE_CHECKING:
    import psycopg

log = get_logger("llm_router.budget_backend_postgres")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_router_envelopes (
    key_blob TEXT PRIMARY KEY,
    cap_usd DOUBLE PRECISION NOT NULL,
    soft_cap_usd DOUBLE PRECISION,
    parents_json TEXT NOT NULL DEFAULT '[]',
    consumed_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    pending_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    soft_breached BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_llm_router_envelopes_soft_breached
    ON llm_router_envelopes(soft_breached) WHERE soft_breached;
"""


def _serialise_key(key: BudgetKey) -> str:
    """Same canonical form as the SQLite backend, so the same key
    addresses the same envelope across backends. JSON array preserves
    None distinguishably from string 'None'."""
    return json.dumps(
        [key.tenant_id, key.org_id, key.user_id, key.agent_id, key.scope]
    )


def _deserialise_parents(raw: str) -> tuple[BudgetKey, ...]:
    if not raw:
        return ()
    parsed = json.loads(raw)
    return tuple(
        BudgetKey(*p) if isinstance(p, list) else BudgetKey(**p)
        for p in parsed
    )


def _serialise_parents(parents: tuple[BudgetKey, ...]) -> str:
    return json.dumps(
        [
            [p.tenant_id, p.org_id, p.user_id, p.agent_id, p.scope]
            for p in parents
        ]
    )


class PostgresBudgetBackend:
    """Postgres-backed implementation of the BudgetBackend Protocol.

    Designed for Phase 3b deployments where N llm_router daemons share one
    Postgres database. Same Protocol as
    :class:`~llm_router.budget_backend.SqliteBudgetBackend`; callers can
    swap via ``LLM_ROUTER_BUDGET_BACKEND=postgres`` without code changes.
    """

    def __init__(self, dsn: str | None = None) -> None:
        try:
            import psycopg
        except ImportError as err:  # pragma: no cover - import guarded
            raise RuntimeError(
                "PostgresBudgetBackend requires the 'postgres' extra: "
                "pip install 'llm_router[postgres]'"
            ) from err
        self._psycopg = psycopg
        self._dsn = dsn or os.environ.get("LLM_ROUTER_BUDGET_POSTGRES_DSN", "")
        if not self._dsn:
            raise RuntimeError(
                "PostgresBudgetBackend requires a DSN — set "
                "LLM_ROUTER_BUDGET_POSTGRES_DSN or pass dsn=..."
            )
        # autocommit=False so each method's transaction boundary is
        # explicit. Each public mutation opens its own connection from
        # the pool — for now we keep one persistent connection per
        # backend instance; a real pool can land later if contention
        # bites.
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)
        self._conn.commit()
        # In-process coroutine fairness layer (same role as in the
        # SQLite backend). Cross-process coordination relies on Postgres
        # row locks; the asyncio.Lock just keeps coroutines on one event
        # loop from racing through the same connection.
        self._lock = asyncio.Lock()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Read helpers (sync snapshot) ──────────────────────────────────────

    def _load(self, key: BudgetKey) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cap_usd, soft_cap_usd, parents_json, consumed_usd, "
                "pending_usd, soft_breached "
                "FROM llm_router_envelopes WHERE key_blob = %s",
                (_serialise_key(key),),
            )
            row = cur.fetchone()
        # Reads happen inside an implicit transaction with autocommit=False;
        # roll back so the next read sees freshly-committed cross-process
        # writes instead of a stale snapshot.
        self._conn.rollback()
        if row is None:
            return None
        return {
            "cap_usd": float(row[0]),
            "soft_cap_usd": float(row[1]) if row[1] is not None else None,
            "parents": _deserialise_parents(row[2]),
            "consumed_usd": float(row[3]),
            "pending_usd": float(row[4]),
            "soft_breached": bool(row[5]),
        }

    def get(self, key: BudgetKey) -> BudgetEnvelope | None:
        row = self._load(key)
        if row is None:
            return None
        return BudgetEnvelope(
            key=key,
            cap_usd=row["cap_usd"],
            parents=row["parents"],
            soft_cap_usd=row["soft_cap_usd"],
        )

    def consumed(self, key: BudgetKey) -> float:
        row = self._load(key)
        return row["consumed_usd"] if row else 0.0

    def pending(self, key: BudgetKey) -> float:
        row = self._load(key)
        return row["pending_usd"] if row else 0.0

    def remaining(self, key: BudgetKey) -> float:
        row = self._load(key)
        if row is None:
            return float("inf")
        return max(
            0.0,
            row["cap_usd"] - row["consumed_usd"] - row["pending_usd"],
        )

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
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_router_envelopes "
                "(key_blob, cap_usd, soft_cap_usd, parents_json) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (key_blob) DO UPDATE SET "
                "cap_usd = excluded.cap_usd, "
                "soft_cap_usd = excluded.soft_cap_usd, "
                "parents_json = excluded.parents_json",
                (
                    _serialise_key(key),
                    float(cap_usd),
                    soft_cap_usd,
                    _serialise_parents(tuple(parents)),
                ),
            )
        self._conn.commit()
        return BudgetEnvelope(
            key=key,
            cap_usd=float(cap_usd),
            parents=tuple(parents),
            soft_cap_usd=(
                float(soft_cap_usd) if soft_cap_usd is not None else None
            ),
        )

    # ── Mutations ─────────────────────────────────────────────────────────

    async def try_reserve(self, key: BudgetKey, cost_usd: float) -> bool:
        if cost_usd <= 0:
            return True
        async with self._lock:
            return await asyncio.to_thread(self._try_reserve_sync, key, cost_usd)

    def _try_reserve_sync(self, key: BudgetKey, cost_usd: float) -> bool:
        try:
            with self._conn.cursor() as cur:
                # Step 1: lock & inspect the chain. SELECT FOR UPDATE serialises
                # any other writer touching these rows for the duration of this
                # transaction — the cross-process atomicity backbone.
                chain_keys = self._chain_keys(cur, key)
                if not chain_keys:
                    self._conn.commit()
                    return True
                # Step 2: single UPDATE per envelope with cap-checking WHERE.
                # If any cap would breach, no row is updated for that key, the
                # cursor reports rowcount=0, and we roll back the whole chain.
                for env_key in chain_keys:
                    cur.execute(
                        "UPDATE llm_router_envelopes "
                        "SET pending_usd = pending_usd + %s "
                        "WHERE key_blob = %s "
                        "AND consumed_usd + pending_usd + %s <= cap_usd",
                        (cost_usd, _serialise_key(env_key), cost_usd),
                    )
                    if cur.rowcount != 1:
                        self._conn.rollback()
                        return False
            self._conn.commit()
            # Soft-tier flip happens in its own transaction so a failure to
            # log alert state cannot roll back the successful reservation.
            for env_key in chain_keys:
                self._maybe_flip_soft_state(env_key)
            return True
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise

    async def release(self, key: BudgetKey, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        async with self._lock:
            await asyncio.to_thread(self._release_sync, key, cost_usd)

    def _release_sync(self, key: BudgetKey, cost_usd: float) -> None:
        try:
            with self._conn.cursor() as cur:
                chain_keys = self._chain_keys(cur, key)
                for env_key in chain_keys:
                    cur.execute(
                        "UPDATE llm_router_envelopes "
                        "SET pending_usd = GREATEST(0.0, pending_usd - %s) "
                        "WHERE key_blob = %s",
                        (cost_usd, _serialise_key(env_key)),
                    )
            self._conn.commit()
            for env_key in chain_keys:
                self._maybe_flip_soft_state(env_key)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
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
        # RED1-5-01: see SqliteBudgetBackend — when the reservation was already
        # released via release(est), do not decrement pending_usd again (it would
        # erase a concurrent sibling's outstanding reservation on a shared key).
        try:
            with self._conn.cursor() as cur:
                chain_keys = self._chain_keys(cur, key)
                _pending_sql = (
                    ", pending_usd = GREATEST(0.0, pending_usd - %s)"
                    if settle_pending
                    else ""
                )
                for env_key in chain_keys:
                    _params = (
                        (cost_usd, cost_usd, _serialise_key(env_key))
                        if settle_pending
                        else (cost_usd, _serialise_key(env_key))
                    )
                    cur.execute(
                        "UPDATE llm_router_envelopes "
                        "SET consumed_usd = consumed_usd + %s"
                        + _pending_sql
                        + " WHERE key_blob = %s",
                        _params,
                    )
            self._conn.commit()
            for env_key in chain_keys:
                self._maybe_flip_soft_state(env_key)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise

    async def settle(
        self, key: BudgetKey, est_cost_usd: float, actual_cost_usd: float
    ) -> None:
        """RED1-7-01: atomic pending−=est + consumed+=actual in one transaction."""
        if est_cost_usd <= 0 and actual_cost_usd <= 0:
            return
        async with self._lock:
            await asyncio.to_thread(
                self._settle_sync, key, float(est_cost_usd or 0.0), float(actual_cost_usd or 0.0)
            )

    def _settle_sync(self, key: BudgetKey, est: float, actual: float) -> None:
        try:
            with self._conn.cursor() as cur:
                chain_keys = self._chain_keys(cur, key)
                for env_key in chain_keys:
                    cur.execute(
                        "UPDATE llm_router_envelopes "
                        "SET consumed_usd = consumed_usd + %s, "
                        "pending_usd = GREATEST(0.0, pending_usd - %s) "
                        "WHERE key_blob = %s",
                        (actual, est, _serialise_key(env_key)),
                    )
            self._conn.commit()
            for env_key in chain_keys:
                self._maybe_flip_soft_state(env_key)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise

    def _chain_keys(self, cur: "psycopg.Cursor", key: BudgetKey) -> list[BudgetKey]:
        """Lock the leaf row + every registered ancestor, return their keys.

        RED1-6-01: walks the parent chain TRANSITIVELY (breadth-first with a
        cycle guard), following each registered row's own ``parents_json`` — a
        cap 2+ levels up (org → user → agent) previously never locked/settled."""
        chain: list[BudgetKey] = []
        seen: set = set()
        queue: list[BudgetKey] = [key]
        while queue:
            k = queue.pop(0)
            sk = _serialise_key(k)
            if sk in seen:
                continue
            seen.add(sk)
            cur.execute(
                "SELECT parents_json FROM llm_router_envelopes "
                "WHERE key_blob = %s FOR UPDATE",
                (sk,),
            )
            row = cur.fetchone()
            if row is None:
                continue
            chain.append(k)
            for parent_key in _deserialise_parents(row[0]):
                if _serialise_key(parent_key) not in seen:
                    queue.append(parent_key)
        return chain

    # ── Soft tier ─────────────────────────────────────────────────────────

    def _maybe_flip_soft_state(self, key: BudgetKey) -> None:
        row = self._load(key)
        if row is None or row["soft_cap_usd"] is None:
            return
        total = row["consumed_usd"] + row["pending_usd"]
        is_breached = total >= row["soft_cap_usd"]
        if is_breached == row["soft_breached"]:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE llm_router_envelopes SET soft_breached = %s "
                "WHERE key_blob = %s",
                (is_breached, _serialise_key(key)),
            )
        self._conn.commit()
        if is_breached:
            log.warning(
                "budget_soft_cap_breached",
                key=str(key),
                soft_cap_usd=row["soft_cap_usd"],
                cap_usd=row["cap_usd"],
                consumed_usd=row["consumed_usd"],
                pending_usd=row["pending_usd"],
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
            "cap_usd": row["cap_usd"],
            "soft_cap_usd": row["soft_cap_usd"],
            "consumed_usd": row["consumed_usd"],
            "pending_usd": row["pending_usd"],
            "remaining_usd": max(
                0.0,
                row["cap_usd"] - row["consumed_usd"] - row["pending_usd"],
            ),
            "usage_pct": (row["consumed_usd"] + row["pending_usd"])
            / row["cap_usd"],
            "soft_breached": row["soft_breached"],
        }


__all__ = ["PostgresBudgetBackend"]
