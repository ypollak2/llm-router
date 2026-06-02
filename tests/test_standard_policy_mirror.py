"""Plan 07 Phase 1b.1: assert policies/standard.yaml mirrors ROUTING_TABLE byte-for-byte.

The point of standard.yaml is to be the data-file equivalent of the hardcoded
ROUTING_TABLE in profiles.py. If they ever diverge, routing behavior changes
silently — this test is the guardrail that catches that.

Wiring callers (router.py, auto-route.py, etc.) to consult the policy file is
deferred to Phase 1b.2. This phase just establishes the data + abstraction.
"""

from __future__ import annotations

import pytest

from llm_router.policy import PolicyManager, RoutingPolicy
from llm_router.profiles import ROUTING_TABLE
from llm_router.types import RoutingProfile, TaskType


@pytest.fixture
def standard_policy() -> RoutingPolicy:
    pm = PolicyManager()
    return pm.load_policy("standard")


class TestStandardPolicyExists:
    def test_load_standard_returns_policy(self, standard_policy: RoutingPolicy) -> None:
        assert standard_policy.name == "standard"

    def test_chains_field_populated(self, standard_policy: RoutingPolicy) -> None:
        assert standard_policy.chains, "standard policy must declare chains"
        assert "budget" in standard_policy.chains
        assert "balanced" in standard_policy.chains
        assert "premium" in standard_policy.chains


class TestRoutingTableEquivalence:
    """For every (profile, task_type) in ROUTING_TABLE, the YAML chain must match."""

    def test_all_routing_table_keys_present_in_yaml(
        self, standard_policy: RoutingPolicy
    ) -> None:
        missing: list[tuple[str, str]] = []
        for (profile, task_type) in ROUTING_TABLE:
            profile_key = profile.value
            task_key = task_type.value
            if profile_key not in standard_policy.chains:
                missing.append((profile_key, task_key))
                continue
            if task_key not in standard_policy.chains[profile_key]:
                missing.append((profile_key, task_key))
        assert not missing, f"standard.yaml is missing chains: {missing}"

    @pytest.mark.parametrize(
        ("profile", "task_type"),
        list(ROUTING_TABLE.keys()),
        ids=lambda v: getattr(v, "name", str(v)),
    )
    def test_chain_byte_identical(
        self,
        standard_policy: RoutingPolicy,
        profile: RoutingProfile,
        task_type: TaskType,
    ) -> None:
        expected = ROUTING_TABLE[(profile, task_type)]
        actual = standard_policy.chains[profile.value][task_type.value]
        assert actual == expected, (
            f"standard.yaml chain for ({profile.value}, {task_type.value}) "
            f"diverges from ROUTING_TABLE.\nExpected: {expected}\nActual:   {actual}"
        )


class TestPhase1b2LoaderInvariant:
    """Plan 07 Phase 1b.2: ROUTING_TABLE is hydrated from standard.yaml at import.

    Documents the architectural relationship for future contributors. If anyone
    re-introduces a hardcoded literal in profiles.py, this test catches the
    structural change (the loader stops being the source) even if the data
    happens to remain in sync.
    """

    def test_loader_function_exists_and_is_callable(self) -> None:
        from llm_router.profiles import _load_routing_table_from_policy

        assert callable(_load_routing_table_from_policy)

    def test_routing_table_equals_loader_output(self) -> None:
        from llm_router.profiles import (
            ROUTING_TABLE,
            _load_routing_table_from_policy,
        )

        assert _load_routing_table_from_policy() == ROUTING_TABLE


class TestDerivedConvenienceFields:
    """workhorses + fallback_chain_complex are declared explicitly, not auto-derived.

    standard.yaml should point them at the same data callers historically used:
    workhorses = chains.balanced.query, fallback_chain_complex = chains.premium.query.
    Asserting this keeps the convenience fields honest.
    """

    def test_workhorses_matches_balanced_query(
        self, standard_policy: RoutingPolicy
    ) -> None:
        assert standard_policy.workhorses == ROUTING_TABLE[
            (RoutingProfile.BALANCED, TaskType.QUERY)
        ]

    def test_fallback_chain_complex_matches_premium_query(
        self, standard_policy: RoutingPolicy
    ) -> None:
        assert standard_policy.fallback_chain_complex == ROUTING_TABLE[
            (RoutingProfile.PREMIUM, TaskType.QUERY)
        ]
