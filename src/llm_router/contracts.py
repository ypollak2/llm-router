# Ported from Chuzom's routing_quality/execution_ledger/capabilities/budget_envelope contracts; env vars renamed to LLM_ROUTER_*.
"""Frozen migration contracts (WS0).

This module contains ONLY the frozen shape of the Chuzom contracts that later
workstreams (WS1+) will port into llm-router: Literal type aliases, frozensets,
dataclass field shapes, DB column tuples, and documented public API signatures.

It intentionally contains no behavior and no chuzom imports. Nothing in the
runtime imports this module yet — it exists purely to pin the exact contract
values so later workstreams cannot silently drift from what was audited here.
Any change to a value in this file must be treated as a deliberate, reviewed
contract change, not an incidental refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# routing_quality.py contracts
# ---------------------------------------------------------------------------

# Schema version for the JSONL routing-quality ledger (routing_quality.py).
# NOT the same counter as EXECUTION_LEDGER_SCHEMA_VERSION below — the two
# ledger subsystems version independently.
ROUTING_QUALITY_SCHEMA_VERSION = 2

BASELINE_POLICY_VERSION = "north-star-v1"

FallbackReason = Literal[
    "provider_failure",
    "timeout",
    "rate_limit",
    "health_skip",
    "policy_rejection",
    "budget_exhausted",
    "cost_cap",
    "capability_failure",
    "verification_failure",
    "quality_failure",
]
# Exactly 10 values.

RouteKind = Literal[
    "completion",
    "delegate",
    "bounded_operational",
    "delegate_substep",
]

# The subset of FallbackReason values that represent a quality/capability
# escalation (as opposed to a technical/infra fallback). Only these reasons
# imply mis_route=True; all other reasons leave mis_route=None (unknown).
QUALITY_REASONS: frozenset[str] = frozenset(
    {"capability_failure", "verification_failure", "quality_failure"}
)

# ---------------------------------------------------------------------------
# execution_ledger.py contracts
# ---------------------------------------------------------------------------

# Schema version for the SQLite execution_events table (execution_ledger.py).
# NOT the same counter as ROUTING_QUALITY_SCHEMA_VERSION above.
EXECUTION_LEDGER_SCHEMA_VERSION = 1

EventType = Literal[
    "route_started",
    "directive_injected",
    "attempt_started",
    "attempt_completed",
    "attempt_rejected",
    "attempt_failed",
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
    "route_realized",
]
# Exactly 17 values.

# Event types that represent a billable attempt (an attempt that consumed
# tokens/cost regardless of whether it won the route).
BILLABLE_EVENTS: frozenset[str] = frozenset(
    {"attempt_completed", "attempt_rejected", "attempt_failed"}
)

TerminalState = Literal[
    "accepted",
    "rejected",
    "failed",
    "cancelled",
    "bypassed",
    "overridden",
    "unknown",
]
# Exactly 7 values.

RealizationStatus = Literal["verified_used", "verified_overridden", "unknown"]
# Exactly 3 values.

AdoptionMethod = Literal["door_call", "agent_marked", "content_match", "unknown"]
# Exactly 4 values.

# Adoption methods that count a route's savings as "realized" (as opposed to
# merely "potential").
COUNTS_AS_REALIZED: frozenset[str] = frozenset({"door_call", "agent_marked"})

# Provider identifiers that represent the Claude subscription lane (as opposed
# to a metered API provider) for cost-accounting purposes.
CLAUDE_PROVIDERS: frozenset[str] = frozenset(
    {"claude_subscription", "subscription", "anthropic", "claude"}
)

# Full column list of the execution_events table, in column order.
EXECUTION_EVENTS_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "event_id",
    "ts",
    "session_id",
    "turn_id",
    "route_id",
    "attempt_id",
    "event_type",
    "task_type",
    "routing_profile",
    "host_mode",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "measured_cost_usd",
    "baseline_equivalent_cost_usd",
    "hook_input_tokens",
    "hook_output_tokens",
    "accepted",
    "rejected",
    "rejection_reason",
    "escalation_reason",
    "fallback_reason",
    "provider_failure_reason",
    "used_by_host",
    "realization_status",
    "override_type",
    "terminal_state",
    "metadata",
    "classifier_cost_usd",
    "failed_attempt_cost_usd",
    "baseline_tokens",
    "adoption_method",
)
# Exactly 36 columns.

# ---------------------------------------------------------------------------
# capabilities.py contracts
# ---------------------------------------------------------------------------

CapabilitySource = Literal["regex", "classifier", "repo_scan", "user_explicit", "history"]
# Exactly 5 values.


@dataclass(frozen=True)
class CapabilityRequirement:
    """8-boolean capability vector plus a derived `needs_tools` property."""

    read_files: bool = False
    write_files: bool = False
    run_commands: bool = False
    repo_search: bool = False
    git_operations: bool = False
    network_access: bool = False
    objective_verification: bool = False
    multi_step_execution: bool = False

    @property
    def needs_tools(self) -> bool:
        return any(
            (
                self.read_files,
                self.write_files,
                self.run_commands,
                self.repo_search,
                self.git_operations,
                self.network_access,
                self.objective_verification,
                self.multi_step_execution,
            )
        )


# Field names of CapabilityRequirement, in declaration order (excludes the
# derived `needs_tools` property, which is not a dataclass field).
CAPABILITY_REQUIREMENT_FIELDS: tuple[str, ...] = (
    "read_files",
    "write_files",
    "run_commands",
    "repo_search",
    "git_operations",
    "network_access",
    "objective_verification",
    "multi_step_execution",
)
# Exactly 8 fields.


@dataclass(frozen=True)
class CapabilityEvidence:
    source: CapabilitySource
    reason: str
    confidence: float  # 0.0-1.0


@dataclass(frozen=True)
class CapabilityDecision:
    required: CapabilityRequirement
    evidence: tuple[CapabilityEvidence, ...]
    confidence: float
    legacy_match: bool = False


# ---------------------------------------------------------------------------
# budget_envelope.py contracts
#
# This section documents the public API *signatures* of Chuzom's
# BudgetEnvelopeManager as string constants — it does not port the
# implementation (which depends on chuzom-internal `BudgetKey`/`budget`
# modules out of WS0's scope). WS5 will port the real implementation against
# llm-router's own budget key/store types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetEnvelope:
    """Documented shape of Chuzom's BudgetEnvelope dataclass."""

    key: str  # Chuzom's `BudgetKey`; documented here as `str` (opaque key type)
    cap_usd: float
    parents: tuple[str, ...] = field(default_factory=tuple)
    soft_cap_usd: float | None = None


