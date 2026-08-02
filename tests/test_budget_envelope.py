# Ported from Chuzom's test_t2_m2_budget_envelope.py; env vars renamed to
# LLM_ROUTER_*; BudgetKey adapted from Chuzom's tenant/org/user dataclass to a
# plain string (see src/llm_router/budget_envelope.py's provenance header for
# the full rationale). Adds new coverage for settle() and soft-cap/tier_state()
# -- both absent from Chuzom's own T2-M2 test file (settle() is T2-M2-adjacent
# plumbing and soft-cap is T2-M3) -- plus contract-compliance, flag-semantics,
# flag-off-invariance, and brand-leak tests specific to this port.
"""Tests for llm_router.budget_envelope (WS5, flag LLM_ROUTER_BUDGET_ENVELOPE)."""

from __future__ import annotations

import asyncio
import inspect
import os

import pytest

from llm_router import budget_envelope as be
from llm_router.contracts import BUDGET_ENVELOPE_API, BUDGET_TIER_STATE_KEYS, BudgetEnvelope

# asyncio_mode = "auto" is set in pyproject.toml's [tool.pytest.ini_options],
# so plain `async def test_*` functions are collected automatically -- no
# blanket `pytestmark = pytest.mark.asyncio` needed (that would incorrectly
# mark this file's many synchronous tests too).


@pytest.fixture(autouse=True)
def _reset_manager():
    """Isolate every test's singleton state.

    Unlike Chuzom's fixture, this does not also need to reset a second,
    parallel `_pending_spend_by_key` store -- this port has none (see the
    module's provenance header).
    """
    be.reset_manager_for_tests()
    yield
    be.reset_manager_for_tests()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_BUDGET_ENVELOPE", raising=False)


# ---------------------------------------------------------------------------
# register / cap validation / remaining
# ---------------------------------------------------------------------------


def test_register_returns_envelope_with_expected_shape():
    mgr = be.get_manager()
    env = mgr.register("session:abc", cap_usd=5.0)
    assert env.key == "session:abc"
    assert env.cap_usd == 5.0
    assert env.parents == ()
    assert env.soft_cap_usd is None


def test_register_rejects_non_positive_cap():
    mgr = be.get_manager()
    with pytest.raises(ValueError):
        mgr.register("session:abc", cap_usd=0.0)
    with pytest.raises(ValueError):
        mgr.register("session:abc", cap_usd=-1.0)


def test_register_rejects_soft_cap_out_of_range():
    mgr = be.get_manager()
    with pytest.raises(ValueError):
        mgr.register("session:abc", cap_usd=5.0, soft_cap_usd=0.0)
    with pytest.raises(ValueError):
        mgr.register("session:abc", cap_usd=5.0, soft_cap_usd=5.0)
    with pytest.raises(ValueError):
        mgr.register("session:abc", cap_usd=5.0, soft_cap_usd=6.0)


def test_register_accepts_valid_soft_cap():
    mgr = be.get_manager()
    env = mgr.register("session:abc", cap_usd=5.0, soft_cap_usd=4.0)
    assert env.soft_cap_usd == 4.0


def test_remaining_before_register_is_unbounded():
    mgr = be.get_manager()
    assert mgr.remaining("session:never-registered") == float("inf")
    assert mgr.consumed("session:never-registered") == 0.0
    assert mgr.pending("session:never-registered") == 0.0
    assert mgr.get("session:never-registered") is None


def test_remaining_after_register_equals_cap():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    assert mgr.remaining("session:abc") == 5.0


# ---------------------------------------------------------------------------
# try_reserve / release
# ---------------------------------------------------------------------------


async def test_reserve_below_cap_succeeds():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    assert await mgr.try_reserve("session:abc", 2.0) is True
    assert mgr.pending("session:abc") == 2.0
    assert mgr.remaining("session:abc") == 3.0


async def test_reserve_exactly_at_cap_succeeds():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    assert await mgr.try_reserve("session:abc", 5.0) is True
    assert mgr.remaining("session:abc") == 0.0


async def test_reserve_over_cap_is_refused_and_atomic():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    assert await mgr.try_reserve("session:abc", 5.01) is False
    # Refusal must not mutate state at all.
    assert mgr.pending("session:abc") == 0.0
    assert mgr.remaining("session:abc") == 5.0


