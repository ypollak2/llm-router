# Ported from Chuzom's test_bounded_operational.py; imports/env vars renamed to
# llm_router.bounded_operational / LLM_ROUTER_BOUNDED_OPERATIONAL.
#
# NOTE ON SCOPE: chuzom's original test_bounded_operational.py also contains a
# `_WritingAgent` fake + three scenario/e2e tests (test_scenario4_bounded_edit_
# writes_verifies_records, test_bounded_caps_plan_to_one_milestone,
# test_auto_detect_off_by_default_stays_delegate) that exercise a full ReAct-loop
# delegation-execution engine (chuzom.agentic.engine.AgentRunResult,
# chuzom.agentic.react.ReActAgent, chuzom.tools.agentic.llm_delegate with
# planner_factory/adapters_factory monkeypatch seams, and a temp_db fixture).
# llm-router has no such engine (bounded_operational.py itself documents this as
# a deliberate gap for a future workstream), so those three tests are NOT ported
# here -- only the three pure-unit tests that exercise should_route_bounded() and
# bounded_op_budget_usd() in isolation are portable without that engine.
"""CF-4 bounded-operational route: decision predicate + pricing-derived budget."""
from __future__ import annotations

import pytest

from llm_router.bounded_operational import (
    bounded_op_budget_usd,
    bounded_operational_enabled,
    should_route_bounded,
)


def test_budget_is_pricing_derived_and_positive():
    tier1_budget = bounded_op_budget_usd(model_tier=1)
    tier3_budget = bounded_op_budget_usd(model_tier=3)
    assert tier1_budget > 0
    assert tier3_budget > 0
    # a pricier tier must never yield a smaller derived budget
    assert tier3_budget > tier1_budget
    # cheap-tier floor: at least the configured budget floor
    assert tier1_budget >= 0.01


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)
    assert bounded_operational_enabled() is False
    assert should_route_bounded("write a file and run the tests", "simple") is False


@pytest.mark.parametrize(
    ("flag_on", "complexity", "req_flags", "expected"),
    [
        # flag off entirely -> never bounded, regardless of prompt/complexity
        (False, "simple", {"write_files": True}, False),
        # flag on, but complexity is not "simple" -> never bounded
        (True, "moderate", {"write_files": True}, False),
        # flag on, simple, but no tool-needing capability detected -> not bounded
        (True, "simple", {}, False),
        # flag on, simple, and at least one qualifying capability -> bounded
        (True, "simple", {"write_files": True}, True),
        (True, "simple", {"run_commands": True}, True),
    ],
)
def test_should_route_bounded_matrix(
    monkeypatch, flag_on, complexity, req_flags, expected
):
    if flag_on:
        monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
    else:
        monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)

    # llm_router.capabilities does not exist yet (documented gap in
    # bounded_operational.py); should_route_bounded() lazy-imports it and fails
    # open to False on ImportError. To exercise the "capability detected"
    # branches of the matrix without that module existing, we inject a stub
    # module into sys.modules for the duration of this test.
    import sys
    import types

    from llm_router.contracts import CapabilityRequirement

    stub = types.ModuleType("llm_router.capabilities")
    stub.detect_capabilities = lambda _prompt: CapabilityRequirement(**req_flags)
    monkeypatch.setitem(sys.modules, "llm_router.capabilities", stub)

    assert should_route_bounded("some prompt", complexity) is expected


def test_should_route_bounded_fails_open_without_capabilities_module(monkeypatch):
    """Documented gap: llm_router.capabilities doesn't exist in this repo yet.
    should_route_bounded() must fail open to False rather than raise."""
    import sys

    monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
    monkeypatch.delitem(sys.modules, "llm_router.capabilities", raising=False)
    assert should_route_bounded("write a file", "simple") is False


def test_no_chuzom_in_runtime_strings():
    """Brand-leak guard: no 'chuzom' in this module's runtime-visible surface."""
    import llm_router.bounded_operational as bo

    assert "chuzom" not in "LLM_ROUTER_BOUNDED_OPERATIONAL".lower()
    assert all("chuzom" not in name.lower() for name in bo._TIER_PRICING_MODEL.values())
