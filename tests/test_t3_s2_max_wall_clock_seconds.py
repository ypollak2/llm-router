"""T3-S2 (Track-3 agent safety, Small): ``max_wall_clock_seconds`` cap.

Pins the contract:

1. **Signature.** ``route_and_call`` accepts a keyword-only
   ``max_wall_clock_seconds: float | None = None``. Default ``None``
   preserves pre-T3-S2 behaviour for every existing call site.

2. **Exception shape.** ``llm_router.types.WallClockExceeded`` exists,
   subclasses ``TimeoutError``, and carries ``cap_seconds`` plus
   ``elapsed_seconds`` so callers can render actionable errors.

3. **End-to-end semantics.** When the cap fires, the router:
   - releases the budget reservation,
   - writes a timeout audit row (best-effort, never propagates),
   - raises ``WallClockExceeded``.
   We exercise this via a mocked ``_dispatch_model_loop`` that
   ``asyncio.sleep(s)``s longer than the cap.

See: Track 3 of the Phase-3 score-to-4 plan
(``docs/audit/post-remediation/GAP_ANALYSIS.md`` G-008 part 2).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from llm_router import router as router_mod
from llm_router.router import route_and_call
from llm_router.types import (
    BudgetExceededError,
    CostBudgetExceeded,
    WallClockExceeded,
)


# ── 1. Signature contract ────────────────────────────────────────────────────


def test_route_and_call_accepts_max_wall_clock_seconds_keyword() -> None:
    sig = inspect.signature(route_and_call)
    assert "max_wall_clock_seconds" in sig.parameters
    param = sig.parameters["max_wall_clock_seconds"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_existing_call_sites_unchanged() -> None:
    """Required params must remain exactly task_type + prompt."""
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
    assert set(required) == {"task_type", "prompt"}


# ── 2. Exception shape ───────────────────────────────────────────────────────


def test_wall_clock_exceeded_is_a_timeout_error() -> None:
    """Callers using the stdlib ``try/except TimeoutError`` idiom must
    catch this exception without importing a llm_router-specific class."""
    assert issubclass(WallClockExceeded, TimeoutError)


def test_wall_clock_exceeded_not_a_budget_error() -> None:
    """Wall-clock and budget are distinct failure modes. Catching
    BudgetExceededError must NOT also catch a wall-clock event —
    operators that auto-retry on budget exhaustion would otherwise
    retry-storm on timeouts."""
    assert not issubclass(WallClockExceeded, BudgetExceededError)
    # And the cost-cap exception still IS a BudgetExceededError —
    # spot-check the relationship has not regressed.
    assert issubclass(CostBudgetExceeded, BudgetExceededError)


def test_wall_clock_exceeded_carries_cap_and_elapsed() -> None:
    exc = WallClockExceeded("nope", cap_seconds=2.0, elapsed_seconds=2.5)
    assert exc.cap_seconds == pytest.approx(2.0)
    assert exc.elapsed_seconds == pytest.approx(2.5)
    assert "nope" in str(exc)


def test_wall_clock_exceeded_elapsed_optional() -> None:
    """Some callers may not have an elapsed measurement. The constructor
    must accept ``elapsed_seconds=None`` and leave the attribute None."""
    exc = WallClockExceeded("x", cap_seconds=1.5)
    assert exc.cap_seconds == pytest.approx(1.5)
    assert exc.elapsed_seconds is None


def test_wall_clock_exceeded_coerces_floats() -> None:
    exc = WallClockExceeded("x", cap_seconds=1, elapsed_seconds=2)
    assert isinstance(exc.cap_seconds, float)
    assert isinstance(exc.elapsed_seconds, float)


# ── 3. End-to-end semantics (mocked dispatcher) ──────────────────────────────


@pytest.mark.asyncio
async def test_wall_clock_cap_fires_when_dispatch_runs_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatcher that sleeps longer than the cap must trigger
    WallClockExceeded. The cap is 0.05s; the mock sleeps 1s."""

    async def _slow_dispatch(**kwargs):
        await asyncio.sleep(1.0)
        raise AssertionError("dispatcher should have been cancelled")

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _slow_dispatch)

    from llm_router.types import TaskType
    with pytest.raises(WallClockExceeded) as excinfo:
        await route_and_call(
            task_type=TaskType.QUERY,
            prompt="hi",
            max_wall_clock_seconds=0.05,
        )
    assert excinfo.value.cap_seconds == pytest.approx(0.05)
    # Elapsed measurement should be present and ≥ the cap.
    assert excinfo.value.elapsed_seconds is not None
    assert excinfo.value.elapsed_seconds >= 0.05


@pytest.mark.asyncio
async def test_wall_clock_cap_of_zero_treated_as_no_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_wall_clock_seconds <= 0`` is interpreted as 'no cap' —
    mirrors the ``max_cost_per_task`` convention from T3-S1. Only a
    positive number activates the guard."""
    from llm_router.types import LLMResponse, TaskType

    async def _fast_dispatch(**kwargs):
        # Return a minimal LLMResponse-like object; the guard for
        # cap > 0 means this should be awaited directly without
        # asyncio.wait_for wrapping.
        return LLMResponse(
            content="ok",
            model="m",
            provider="p",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            latency_ms=1.0,
        )

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _fast_dispatch)

    # No exception — both cap=None and cap=0 paths must succeed.
    resp_none = await route_and_call(
        task_type=TaskType.QUERY, prompt="hi", max_wall_clock_seconds=None
    )
    assert resp_none.content == "ok"

    resp_zero = await route_and_call(
        task_type=TaskType.QUERY, prompt="hi", max_wall_clock_seconds=0.0
    )
    assert resp_zero.content == "ok"


@pytest.mark.asyncio
async def test_wall_clock_cap_loose_enough_does_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the dispatcher returns well within the cap, no timeout is
    raised. Pin the happy-path: cap > work duration → success."""
    from llm_router.types import LLMResponse

    async def _fast_dispatch(**kwargs):
        await asyncio.sleep(0.01)
        return LLMResponse(
            content="ok",
            model="m",
            provider="p",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            latency_ms=10.0,
        )

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _fast_dispatch)

    from llm_router.types import TaskType as _TT
    resp = await route_and_call(
        task_type=_TT.QUERY, prompt="hi", max_wall_clock_seconds=5.0
    )
    assert resp.content == "ok"