async def test_reserve_on_unregistered_key_always_succeeds():
    mgr = be.get_manager()
    assert await mgr.try_reserve("session:never-registered", 1_000_000.0) is True


@pytest.mark.parametrize("cost", [0.0, -1.0])
async def test_reserve_non_positive_is_a_noop(cost):
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    assert await mgr.try_reserve("session:abc", cost) is True
    assert mgr.pending("session:abc") == 0.0


async def test_release_reverts_pending():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 2.0)
    await mgr.release("session:abc", 2.0)
    assert mgr.pending("session:abc") == 0.0
    assert mgr.remaining("session:abc") == 5.0


async def test_release_floors_at_zero():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 1.0)
    await mgr.release("session:abc", 999.0)
    assert mgr.pending("session:abc") == 0.0


@pytest.mark.parametrize("cost", [0.0, -1.0])
async def test_release_non_positive_is_a_noop(cost):
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 2.0)
    await mgr.release("session:abc", cost)
    assert mgr.pending("session:abc") == 2.0


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


async def test_commit_moves_pending_to_consumed_by_default():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 3.0)
    await mgr.commit("session:abc", 3.0)
    assert mgr.pending("session:abc") == 0.0
    assert mgr.consumed("session:abc") == 3.0
    assert mgr.remaining("session:abc") == 2.0


async def test_commit_with_settle_pending_false_leaves_pending_untouched():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 3.0)
    await mgr.release("session:abc", 3.0)
    await mgr.commit("session:abc", 3.0, settle_pending=False)
    assert mgr.pending("session:abc") == 0.0
    assert mgr.consumed("session:abc") == 3.0


@pytest.mark.parametrize("cost", [0.0, -1.0])
async def test_commit_non_positive_is_a_noop(cost):
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.commit("session:abc", cost)
    assert mgr.consumed("session:abc") == 0.0


# ---------------------------------------------------------------------------
# settle() -- new coverage, not present in Chuzom's T2-M2 test file
# ---------------------------------------------------------------------------


async def test_settle_atomically_clears_pending_and_records_actual():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 2.0)
    await mgr.settle("session:abc", est_cost_usd=2.0, actual_cost_usd=2.5)
    assert mgr.pending("session:abc") == 0.0
    assert mgr.consumed("session:abc") == 2.5
    assert mgr.remaining("session:abc") == 2.5


async def test_settle_actual_cheaper_than_estimate_frees_the_difference():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 3.0)
    await mgr.settle("session:abc", est_cost_usd=3.0, actual_cost_usd=1.0)
    assert mgr.pending("session:abc") == 0.0
    assert mgr.consumed("session:abc") == 1.0
    assert mgr.remaining("session:abc") == 4.0


async def test_settle_both_zero_is_a_noop():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.settle("session:abc", est_cost_usd=0.0, actual_cost_usd=0.0)
    assert mgr.consumed("session:abc") == 0.0
    assert mgr.pending("session:abc") == 0.0


async def test_settle_closes_the_release_then_commit_race_window():
    """Regression test for the window RED1-7-01 (ported) closes: doing
    release() then commit(settle_pending=False) as two separate lock
    acquisitions would let a concurrent try_reserve slip in between them and
    observe pending already cleared but actual spend not yet recorded. settle()
    does both under a single lock acquisition, so a concurrent reserve that
    would only fit *after* the actual spend is recorded must still fail.
    """
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0)
    await mgr.try_reserve("session:abc", 2.0)  # pending=2.0, remaining=3.0

    async def settle_higher():
        # Real cost came in higher than estimated -- consumes 4.0 total,
        # leaving only 1.0 remaining once settled.
        await mgr.settle("session:abc", est_cost_usd=2.0, actual_cost_usd=4.0)

    async def reserve_too_much():
        # This must never be allowed to squeeze in seeing an intermediate
        # state where pending is already 0 but consumed isn't yet 4.0.
        return await mgr.try_reserve("session:abc", 2.0)

    results = await asyncio.gather(settle_higher(), reserve_too_much())
    reserve_ok = results[1]
    # Whichever interleaving happens, the final remaining must be consistent
    # with the sequential composition of both operations -- no over-reserve.
    assert mgr.consumed("session:abc") == 4.0
    if reserve_ok:
        assert mgr.pending("session:abc") == 2.0
        assert mgr.remaining("session:abc") == 0.0
    else:
        assert mgr.pending("session:abc") == 0.0
        assert mgr.remaining("session:abc") == 1.0


