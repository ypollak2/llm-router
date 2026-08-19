"""T2-M2: per-identity ``BudgetEnvelope`` with parent-child propagation.

Builds on T2-M1's ``BudgetKey`` (shape) and per-key reservation
primitives (``reserve_for`` / ``release_for``) by adding:

* **Caps.** Each envelope has a ``cap_usd``; the manager refuses
  reservations that would push consumed+pending over the cap.
* **Parent propagation.** A child envelope declares its parents; a
  reservation on the child is checked against every parent's cap
  atomically. If any envelope in the chain would breach, the
  reservation is refused and **no** envelope is changed.
* **Atomic check-then-charge** under an in-process ``asyncio.Lock``.
  T2-L1 will replace this with a distributed-safe backend; the
  in-process correctness is the shape T2-L1 has to honour.

Today's accounting is in-process only. Single-process deployments
get correct behaviour; multi-process / multi-host coordination lands
in T2-XL1 (per-tenant single-writer or central event stream — the
Q-P-2 hybrid A→B path explicitly defers this to Phase 3b).

Contract:

* ``register(key, cap_usd, parents=())`` — register or replace an
  envelope. Re-registering a key with a different cap is allowed;
  parent set must remain a subset of previously-known envelopes.
* ``try_reserve(key, cost_usd)`` — async. Returns ``True`` and
  reserves ``cost_usd`` on self + each parent. Returns ``False`` if
  any envelope would breach; no changes made.
* ``release(key, cost_usd)`` — async. Reverts a prior ``try_reserve``
  on self + parents (cancel / refund path).
* ``commit(key, cost_usd)`` — async. Moves ``cost_usd`` from
  pending to consumed on self + parents (success path).
* ``consumed(key)`` / ``pending(key)`` / ``remaining(key)`` —
  introspection accessors. Non-async because they read snapshot
  values; concurrent mutations may produce strictly-monotone-ish
  numbers, which is the correct semantic for a metric.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002 (parent-child
budget propagation slice of the per-identity budget cluster).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from llm_router.budget import release_for, reserve_for
from llm_router.budget_key import BudgetKey
from llm_router.logging import get_logger

log = get_logger("llm_router.budget_envelope")


@dataclass(frozen=True)
class BudgetEnvelope:
    """Static description of one envelope.

    The dynamic state (consumed_usd, pending tally) lives in the
    manager — the envelope is just the *contract*: which key, what
    cap, which parents to debit on every reservation.

    Tier semantics (T2-M3):

    * ``cap_usd`` is the **hard** cap. Reservations that would push
      consumed+pending above it are refused atomically — see
      ``BudgetEnvelopeManager.try_reserve``.
    * ``soft_cap_usd`` (optional) is a **soft** alerting threshold.
      Reservations that cross it succeed but the manager records the
      transition; operators can query ``tier_state(key)`` to surface
      alerts (e.g. dashboards, paging) without changing the
      enforcement contract.
    * Forecast / predictive tiers are deferred to T2-L1 — they need
      cross-instance burn-rate data that an in-process manager cannot
      compute correctly under multi-instance load.

    ``soft_cap_usd`` must be ``None`` or strictly less than
    ``cap_usd``; an equal or larger soft cap would be a no-op or
    nonsensical and is rejected at registration.
    """

    key: BudgetKey
    cap_usd: float
    parents: tuple[BudgetKey, ...] = field(default_factory=tuple)
    # T2-M3: optional soft alerting threshold. None = no soft tier.
    soft_cap_usd: float | None = None


class BudgetEnvelopeManager:
    """In-process atomic manager for BudgetEnvelope state.

    Single coarse asyncio.Lock around mutations: correct for the
    in-process case, simple to reason about, and gives T2-L1 a
    concrete reference behaviour to match when the distributed
    backend lands.
    """

    def __init__(self) -> None:
        self._envelopes: dict[BudgetKey, BudgetEnvelope] = {}
        self._consumed: dict[BudgetKey, float] = {}
        # Per-key pending in addition to budget._pending_spend_by_key.
        # The duplication is deliberate: the global dict carries every
        # reservation llm_router makes anywhere; this dict scopes to
        # envelopes specifically so a release on the envelope manager
        # doesn't accidentally floor a reservation that some other
        # subsystem made on the same key.
        self._pending: dict[BudgetKey, float] = {}
        # T2-M3: per-key soft-tier state. True once consumed+pending
        # crosses soft_cap_usd. The value flips back to False on a
        # release that brings the total back below the soft cap, so
        # callers polling ``tier_state`` see consistent transitions.
        self._soft_breached: dict[BudgetKey, bool] = {}
        self._lock = asyncio.Lock()

    def register(
        self,
        key: BudgetKey,
        cap_usd: float,
        *,
        parents: tuple[BudgetKey, ...] = (),
        soft_cap_usd: float | None = None,
    ) -> BudgetEnvelope:
        """Register or replace an envelope.

        ``cap_usd`` must be positive; ``parents`` may name keys that
        were not yet registered — they will be honoured if/when they
        are. A re-register preserves any existing consumed/pending
        totals so a long-lived parent re-registered with a higher cap
        does not lose its accounting history.

        T2-M3: ``soft_cap_usd`` (optional) sets a soft alerting
        threshold. Must be strictly less than ``cap_usd`` when
        provided — equal or larger would be a no-op or nonsensical.
        """
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
        env = BudgetEnvelope(
            key=key,
            cap_usd=float(cap_usd),
            parents=tuple(parents),
            soft_cap_usd=float(soft_cap_usd) if soft_cap_usd is not None else None,
        )
        self._envelopes[key] = env
        self._consumed.setdefault(key, 0.0)
        self._pending.setdefault(key, 0.0)
        self._soft_breached.setdefault(key, False)
        return env

    def get(self, key: BudgetKey) -> BudgetEnvelope | None:
        return self._envelopes.get(key)

    def consumed(self, key: BudgetKey) -> float:
        return self._consumed.get(key, 0.0)

    def pending(self, key: BudgetKey) -> float:
        return self._pending.get(key, 0.0)

    def remaining(self, key: BudgetKey) -> float:
        """``cap - consumed - pending`` floored at zero; ``inf`` for
        keys without a registered envelope (no enforcement)."""
        env = self._envelopes.get(key)
        if env is None:
            return float("inf")
        return max(
            0.0,
            env.cap_usd - self._consumed.get(key, 0.0) - self._pending.get(key, 0.0),
        )

    def _chain(self, key: BudgetKey) -> list[BudgetEnvelope]:
        """Return [self_env, *ancestor_envs] for the registered envelopes
        in the parent chain. Unregistered parents are silently
        skipped — caller knows what envelopes it registered.

        RED1-6-01: walks the chain TRANSITIVELY (breadth-first with a cycle
        guard) so a cap 2+ levels up (org → user → agent) is settled/enforced,
        not just the leaf's immediate parents."""
        out: list[BudgetEnvelope] = []
        seen: set = set()
        queue: list[BudgetKey] = [key]
        while queue:
            k = queue.pop(0)
            if k in seen:
                continue
            seen.add(k)
            env = self._envelopes.get(k)
            if env is None:
                continue
            out.append(env)
            for parent_key in env.parents:
                if parent_key not in seen:
                    queue.append(parent_key)
        return out

    async def try_reserve(self, key: BudgetKey, cost_usd: float) -> bool:
        """Atomic: walk self + parents; if every cap accommodates
        ``cost_usd`` on top of (consumed + pending), commit the
        reservation on each and return True. Otherwise return False
        and leave every envelope unchanged.

        Non-positive ``cost_usd`` is a no-op that returns True.
        """
        if cost_usd <= 0:
            return True
        async with self._lock:
            chain = self._chain(key)
            if not chain:
                # No envelope registered → no enforcement.
                return True
            for env in chain:
                projected = (
                    self._consumed.get(env.key, 0.0)
                    + self._pending.get(env.key, 0.0)
                    + cost_usd
                )
                if projected > env.cap_usd:
                    return False
            # All checks passed; commit pending on each.
            for env in chain:
                self._pending[env.key] = (
                    self._pending.get(env.key, 0.0) + cost_usd
                )
                reserve_for(env.key, cost_usd)
                self._update_soft_state(env)
            return True

    async def release(self, key: BudgetKey, cost_usd: float) -> None:
        """Revert a prior ``try_reserve``. Cancel / refund path."""
        if cost_usd <= 0:
            return
        async with self._lock:
            chain = self._chain(key)
            for env in chain:
                current = self._pending.get(env.key, 0.0)
                self._pending[env.key] = max(0.0, current - cost_usd)
                release_for(env.key, cost_usd)
                self._update_soft_state(env)

    async def commit(
        self, key: BudgetKey, cost_usd: float, *, settle_pending: bool = True
    ) -> None:
        """Move ``cost_usd`` from pending to consumed on self + parents.

        Success path: caller called ``try_reserve(key, estimated)``,
        the provider returned with ``actual_cost``, caller calls
        ``release(key, estimated)`` to undo the reservation exactly and then
        ``commit(key, actual_cost, settle_pending=False)`` to record true spend
        without touching pending again. ``settle_pending=True`` (the default)
        keeps the standalone "move pending→consumed" behaviour for callers that
        do not release separately.

        RED1-5-01: when ``settle_pending`` is False, pending is NOT decremented
        here (release already did it). Decrementing twice erased a concurrent
        sibling's outstanding reservation on a shared key and let a third caller
        exceed the cap.
        """
        if cost_usd <= 0:
            return
        async with self._lock:
            chain = self._chain(key)
            for env in chain:
                self._consumed[env.key] = (
                    self._consumed.get(env.key, 0.0) + cost_usd
                )
                if settle_pending:
                    # Standalone callers: decrement pending to match.
                    self._pending[env.key] = max(
                        0.0, self._pending.get(env.key, 0.0) - cost_usd
                    )
                    release_for(env.key, cost_usd)
                self._update_soft_state(env)

    async def settle(
        self, key: BudgetKey, est_cost_usd: float, actual_cost_usd: float
    ) -> None:
        """RED1-7-01: atomically undo the reservation (pending -= est) and record
        the real spend (consumed += actual) under a SINGLE lock-hold, so a
        concurrent try_reserve cannot observe the reservation as gone while the
        spend has not yet landed (which allowed a shared cap to be breached)."""
        if est_cost_usd <= 0 and actual_cost_usd <= 0:
            return
        est = float(est_cost_usd or 0.0)
        actual = float(actual_cost_usd or 0.0)
        async with self._lock:
            for env in self._chain(key):
                self._consumed[env.key] = self._consumed.get(env.key, 0.0) + actual
                self._pending[env.key] = max(0.0, self._pending.get(env.key, 0.0) - est)
                release_for(env.key, est)
                self._update_soft_state(env)

    # ── T2-M3 soft tier ────────────────────────────────────────────────────

    def _update_soft_state(self, env: BudgetEnvelope) -> None:
        """Recompute and persist the soft-breached flag for ``env``.

        Called from inside the manager lock after every consumed /
        pending mutation. No-op when the envelope has no soft cap.
        Emits a structured log on the rising edge (False → True) so
        operators can wire it to alerting without polling tier_state.
        """
        if env.soft_cap_usd is None:
            return
        total = self._consumed.get(env.key, 0.0) + self._pending.get(env.key, 0.0)
        was_breached = self._soft_breached.get(env.key, False)
        is_breached = total >= env.soft_cap_usd
        self._soft_breached[env.key] = is_breached
        if is_breached and not was_breached:
            log.warning(
                "budget_soft_cap_breached",
                key=str(env.key),
                soft_cap_usd=env.soft_cap_usd,
                cap_usd=env.cap_usd,
                consumed_usd=self._consumed.get(env.key, 0.0),
                pending_usd=self._pending.get(env.key, 0.0),
            )

    def tier_state(self, key: BudgetKey) -> dict[str, float | bool | None]:
        """Return introspection snapshot for ``key``.

        Keys:

        * ``cap_usd`` — hard cap or ``None`` if unregistered.
        * ``soft_cap_usd`` — soft cap or ``None``.
        * ``consumed_usd`` — committed spend so far.
        * ``pending_usd`` — outstanding reservations.
        * ``remaining_usd`` — ``cap - consumed - pending``, floored at 0.
        * ``usage_pct`` — ``(consumed+pending)/cap`` in [0.0, 1.0+).
          ``None`` if no envelope is registered (no enforcement, no
          meaningful ratio).
        * ``soft_breached`` — True iff consumed+pending ≥ soft_cap_usd.
          False when no soft cap is configured.

        Non-async because callers (dashboards, observability) expect a
        snapshot, not a serialised view. Concurrent mutations can
        produce numbers that drift mid-read; that's fine for a metric
        and matches the contract of ``consumed`` / ``pending`` /
        ``remaining``.
        """
        env = self._envelopes.get(key)
        if env is None:
            return {
                "cap_usd": None,
                "soft_cap_usd": None,
                "consumed_usd": 0.0,
                "pending_usd": 0.0,
                "remaining_usd": float("inf"),
                "usage_pct": None,
                "soft_breached": False,
            }
        consumed = self._consumed.get(key, 0.0)
        pending = self._pending.get(key, 0.0)
        return {
            "cap_usd": env.cap_usd,
            "soft_cap_usd": env.soft_cap_usd,
            "consumed_usd": consumed,
            "pending_usd": pending,
            "remaining_usd": max(0.0, env.cap_usd - consumed - pending),
            "usage_pct": (consumed + pending) / env.cap_usd,
            "soft_breached": self._soft_breached.get(key, False),
        }


# ── Module-level singleton ──────────────────────────────────────────────────

_manager: BudgetEnvelopeManager | None = None


def get_manager() -> BudgetEnvelopeManager:
    global _manager
    if _manager is None:
        _manager = BudgetEnvelopeManager()
    return _manager


def reset_manager_for_tests() -> None:
    """Drop the singleton so the next ``get_manager`` starts fresh.
    Production code never calls this."""
    global _manager
    _manager = None
    # Drop the global per-key reservation dict too so tests don't
    # leak pending spend between runs.
    from llm_router.budget import reset_pending_spend_for_tests
    reset_pending_spend_for_tests()


__all__ = [
    "BudgetEnvelope",
    "BudgetEnvelopeManager",
    "get_manager",
    "reset_manager_for_tests",
]
