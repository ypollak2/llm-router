# Ported from Chuzom's budget_envelope.py; env vars renamed to LLM_ROUTER_*;
# data source rewired to llm-router's layer. `BudgetEnvelope` is reused
# unchanged from the frozen `llm_router.contracts` (WS0) instead of being
# redefined here. Chuzom's `BudgetKey` is a `(tenant_id, org_id, user_id,
# agent_id, scope)` dataclass -- a multi-tenant concept that has no equivalent
# in llm-router's single-user CLI product. `contracts.BudgetEnvelope.key` is
# already typed as a plain `str` ("Chuzom's `BudgetKey`; documented here as
# `str` (opaque key type)") and contracts.py's own comment licenses this
# workstream to "port the real implementation against llm-router's own budget
# key/store types" -- so `BudgetKey` here is simply `str`; callers are free to
# use whatever scoping scheme they like (e.g. ``"session:<id>"``,
# ``"daily:<date>"``, ``"monthly:<yyyy-mm>"``).
#
# Chuzom additionally mirrors every reservation into a second, module-level
# ``_pending_spend_by_key`` dict in ``chuzom.budget`` (deliberately duplicated
# there so other subsystems can see chuzom's reservations without depending on
# the envelope manager). llm-router does not have -- and this port does not
# introduce -- that second store: this manager's own ``_pending`` dict is the
# single source of truth for envelope accounting, per the "no parallel spend
# store" constraint on this port. `reset_manager_for_tests()` therefore only
# needs to reset this module's own singleton, unlike chuzom's version, which
# also calls out to ``chuzom.budget.reset_pending_spend_for_tests()``.
#
# WS5 ships this module standalone, gated by `LLM_ROUTER_BUDGET_ENVELOPE`
# (default off): with the flag off, nothing in this module is invoked by the
# router, so routing/spend behavior stays byte-identical to pre-WS5 llm-router.
# Wiring this manager into the live dispatch loop's existing `_pending_spend`
# reserve/release sites is intentionally deferred -- see the migration plan's
# "envelope-vs-pending-spend" ADR item -- rather than risking a correctness
# regression in that call path within an otherwise fully-independent
# workstream.
"""Budget envelope: atomic reserve -> commit/release/settle spend accounting.

Ported from Chuzom's T2-M2/T2-M3 budget-envelope work: caps (and optional
soft-cap alerting tiers) on a key, with parent-child cap propagation (a
child's spend also debits every ancestor envelope in its chain) and atomic
check-then-charge accounting guarded by a single ``asyncio.Lock`` (in-process
only; multi-process/multi-worker accounting is out of scope here, same as
upstream).

Typical protocol for a single call:

    mgr = get_manager()
    mgr.register("session:abc123", cap_usd=5.0)
    if not await mgr.try_reserve("session:abc123", estimated_cost):
        ...refuse / downgrade...
    try:
        actual_cost = await do_the_call()
    finally:
        await mgr.settle("session:abc123", estimated_cost, actual_cost)

``settle`` is the preferred single-call finish: it atomically undoes the
reservation and records the real spend in one lock acquisition. ``release``
followed by ``commit(..., settle_pending=False)`` is equivalent but takes two
lock acquisitions and is only worth using when the two steps genuinely happen
at different times (see ``commit``'s docstring for the exact protocol).
"""

from __future__ import annotations

import asyncio
import os

from llm_router.contracts import BudgetEnvelope
from llm_router.logging import get_logger

log = get_logger("llm_router.budget_envelope")

# Chuzom's `BudgetKey` is a tenant/org/user/agent dataclass; llm-router has no
# multi-tenant concept, so the key is simply an opaque string (see the module
# provenance header above for the full rationale).
BudgetKey = str

__all__ = [
    "BudgetEnvelope",
    "BudgetEnvelopeManager",
    "BudgetKey",
    "budget_envelope_enabled",
    "get_manager",
    "reset_manager_for_tests",
]


