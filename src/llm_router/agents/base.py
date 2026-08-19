"""Agent layer data model — AgentProfile + AgentSession + SessionState."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class SessionState(str, Enum):
    """Lifecycle states for an agent session.

    Transitions:
        ACTIVE → COMPLETED   (normal finish)
        ACTIVE → ERRORED     (caller hit an unrecoverable error)
        ACTIVE → BUDGET_EXCEEDED  (budget envelope refused a call)
    Terminal states are immutable; record_step on a terminal session is a
    no-op that returns the session unchanged.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    ERRORED = "errored"
    BUDGET_EXCEEDED = "budget_exceeded"
    # G-026/G-030: operator-initiated emergency stop. Distinct from
    # ERRORED so the audit can separate "we killed it" from "it crashed".
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            SessionState.COMPLETED,
            SessionState.ERRORED,
            SessionState.BUDGET_EXCEEDED,
            SessionState.CANCELLED,
        )


@dataclass(frozen=True)
class AgentProfile:
    """How LLM Router should route on behalf of one agent type.

    Profiles are pure data — no behaviour. The decision engine and the
    budget envelope consume them at runtime.

    Attributes:
        id: Stable identifier (matches config/agents.yaml `id:` field).
        description: Human-readable purpose. Surfaced in `llm_router_agent_list`.
        tier_preference: Ordered list of preferred model tiers — local /
            cheap / mid / premium. The selector tries these in order.
            Empty means "use the default chain".
        signal_boosts: Multipliers applied to specific signal scores when
            this agent's session is active. E.g. {"code_keywords": 1.5}
            pushes the decision engine harder toward code-routing rules.
        preferred_chain: Chain alias (from config/signals.yaml) the
            selector should resolve. Overrides the default decision chain
            when set.
        default_budget_usd: Per-session budget if the caller doesn't
            override at start_session time.
        hard_max_budget_usd: Absolute cap. Sessions can request lower but
            never higher than this value.
    """

    id: str
    description: str
    tier_preference: tuple[str, ...] = ()
    signal_boosts: dict[str, float] = field(default_factory=dict)
    preferred_chain: str = ""
    default_budget_usd: float = 0.50
    hard_max_budget_usd: float = 2.00


@dataclass(frozen=True)
class AgentRoutingPolicy:
    """T3-XL1 agent-aware routing bias attached to one ``AgentSession``.

    Distinct from ``AgentProfile`` (which is type-level config set at
    agent-registration time) — a ``AgentRoutingPolicy`` is per-session and
    inherits through the parent session chain so a sub-agent picks up its
    spawner's constraints unless it explicitly overrides them.

    Attributes:
        preferred_providers: Provider names in priority order
            (e.g. ``("anthropic", "openai", "gemini")``). The router puts
            preferred providers at the head of the candidate list. Empty
            means "no provider bias".
        preferred_models_by_classification: Per-classification model
            priority lists. Keyed by llm_router classification labels
            (``"simple"``, ``"moderate"``, ``"complex"``, ``"research"``,
            ``"code"``, …). Each value is the preferred model order for
            that classification.
        max_cost_per_turn_usd: Per-call cost cap, distinct from the
            session-level ``budget_cap_usd``. ``None`` = no per-turn cap.
        max_temperature: Sampling-temperature ceiling. ``None`` = no clamp.
        inherits_from: Optional explicit parent policy. ``resolved()``
            walks this chain and merges parent → child so the leaf wins.

    Inheritance is also driven implicitly by the session graph: see
    ``SessionStore.effective_policy``, which walks ``parent_session_id``
    and merges root → leaf. Both paths use ``merged_with`` so the rules
    are identical.
    """

    preferred_providers: tuple[str, ...] = ()
    # Mapping rather than dict so the frozen invariant is enforced; the
    # factory wraps any provided dict in a MappingProxyType.
    preferred_models_by_classification: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    max_cost_per_turn_usd: float | None = None
    max_temperature: float | None = None
    inherits_from: "AgentRoutingPolicy | None" = None

    def merged_with(self, parent: "AgentRoutingPolicy") -> "AgentRoutingPolicy":
        """Return a policy where ``self`` (child) overrides ``parent``.

        Merge rules:

        * **Scalar fields** (``max_cost_per_turn_usd``, ``max_temperature``)
          — child value wins if not ``None``; otherwise parent fills in.
        * **Provider tuple** — child wins outright if non-empty; otherwise
          parent's order survives. Tuples are not unioned because order
          matters for routing and a union would be ambiguous.
        * **Classification dict** — merged key-by-key. Child's keys
          replace parent's; parent keys absent from child survive.
        * **inherits_from** — dropped on the merged result; caller is
          expected to have already resolved the chain.
        """
        merged_dict = dict(parent.preferred_models_by_classification)
        merged_dict.update(self.preferred_models_by_classification)
        return AgentRoutingPolicy(
            preferred_providers=(
                self.preferred_providers
                if self.preferred_providers
                else parent.preferred_providers
            ),
            preferred_models_by_classification=MappingProxyType(merged_dict),
            max_cost_per_turn_usd=(
                self.max_cost_per_turn_usd
                if self.max_cost_per_turn_usd is not None
                else parent.max_cost_per_turn_usd
            ),
            max_temperature=(
                self.max_temperature
                if self.max_temperature is not None
                else parent.max_temperature
            ),
            inherits_from=None,
        )

    def resolved(self) -> "AgentRoutingPolicy":
        """Walk ``inherits_from`` chain and return the merged policy."""
        if self.inherits_from is None:
            return replace(self, inherits_from=None)
        parent_resolved = self.inherits_from.resolved()
        return self.merged_with(parent_resolved)


@dataclass(frozen=True)
class AgentSession:
    """One run of one agent. Immutable snapshot — SessionStore returns a
    fresh instance on every update.

    Cost accounting:
        budget_cap_usd is fixed at create time (between profile default
        and profile hard max). consumed_usd grows monotonically via
        record_step. remaining_usd is derived; never stored.
    """

    session_id: str
    agent_id: str
    started_at: float
    completed_at: float | None
    parent_session_id: str | None
    budget_cap_usd: float
    consumed_usd: float
    step_count: int
    state: SessionState
    framework: str | None = None  # which framework adapter started this
    # T3-M3 (Track-3 agent-safety) — runaway guards.
    # max_iterations: hard cap on step_count; record_step raises
    #   IterationsExceeded when reached and transitions the session
    #   to BUDGET_EXCEEDED (terminal). None = no cap.
    # max_recursion_depth: hard cap on the parent_session_id chain
    #   length at child-create time. None = no cap.
    max_iterations: int | None = None
    max_recursion_depth: int | None = None
    # T3-XL1: agent-aware routing policy. None = no policy on this
    # session; the effective policy may still be inherited from a
    # parent via SessionStore.effective_policy.
    routing_policy: AgentRoutingPolicy | None = None
    # G-029 ledger fields. tool_call_count increments via
    # record_tool_call; last_activity_at is bumped on every state
    # mutation and powers the admin "stuck" filter. Legacy rows
    # predating the column surface last_activity_at=None.
    tool_call_count: int = 0
    max_tool_calls: int | None = None
    max_children_concurrent: int | None = None
    last_activity_at: float | None = None

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_cap_usd - self.consumed_usd)

    @property
    def is_active(self) -> bool:
        return self.state == SessionState.ACTIVE
