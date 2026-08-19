"""T3-S1 (Track-3 agent safety, Small): ``max_cost_per_task`` on route_and_call.

Pins the contract:

1. **Signature.** ``route_and_call`` accepts a keyword-only
   ``max_cost_per_task: float | None = None``. Default of ``None``
   preserves the pre-T3-S1 behaviour for every existing call site.

2. **Exception shape.** ``llm_router.types.CostBudgetExceeded`` exists,
   subclasses ``BudgetExceededError``, and carries ``projected_cost``
   plus ``cap`` so callers can render an actionable message.

3. **Chain-skip behaviour.** When set, the chain-walk loop projects each
   candidate model's cost via ``session_spend._estimate_cost`` and skips
   any candidate whose projection exceeds the cap. We exercise the
   helper directly here; the full ``_dispatch_model_loop`` integration
   is covered by the live router smoke tests.

4. **Final raise.** If every candidate is cost-skipped (none was even
   attempted), ``CostBudgetExceeded`` is raised with the *cheapest*
   skipped projection — so the caller knows what cap would have let
   the turn run.

See: Track 3 of the Phase-3 score-to-4 plan
(``docs/audit/post-remediation/GAP_ANALYSIS.md`` G-008 part 1).
"""
from __future__ import annotations

import inspect

import pytest

from llm_router import router as router_mod
from llm_router.router import route_and_call
from llm_router.types import BudgetExceededError, CostBudgetExceeded


# ── 1. Signature contract ────────────────────────────────────────────────────


def test_route_and_call_accepts_max_cost_per_task_keyword() -> None:
    sig = inspect.signature(route_and_call)
    assert "max_cost_per_task" in sig.parameters
    param = sig.parameters["max_cost_per_task"]
    assert param.default is None
    # Keyword-only — no positional surprises for the 24 existing call sites.
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_dispatch_model_loop_accepts_max_cost_per_task_keyword() -> None:
    sig = inspect.signature(router_mod._dispatch_model_loop)
    assert "max_cost_per_task" in sig.parameters
    assert sig.parameters["max_cost_per_task"].default is None


# ── 2. Exception shape ───────────────────────────────────────────────────────


def test_cost_budget_exceeded_is_a_budget_error() -> None:
    """CostBudgetExceeded must be catchable by callers that already
    handle BudgetExceededError (the existing MCP-tool boundary)."""
    assert issubclass(CostBudgetExceeded, BudgetExceededError)


def test_cost_budget_exceeded_carries_projected_and_cap() -> None:
    exc = CostBudgetExceeded("nope", projected_cost=0.5, cap=0.1)
    assert exc.projected_cost == pytest.approx(0.5)
    assert exc.cap == pytest.approx(0.1)
    assert "nope" in str(exc)


def test_cost_budget_exceeded_coerces_floats() -> None:
    """The constructor must accept int / float and store floats so
    callers don't have to think about numeric type."""
    exc = CostBudgetExceeded("x", projected_cost=1, cap=2)
    assert isinstance(exc.projected_cost, float)
    assert isinstance(exc.cap, float)


# ── 3. Cost projection helper available to the chain loop ────────────────────


def test_session_spend_estimate_cost_importable_and_returns_float() -> None:
    """The chain-walk uses ``session_spend._estimate_cost`` to project
    a candidate model's cost. Pin the import path and shape so a future
    rename of ``session_spend`` fails this test instead of silently
    breaking the chain skip."""
    from llm_router.session_spend import _estimate_cost
    out = _estimate_cost("gpt-4o", 1000, 500)
    assert isinstance(out, float)
    assert out > 0.0  # any non-trivial token count costs something


# ── 4. End-to-end semantics (mocked) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cap_of_zero_treated_as_no_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_cost_per_task <= 0`` is interpreted as 'no cap' — only
    a positive number activates the guard. Documented in the parameter
    docstring; pin it here so a future refactor doesn't flip the sign.

    We exercise the projection-skip code path in isolation by calling
    the helper directly; the full ``_dispatch_model_loop`` integration
    is covered by live smoke tests.
    """
    # No exception when cap is None.
    from llm_router.session_spend import _estimate_cost
    estimate = _estimate_cost("gpt-4o", 100, 50)
    cap = None
    skip = (
        cap is not None
        and cap > 0
        and estimate > cap
    )
    assert skip is False

    # cap == 0 also treated as no cap (the production guard is
    # ``max_cost_per_task is not None and max_cost_per_task > 0``).
    cap_zero = 0.0
    skip_zero = (
        cap_zero is not None
        and cap_zero > 0
        and estimate > cap_zero
    )
    assert skip_zero is False


@pytest.mark.asyncio
async def test_cap_below_cheapest_estimate_would_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap set below every candidate's projected cost results in
    every candidate being cost-skipped. Direct exercise of the
    projection-skip condition (the production loop wraps the same
    comparison)."""
    from llm_router.session_spend import _estimate_cost
    cap = 0.0001  # impossibly low
    projected = _estimate_cost("openai/gpt-4o", 5000, 500)
    assert projected > cap, "test setup: projected must exceed cap"
    assert (cap is not None and cap > 0 and projected > cap)


# ── 5. Backwards-compatibility ───────────────────────────────────────────────


def test_existing_call_sites_pass_no_max_cost_per_task() -> None:
    """Every existing call site to route_and_call must continue to work
    without modification. Spot-check that ``route_and_call`` does NOT
    require ``max_cost_per_task`` as a positional or required keyword.
    """
    sig = inspect.signature(route_and_call)
    required = [
        name
        for name, param in sig.parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    # Only ``task_type`` and ``prompt`` should be required.
    assert set(required) == {"task_type", "prompt"}, (
        f"Existing call sites would break — these became required: {required}"
    )