# ---------------------------------------------------------------------------
# parent-child cap propagation
# ---------------------------------------------------------------------------


async def test_child_reserve_debits_parent_too():
    mgr = be.get_manager()
    mgr.register("org:acme", cap_usd=10.0)
    mgr.register("session:abc", cap_usd=10.0, parents=("org:acme",))
    await mgr.try_reserve("session:abc", 4.0)
    assert mgr.pending("session:abc") == 4.0
    assert mgr.pending("org:acme") == 4.0
    assert mgr.remaining("org:acme") == 6.0


async def test_child_reserve_refused_when_parent_is_full():
    mgr = be.get_manager()
    mgr.register("org:acme", cap_usd=5.0)
    mgr.register("session:abc", cap_usd=100.0, parents=("org:acme",))
    assert await mgr.try_reserve("session:abc", 5.01) is False
    assert mgr.pending("session:abc") == 0.0
    assert mgr.pending("org:acme") == 0.0


async def test_child_reserve_refused_when_child_cap_too_small():
    mgr = be.get_manager()
    mgr.register("org:acme", cap_usd=100.0)
    mgr.register("session:abc", cap_usd=5.0, parents=("org:acme",))
    assert await mgr.try_reserve("session:abc", 5.01) is False
    assert mgr.pending("org:acme") == 0.0


async def test_child_release_and_commit_propagate_to_parent():
    mgr = be.get_manager()
    mgr.register("org:acme", cap_usd=10.0)
    mgr.register("session:abc", cap_usd=10.0, parents=("org:acme",))
    await mgr.try_reserve("session:abc", 4.0)
    await mgr.commit("session:abc", 4.0)
    assert mgr.consumed("session:abc") == 4.0
    assert mgr.consumed("org:acme") == 4.0
    assert mgr.pending("org:acme") == 0.0


async def test_transitive_grandparent_chain_is_debited():
    """Ported from Chuzom's RED1-6-01 regression: a naive walk visiting only
    direct parents misses grandparents. `grandparent` must be debited too."""
    mgr = be.get_manager()
    mgr.register("grandparent", cap_usd=10.0)
    mgr.register("parent", cap_usd=10.0, parents=("grandparent",))
    mgr.register("child", cap_usd=10.0, parents=("parent",))
    assert await mgr.try_reserve("child", 4.0) is True
    assert mgr.pending("grandparent") == 4.0
    assert mgr.pending("parent") == 4.0
    assert mgr.pending("child") == 4.0


async def test_chain_cycle_guard_does_not_infinite_loop():
    mgr = be.get_manager()
    mgr.register("a", cap_usd=10.0, parents=("b",))
    mgr.register("b", cap_usd=10.0, parents=("a",))
    # Must terminate and must still enforce both caps in the cycle.
    assert await mgr.try_reserve("a", 4.0) is True
    assert mgr.pending("a") == 4.0
    assert mgr.pending("b") == 4.0


async def test_unregistered_parent_is_skipped_silently():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=5.0, parents=("org:never-registered",))
    assert await mgr.try_reserve("session:abc", 4.0) is True
    assert mgr.get("org:never-registered") is None


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_reserves_against_tight_cap():
    """50 concurrent $1 reserves against a $10 cap must yield exactly 10
    successes and 40 refusals, with pending landing at exactly 10.0 -- no
    over-reserve, regardless of interleaving."""
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=10.0)

    async def reserve_one() -> bool:
        return await mgr.try_reserve("session:abc", 1.0)

    results = await asyncio.gather(*(reserve_one() for _ in range(50)))
    assert sum(1 for r in results if r) == 10
    assert sum(1 for r in results if not r) == 40
    assert mgr.pending("session:abc") == 10.0
    assert mgr.remaining("session:abc") == 0.0


# ---------------------------------------------------------------------------
# soft-cap / tier_state() -- new coverage, not present in Chuzom's T2-M2 file
# ---------------------------------------------------------------------------


def test_tier_state_for_unregistered_key_has_all_contract_keys_with_defaults():
    mgr = be.get_manager()
    state = mgr.tier_state("session:never-registered")
    assert set(state.keys()) == set(BUDGET_TIER_STATE_KEYS)
    assert state["cap_usd"] is None
    assert state["soft_cap_usd"] is None
    assert state["consumed_usd"] == 0.0
    assert state["pending_usd"] == 0.0
    assert state["remaining_usd"] == float("inf")
    assert state["usage_pct"] is None
    assert state["soft_breached"] is False