# Public method/function signatures of Chuzom's BudgetEnvelopeManager module,
# recorded as documented strings (not executable code) for WS5 to port
# against llm-router's own budget key/store types.
BUDGET_ENVELOPE_API: tuple[str, ...] = (
    (
        "register(self, key: BudgetKey, cap_usd: float, *, "
        "parents: tuple[BudgetKey, ...] = (), soft_cap_usd: float | None = None) -> BudgetEnvelope"
    ),
    "get(self, key: BudgetKey) -> BudgetEnvelope | None",
    "consumed(self, key: BudgetKey) -> float",
    "pending(self, key: BudgetKey) -> float",
    "remaining(self, key: BudgetKey) -> float",
    "async try_reserve(self, key: BudgetKey, cost_usd: float) -> bool",
    "async release(self, key: BudgetKey, cost_usd: float) -> None",
    (
        "async commit(self, key: BudgetKey, cost_usd: float, *, "
        "settle_pending: bool = True) -> None"
    ),
    "async settle(self, key: BudgetKey, est_cost_usd: float, actual_cost_usd: float) -> None",
    "tier_state(self, key: BudgetKey) -> dict[str, float | bool | None]",
    "get_manager() -> BudgetEnvelopeManager",
    "reset_manager_for_tests() -> None",
)
# Exactly 12 signatures (10 BudgetEnvelopeManager methods + 2 module functions).

# Keys present in the dict returned by BudgetEnvelopeManager.tier_state().
BUDGET_TIER_STATE_KEYS: tuple[str, ...] = (
    "cap_usd",
    "soft_cap_usd",
    "consumed_usd",
    "pending_usd",
    "remaining_usd",
    "usage_pct",
    "soft_breached",
)
# Exactly 7 keys.
