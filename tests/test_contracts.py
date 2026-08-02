"""Literal snapshot tests for src/llm_router/contracts.py (WS0).

Every expected value below is spelled out directly in this file (not derived
from the module under test), so any accidental drift in the frozen contract
fails loudly instead of silently passing a tautological self-comparison.
"""

from __future__ import annotations

import typing

from llm_router import contracts


def _literal_values(tp: object) -> tuple[object, ...]:
    """Extract the literal values of a typing.Literal alias, in order."""
    return typing.get_args(tp)


class TestRoutingQualityContracts:
    def test_schema_version(self):
        assert contracts.ROUTING_QUALITY_SCHEMA_VERSION == 2

    def test_baseline_policy_version(self):
        assert contracts.BASELINE_POLICY_VERSION == "north-star-v1"

    def test_fallback_reason_values(self):
        expected = (
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
        )
        assert _literal_values(contracts.FallbackReason) == expected
        assert len(expected) == 10

    def test_route_kind_values(self):
        expected = (
            "completion",
            "delegate",
            "bounded_operational",
            "delegate_substep",
        )
        assert _literal_values(contracts.RouteKind) == expected

    def test_quality_reasons(self):
        assert contracts.QUALITY_REASONS == frozenset(
            {"capability_failure", "verification_failure", "quality_failure"}
        )
        assert isinstance(contracts.QUALITY_REASONS, frozenset)


class TestExecutionLedgerContracts:
    def test_schema_version(self):
        assert contracts.EXECUTION_LEDGER_SCHEMA_VERSION == 1

    def test_schema_versions_are_independent_counters(self):
        # The two ledger subsystems must never be conflated.
        assert (
            contracts.ROUTING_QUALITY_SCHEMA_VERSION
            != contracts.EXECUTION_LEDGER_SCHEMA_VERSION
        )

    def test_event_type_values(self):
        expected = (
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
        )
        assert _literal_values(contracts.EventType) == expected
        assert len(expected) == 17

    def test_billable_events(self):
        assert contracts.BILLABLE_EVENTS == frozenset(
            {"attempt_completed", "attempt_rejected", "attempt_failed"}
        )

    def test_terminal_state_values(self):
        expected = (
            "accepted",
            "rejected",
            "failed",
            "cancelled",
            "bypassed",
            "overridden",
            "unknown",
        )
        assert _literal_values(contracts.TerminalState) == expected
        assert len(expected) == 7

    def test_realization_status_values(self):
        expected = ("verified_used", "verified_overridden", "unknown")
        assert _literal_values(contracts.RealizationStatus) == expected
        assert len(expected) == 3

    def test_adoption_method_values(self):
        expected = ("door_call", "agent_marked", "content_match", "unknown")
        assert _literal_values(contracts.AdoptionMethod) == expected
        assert len(expected) == 4

    def test_counts_as_realized(self):
        assert contracts.COUNTS_AS_REALIZED == frozenset({"door_call", "agent_marked"})

    def test_claude_providers(self):
        assert contracts.CLAUDE_PROVIDERS == frozenset(
            {"claude_subscription", "subscription", "anthropic", "claude"}
        )

    def test_execution_events_columns(self):
        expected = (
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
        assert contracts.EXECUTION_EVENTS_COLUMNS == expected
        assert len(expected) == 36


class TestCapabilitiesContracts:
    def test_capability_source_values(self):
        expected = ("regex", "classifier", "repo_scan", "user_explicit", "history")
        assert _literal_values(contracts.CapabilitySource) == expected
        assert len(expected) == 5

    def test_capability_requirement_fields(self):
        expected = (
            "read_files",
            "write_files",
            "run_commands",
            "repo_search",
            "git_operations",
            "network_access",
            "objective_verification",
            "multi_step_execution",
        )
        assert contracts.CAPABILITY_REQUIREMENT_FIELDS == expected
        assert len(expected) == 8

        field_names = tuple(f.name for f in __import__("dataclasses").fields(
            contracts.CapabilityRequirement
        ))
        assert field_names == expected

    def test_capability_requirement_defaults_all_false(self):
        req = contracts.CapabilityRequirement()
        assert req.needs_tools is False
        for name in contracts.CAPABILITY_REQUIREMENT_FIELDS:
            assert getattr(req, name) is False

    def test_capability_requirement_needs_tools_true_if_any_set(self):
        req = contracts.CapabilityRequirement(read_files=True)
        assert req.needs_tools is True

    def test_capability_evidence_fields(self):
        import dataclasses

        field_names = tuple(f.name for f in dataclasses.fields(contracts.CapabilityEvidence))
        assert field_names == ("source", "reason", "confidence")

    def test_capability_decision_fields(self):
        import dataclasses

        field_names = tuple(f.name for f in dataclasses.fields(contracts.CapabilityDecision))
        assert field_names == ("required", "evidence", "confidence", "legacy_match")
        decision = contracts.CapabilityDecision(
            required=contracts.CapabilityRequirement(),
            evidence=(),
            confidence=0.0,
        )
        assert decision.legacy_match is False


class TestBudgetEnvelopeContracts:
    def test_budget_envelope_fields(self):
        import dataclasses

        field_names = tuple(f.name for f in dataclasses.fields(contracts.BudgetEnvelope))
        assert field_names == ("key", "cap_usd", "parents", "soft_cap_usd")

    def test_budget_envelope_api_signatures(self):
        expected = (
            (
                "register(self, key: BudgetKey, cap_usd: float, *, "
                "parents: tuple[BudgetKey, ...] = (), soft_cap_usd: float | None = None) "
                "-> BudgetEnvelope"
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
        assert contracts.BUDGET_ENVELOPE_API == expected
        assert len(expected) == 12

    def test_budget_tier_state_keys(self):
        expected = (
            "cap_usd",
            "soft_cap_usd",
            "consumed_usd",
            "pending_usd",
            "remaining_usd",
            "usage_pct",
            "soft_breached",
        )
        assert contracts.BUDGET_TIER_STATE_KEYS == expected
        assert len(expected) == 7


class TestModuleHygiene:
    def test_no_chuzom_imports(self):
        import pathlib

        src = pathlib.Path(contracts.__file__).read_text(encoding="utf-8")
        # Only the mandated provenance comment may mention chuzom; no import
        # statement may reference it.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "chuzom" not in stripped.lower()