async def test_tier_state_reflects_consumed_and_pending():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=10.0, soft_cap_usd=8.0)
    await mgr.try_reserve("session:abc", 3.0)
    await mgr.commit("session:abc", 3.0)
    await mgr.try_reserve("session:abc", 2.0)
    state = mgr.tier_state("session:abc")
    assert set(state.keys()) == set(BUDGET_TIER_STATE_KEYS)
    assert state["cap_usd"] == 10.0
    assert state["soft_cap_usd"] == 8.0
    assert state["consumed_usd"] == 3.0
    assert state["pending_usd"] == 2.0
    assert state["remaining_usd"] == 5.0
    assert state["usage_pct"] == pytest.approx(0.5)
    assert state["soft_breached"] is False


async def test_soft_cap_breach_flips_tier_state_and_logs_once(monkeypatch):
    """`get_logger` returns a structlog logger, so `caplog` (which only hooks
    stdlib `logging` records) can't observe it unless `configure_logging()`
    has run. `structlog.testing.capture_logs()` would be the natural choice,
    but it isn't hermetic in this suite: `tests/commands/test_routing.py`
    replaces `sys.modules["structlog"]` with a bare `MagicMock()` at import
    time and never restores it, which permanently breaks
    `from structlog.testing import capture_logs` for any test collected
    afterwards (a pre-existing test-isolation gap, not touched here).
    Monkeypatching this module's own logger directly sidesteps that
    entirely and needs no real structlog internals."""
    records: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(be.log, "warning", lambda *a, **k: records.append((a, k)))

    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=10.0, soft_cap_usd=5.0)
    await mgr.try_reserve("session:abc", 5.0)  # crosses soft cap
    await mgr.try_reserve("session:abc", 1.0)  # stays breached
    assert mgr.tier_state("session:abc")["soft_breached"] is True
    # Rising edge only: exactly one warning, not one per call while breached.
    assert len(records) == 1
    assert "budget_soft_cap_breached" in records[0][0][0]


async def test_soft_cap_none_never_breaches():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=10.0)
    await mgr.try_reserve("session:abc", 10.0)
    assert mgr.tier_state("session:abc")["soft_breached"] is False


# ---------------------------------------------------------------------------
# get_manager() / reset_manager_for_tests() singleton semantics
# ---------------------------------------------------------------------------


def test_get_manager_returns_same_instance_until_reset():
    mgr1 = be.get_manager()
    mgr2 = be.get_manager()
    assert mgr1 is mgr2
    be.reset_manager_for_tests()
    mgr3 = be.get_manager()
    assert mgr3 is not mgr1


# ---------------------------------------------------------------------------
# flag semantics (LLM_ROUTER_BUDGET_ENVELOPE)
# ---------------------------------------------------------------------------


def test_flag_defaults_to_disabled():
    assert "LLM_ROUTER_BUDGET_ENVELOPE" not in os.environ
    assert be.budget_envelope_enabled() is False


@pytest.mark.parametrize("value", ["1", "on", "ON", "true", "True", "yes", "YES"])
def test_flag_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("LLM_ROUTER_BUDGET_ENVELOPE", value)
    assert be.budget_envelope_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no", "nope"])
def test_flag_falsy_values_disable(monkeypatch, value):
    monkeypatch.setenv("LLM_ROUTER_BUDGET_ENVELOPE", value)
    assert be.budget_envelope_enabled() is False


async def test_flag_on_off_single_threaded_equivalence():
    """The manager's own accounting behavior does not itself branch on the
    flag (the flag only gates *whether a caller wires this module in* --
    WS5 ships with no such wiring in router.py). This test pins that the
    manager's reserve/commit arithmetic is identical regardless of the flag's
    value, since nothing in this module reads the flag except
    `budget_envelope_enabled()` itself.
    """

    async def run_sequence() -> tuple[float, float, bool]:
        be.reset_manager_for_tests()
        mgr = be.get_manager()
        mgr.register("session:abc", cap_usd=5.0)
        ok = await mgr.try_reserve("session:abc", 2.0)
        await mgr.commit("session:abc", 2.0)
        return (mgr.consumed("session:abc"), mgr.remaining("session:abc"), ok)

    os.environ.pop("LLM_ROUTER_BUDGET_ENVELOPE", None)
    off_result = await run_sequence()

    os.environ["LLM_ROUTER_BUDGET_ENVELOPE"] = "1"
    try:
        on_result = await run_sequence()
    finally:
        os.environ.pop("LLM_ROUTER_BUDGET_ENVELOPE", None)

    assert off_result == on_result