def budget_envelope_enabled() -> bool:
    """WS5 ships flag-off: envelope accounting is only active when explicitly
    enabled -- it never drives a live routing/spend decision by default (the
    live dispatch loop's existing `_pending_spend` mechanism is untouched)."""
    return os.environ.get("LLM_ROUTER_BUDGET_ENVELOPE", "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


class BudgetEnvelopeManager:
    """In-process, lock-guarded budget envelope accounting.

    Implements the 10 instance methods documented in
    ``llm_router.contracts.BUDGET_ENVELOPE_API`` exactly (method names,
    parameter names/order/defaults, return types).
    """

    def __init__(self) -> None:
        self._envelopes: dict[BudgetKey, BudgetEnvelope] = {}
        self._consumed: dict[BudgetKey, float] = {}
        self._pending: dict[BudgetKey, float] = {}
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
        """Register (or re-register) an envelope for *key*.

        Re-registering an already-known key preserves its existing
        consumed/pending/soft-breach accounting (only the cap/parents/soft-cap
        shape is replaced) via ``setdefault`` below.
        """
        if cap_usd <= 0:
            raise ValueError(f"cap_usd must be positive, got {cap_usd!r}")
        if soft_cap_usd is not None and not (0 < soft_cap_usd < cap_usd):
            raise ValueError(
                f"soft_cap_usd must be > 0 and < cap_usd, got {soft_cap_usd!r} "
                f"(cap_usd={cap_usd!r})"
            )
        env = BudgetEnvelope(
            key=key,
            cap_usd=cap_usd,
            parents=tuple(parents),
            soft_cap_usd=soft_cap_usd,
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
        """Cap minus consumed minus pending; unbounded (``inf``) if *key* was
        never registered -- an unregistered key means "no enforcement"."""
        env = self._envelopes.get(key)
        if env is None:
            return float("inf")
        used = self._consumed.get(key, 0.0) + self._pending.get(key, 0.0)
        return max(0.0, env.cap_usd - used)

    def _chain(self, key: BudgetKey) -> list[BudgetEnvelope]:
        """Return ``[self_env, *ancestor_envs]`` for *key*.

        Ported from Chuzom's RED1-6-01 fix: a naive walk that only visits the
        *direct* parents misses transitive grandparents, silently under-
        propagating a debit past the first ancestor. This does a BFS over the
        full parent graph with a ``seen`` cycle guard (a cap graph should never
        have cycles, but a guard costs nothing and avoids an infinite loop if
        one is ever misconfigured). Unregistered parent keys are skipped
        silently -- they carry no cap to enforce.
        """
        env = self._envelopes.get(key)
        if env is None:
            return []
        chain: list[BudgetEnvelope] = [env]
        seen: set[BudgetKey] = {key}
        queue: list[BudgetKey] = list(env.parents)
        while queue:
            pkey = queue.pop(0)
            if pkey in seen:
                continue
            seen.add(pkey)
            penv = self._envelopes.get(pkey)
            if penv is None:
                continue
            chain.append(penv)
            queue.extend(penv.parents)
        return chain

    async def try_reserve(self, key: BudgetKey, cost_usd: float) -> bool:
        """Atomically reserve *cost_usd* against *key* and every ancestor.

        Refuses (returns ``False``) with NO mutation at all if any envelope in
        the chain would be pushed over its cap. An unregistered *key* always
        succeeds (no envelope means no enforcement).
        """
        if cost_usd <= 0:
            return True
        async with self._lock:
            chain = self._chain(key)
            if not chain:
                return True
            for env in chain:
                used = self._consumed.get(env.key, 0.0) + self._pending.get(env.key, 0.0)
                if used + cost_usd > env.cap_usd:
                    return False
            for env in chain:
                self._pending[env.key] = self._pending.get(env.key, 0.0) + cost_usd
                self._update_soft_state(env)
            return True

    async def release(self, key: BudgetKey, cost_usd: float) -> None:
        """Undo a reservation on *key* and every ancestor (floors at 0)."""
        if cost_usd <= 0:
            return
        async with self._lock:
            for env in self._chain(key):
                current = self._pending.get(env.key, 0.0)
                self._pending[env.key] = max(0.0, current - cost_usd)
                self._update_soft_state(env)

    async def commit(self, key: BudgetKey, cost_usd: float, *, settle_pending: bool = True) -> None:
        """Move *cost_usd* into consumed spend for *key* and every ancestor.

        Ported from Chuzom's RED1-5-01 fix. Two valid call protocols:

        * Reserved via ``try_reserve`` and now committing the *same* amount in
          one shot: call ``commit(cost_usd, settle_pending=True)`` (the
          default) -- this both records the spend AND clears the matching
          reservation.
        * Already released the reservation separately (e.g. via ``release``)
          and is now recording actual spend on its own: call
          ``commit(cost_usd, settle_pending=False)``. Passing
          ``settle_pending=True`` here would decrement ``pending`` a second
          time for the same reservation.

        When in doubt, prefer :meth:`settle`, which does both steps
        atomically under a single lock acquisition.
        """
        if cost_usd <= 0:
            return
        async with self._lock:
            for env in self._chain(key):
                self._consumed[env.key] = self._consumed.get(env.key, 0.0) + cost_usd
                if settle_pending:
                    current = self._pending.get(env.key, 0.0)
                    self._pending[env.key] = max(0.0, current - cost_usd)
                self._update_soft_state(env)

    async def settle(self, key: BudgetKey, est_cost_usd: float, actual_cost_usd: float) -> None:
        """Atomically undo a reservation and record real spend in one lock hold.

        Ported from Chuzom's RED1-7-01 fix: doing this as two separate calls
        (``release(est)`` then ``commit(actual, settle_pending=False)``) opens
        a window -- between the two lock acquisitions -- where a concurrent
        ``try_reserve`` can observe the reservation already gone but the real
        spend not yet recorded, letting it slip past a cap it should have been
        blocked by. ``settle`` closes that window by doing both under one
        lock acquisition.
        """
        if est_cost_usd <= 0 and actual_cost_usd <= 0:
            return
        async with self._lock:
            for env in self._chain(key):
                if est_cost_usd > 0:
                    current = self._pending.get(env.key, 0.0)
                    self._pending[env.key] = max(0.0, current - est_cost_usd)
                if actual_cost_usd > 0:
                    self._consumed[env.key] = self._consumed.get(env.key, 0.0) + actual_cost_usd
                self._update_soft_state(env)

    def _update_soft_state(self, env: BudgetEnvelope) -> None:
        """T2-M3 soft-cap bookkeeping: alerting only, never blocks a reserve.

        Logs a warning on the rising edge (not-breached -> breached) only, so
        a sustained breach doesn't spam the log on every subsequent call.
        """
        if env.soft_cap_usd is None:
            return
        total = self._consumed.get(env.key, 0.0) + self._pending.get(env.key, 0.0)
        was_breached = self._soft_breached.get(env.key, False)
        is_breached = total >= env.soft_cap_usd
        self._soft_breached[env.key] = is_breached
        if is_breached and not was_breached:
            log.warning(
                "budget_soft_cap_breached key=%s soft_cap_usd=%s cap_usd=%s "
                "consumed_usd=%s pending_usd=%s",
                env.key,
                env.soft_cap_usd,
                env.cap_usd,
                self._consumed.get(env.key, 0.0),
                self._pending.get(env.key, 0.0),
            )

    def tier_state(self, key: BudgetKey) -> dict[str, float | bool | None]:
        """Return the 7-key introspection dict documented in
        ``llm_router.contracts.BUDGET_TIER_STATE_KEYS``."""
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


_manager: BudgetEnvelopeManager | None = None


def get_manager() -> BudgetEnvelopeManager:
    """Return the process-wide :class:`BudgetEnvelopeManager` singleton."""
    global _manager
    if _manager is None:
        _manager = BudgetEnvelopeManager()
    return _manager


def reset_manager_for_tests() -> None:
    """Drop the singleton so the next :func:`get_manager` call starts fresh.

    Unlike Chuzom's version, this does not need to reset any second,
    module-level pending-spend store -- this port intentionally has none (see
    the module provenance header).
    """
    global _manager
    _manager = None
