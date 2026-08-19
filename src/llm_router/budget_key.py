"""T2-M1: ``BudgetKey`` — the principal-scoped key for cost accounting.

The Q-P-2 Phase 3a decision keeps llm_router single-org-per-instance, but
makes the budget *shape* hierarchical from day 1 so:

* T2-M2 (parent-child budget envelope) can layer on top without a
  schema migration.
* T2-L1 (atomic check-then-charge backend) can persist budgets
  against this exact key shape.
* Phase 3b (sidecar-per-tenant) just means each sidecar's budgets
  carry a non-default ``tenant_id`` — no code change in this layer.

Today's existing accounting (``budget.reserve_tokens(provider, tokens)``
+ ``router._pending_spend``) remains untouched for backwards compat
with the 24+ in-flight call sites. This module adds a *parallel*
key-scoped accounting surface that the router can opt into on
identity-aware paths.

A ``BudgetKey`` is a frozen value object so it can be a dict key, a
lock identifier (T2-L1), and a hashable audit dimension. Equality
is structural; ordering is not defined.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_router.identity import TurnIdentity


# Canonical scope strings. These name the *kind* of spend the key
# represents so a future BudgetEnvelope can rate-limit different
# scopes differently (e.g. one tenant-wide monthly cap + one per-user
# daily cap, both checked atomically at the same key).
SCOPE_TURN = "turn"          # one routed turn
SCOPE_AGENT_SESSION = "agent_session"  # an AgentSession's lifetime
SCOPE_WORKFLOW = "workflow"  # a multi-agent workflow run
SCOPE_DAILY = "daily"
SCOPE_MONTHLY = "monthly"


@dataclass(frozen=True)
class BudgetKey:
    """Principal-scoped budget identity.

    Fields are ordered most-aggregating → least-aggregating
    (tenant → org → user → agent → scope) so when T2-L1 lands the
    SQL ``ORDER BY`` for "rollup by tenant" is naturally aligned.

    All non-scope fields are optional. Production identity-aware
    paths set every field that ``TurnIdentity`` carries; Tier-1 paths
    may leave ``agent_id`` as None when the turn is not part of an
    agent run.
    """

    tenant_id: str | None
    org_id: str | None
    user_id: str | None
    agent_id: str | None
    scope: str = SCOPE_TURN

    def rolls_up_to(self, *, drop: str) -> "BudgetKey":
        """Return a coarser key with one field dropped.

        Used for parent-child propagation in T2-M2 and for tenant
        rollup reports. Example::

            >>> k = BudgetKey("t1", "o1", "alice", "agent-7")
            >>> k.rolls_up_to(drop="agent_id")
            BudgetKey(tenant_id='t1', org_id='o1', user_id='alice', agent_id=None, scope='turn')
        """
        kwargs = {
            "tenant_id": self.tenant_id,
            "org_id": self.org_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "scope": self.scope,
        }
        if drop not in {"tenant_id", "org_id", "user_id", "agent_id"}:
            raise ValueError(
                f"BudgetKey.rolls_up_to: cannot drop {drop!r} — "
                "only the identity fields can be coarsened"
            )
        kwargs[drop] = None
        return BudgetKey(**kwargs)


def budget_key_from_identity(
    identity: "TurnIdentity",
    *,
    scope: str = SCOPE_TURN,
) -> BudgetKey:
    """Derive a ``BudgetKey`` from a resolved ``TurnIdentity``.

    Phase 3a semantics: ``tenant_id`` defaults to ``org_id`` inside
    ``current_identity()``, so production keys always carry both
    fields. ``agent_id`` may be None when the turn is not part of an
    agent run.
    """
    return BudgetKey(
        tenant_id=identity.tenant_id or identity.org_id,
        org_id=identity.org_id,
        user_id=identity.user_id,
        agent_id=identity.agent_id,
        scope=scope,
    )


__all__ = [
    "BudgetKey",
    "budget_key_from_identity",
    "SCOPE_TURN",
    "SCOPE_AGENT_SESSION",
    "SCOPE_WORKFLOW",
    "SCOPE_DAILY",
    "SCOPE_MONTHLY",
]