# ---------------------------------------------------------------------------
# flag-off invariance: router.py is untouched by this module's mere existence
# ---------------------------------------------------------------------------


def test_importing_module_has_no_side_effects_on_router():
    """WS5 ships budget_envelope.py as a standalone module with zero wiring
    into router.py's existing `_pending_spend` reserve/release mechanism (see
    the migration plan's WS5 entry: "WS0 only; fully independent", and the
    "envelope-vs-pending-spend" ADR deferred to the program level). Importing
    this module must not touch, patch, or reference router.py's globals at
    all.
    """
    import llm_router.router as router_module

    assert not hasattr(router_module, "budget_envelope")
    assert not hasattr(router_module, "BudgetEnvelopeManager")


def test_router_source_does_not_reference_budget_envelope_module():
    import inspect as _inspect

    import llm_router.router as router_module

    source = _inspect.getsource(router_module)
    assert "budget_envelope" not in source
    assert "BudgetEnvelopeManager" not in source


# ---------------------------------------------------------------------------
# contract compliance
# ---------------------------------------------------------------------------


def test_tier_state_keys_match_frozen_contract():
    mgr = be.get_manager()
    mgr.register("session:abc", cap_usd=1.0)
    assert set(mgr.tier_state("session:abc").keys()) == set(BUDGET_TIER_STATE_KEYS)
    assert len(BUDGET_TIER_STATE_KEYS) == 7


def test_budget_envelope_dataclass_reused_from_contracts():
    # This module must reuse contracts.BudgetEnvelope, not redefine its own.
    assert be.BudgetEnvelope is BudgetEnvelope


def test_manager_exposes_exactly_the_frozen_api_surface():
    """Every method name referenced in BUDGET_ENVELOPE_API's 10
    BudgetEnvelopeManager signatures (the trailing 2 entries are the module
    functions get_manager/reset_manager_for_tests) must exist on the class
    with a matching async-ness."""
    manager_sigs = BUDGET_ENVELOPE_API[:10]
    module_fn_sigs = BUDGET_ENVELOPE_API[10:]
    assert len(manager_sigs) == 10
    assert len(module_fn_sigs) == 2

    for sig in manager_sigs:
        is_async = sig.startswith("async ")
        name = sig[len("async ") :] if is_async else sig
        name = name.split("(", 1)[0]
        attr = getattr(be.BudgetEnvelopeManager, name, None)
        assert attr is not None, f"missing method {name!r}"
        assert asyncio.iscoroutinefunction(attr) is is_async, (
            f"{name!r} async-ness mismatch (expected async={is_async})"
        )

    assert inspect.iscoroutinefunction(be.get_manager) is False
    assert inspect.iscoroutinefunction(be.reset_manager_for_tests) is False
    assert callable(be.get_manager)
    assert callable(be.reset_manager_for_tests)


# ---------------------------------------------------------------------------
# brand-leak
# ---------------------------------------------------------------------------


def test_no_chuzom_string_in_runtime_module_source_outside_provenance_header():
    """The provenance header/docstring are the only allowed "chuzom" mentions
    (per scripts/ci/check_identity.py's whole-file exemption for files matching
    'ported from chuzom'). This test independently asserts that no *runtime
    string value* (log messages, exception messages, dict/enum values) that
    the module could emit at runtime contains "chuzom" -- a stricter check
    than the identity gate, scoped to values a user could actually observe.
    """
    mgr = be.get_manager()
    env = mgr.register("session:abc", cap_usd=5.0, soft_cap_usd=1.0)
    assert "chuzom" not in repr(env).lower()

    state = mgr.tier_state("session:abc")
    assert "chuzom" not in repr(state).lower()

    with pytest.raises(ValueError) as exc_info:
        mgr.register("session:bad", cap_usd=-1.0)
    assert "chuzom" not in str(exc_info.value).lower()

    assert "chuzom" not in be.__name__.lower()
    for name in be.__all__:
        assert "chuzom" not in name.lower()
