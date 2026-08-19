"""SUBSCRIPTION_LOCAL must actually reach the chain, not just exist as a module.

WHY THIS EXISTS
===============

``subscription_local_routing.py`` shipped complete: a documented module, a
``RoutingProfile.SUBSCRIPTION_LOCAL`` enum member, an activation gate, a
pressure lookup, a stable-sort reorder with three documented regimes, and
``tests/test_subscription_local_routing.py`` exercising all of it.

It had **no production caller**. Measured on ``main``::

    $ grep -rn "subscription_local_routing\\|reorder_for_subscription_local\\|
      is_subscription_local_active" src/llm_router/ | grep -v subscription_local_routing.py
    src/llm_router/types.py:107:  # ... (see subscription_local_routing.py)

One hit, and it is a comment. Selecting the profile did nothing.

This is the failure mode unit tests are worst at. Every test passed. The module
was correct. Nothing was broken *inside* the boundary the tests drew — the
boundary simply never touched the running system, so "SUBSCRIPTION_LOCAL works"
and "SUBSCRIPTION_LOCAL is reachable" were separate claims and only the first
was ever checked.

WHAT THIS ASSERTS
=================

1. The reorder is invoked from ``build_chain`` — the observable behaviour, via
   a spy, not by grepping for an import (a mention is not a call).
2. It is inert without ``LLM_ROUTER_SUBSCRIPTION_PROVIDER``, so existing installs
   see a byte-identical chain. That is what makes wiring it safe to do
   unconditionally rather than behind another flag.
3. It fails open: a raising reorder returns the unreordered chain rather than
   taking routing down.

CONTROL (re-run this if the test is edited)
===========================================

With the ``_apply_subscription_local`` calls removed from ``build_chain``::

    test_reorder_is_actually_invoked_when_active   FAILED  (spy never called)
    test_failure_in_the_reorder_does_not_break_routing  PASSED (vacuously)

The second passing on its own is the point: an "it doesn't crash" test stays
green against code that never runs. Only the spy discriminates.
"""

from __future__ import annotations

import pytest

from llm_router import chain_builder
from llm_router.types import RoutingProfile, TaskType


@pytest.fixture
def _no_discovery(monkeypatch):
    """Force the static path so the test does not depend on live discovery."""
    async def _empty():
        return {}

    import llm_router.discover as discover

    monkeypatch.setattr(discover, "discover_available_models", _empty)


@pytest.mark.asyncio
async def test_reorder_is_actually_invoked_when_active(monkeypatch, _no_discovery):
    """The call, observed. Not an import, not a name in the file — the call."""
    calls: list[dict] = []

    def _spy(chain, *, complexity, profile, subscription_pressure=None):
        calls.append(
            {
                "chain": list(chain),
                "complexity": complexity,
                "profile": profile,
                "pressure": subscription_pressure,
            }
        )
        return ["sentinel/reordered", *chain]

    async def _pressure():
        return 0.91

    import llm_router.subscription_local_routing as slr

    monkeypatch.setattr(slr, "is_subscription_local_active", lambda profile: True)
    monkeypatch.setattr(slr, "get_subscription_pressure", _pressure)
    monkeypatch.setattr(slr, "reorder_for_subscription_local", _spy)

    chain = await chain_builder.build_chain(
        TaskType.CODE, "moderate", RoutingProfile.SUBSCRIPTION_LOCAL
    )

    assert calls, (
        "build_chain never called reorder_for_subscription_local. The module "
        "is present and correct and completely unreachable — which is the "
        "state this test exists to detect."
    )
    assert calls[0]["pressure"] == 0.91, (
        "the reorder was called but not with the resolved subscription "
        "pressure, so its strained/unstrained regimes cannot fire"
    )
    assert chain[0] == "sentinel/reordered", (
        f"the reorder ran but its result was discarded; build_chain returned "
        f"{chain[:2]!r}"
    )


@pytest.mark.asyncio
async def test_inert_without_a_subscription_provider(monkeypatch, _no_discovery):
    """No opt-in, no change — this is what makes the wiring safe to ship on."""
    import llm_router.subscription_local_routing as slr

    monkeypatch.delenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", raising=False)

    called = False

    def _spy(chain, **kwargs):
        nonlocal called
        called = True
        return ["sentinel/should-not-appear", *chain]

    monkeypatch.setattr(slr, "reorder_for_subscription_local", _spy)

    for profile in (RoutingProfile.BALANCED, RoutingProfile.SUBSCRIPTION_LOCAL):
        chain = await chain_builder.build_chain(TaskType.CODE, "moderate", profile)
        assert "sentinel/should-not-appear" not in chain, (
            f"the reorder fired for {profile.value} with no "
            f"LLM_ROUTER_SUBSCRIPTION_PROVIDER set — existing installs would see "
            f"their chain order change without opting in"
        )
    assert not called


@pytest.mark.asyncio
async def test_failure_in_the_reorder_does_not_break_routing(monkeypatch, _no_discovery):
    """An un-reordered chain is still a working chain; no chain is an outage."""
    import llm_router.subscription_local_routing as slr

    def _boom(chain, **kwargs):
        raise RuntimeError("pressure backend exploded")

    async def _pressure():
        return 0.5

    monkeypatch.setattr(slr, "is_subscription_local_active", lambda profile: True)
    monkeypatch.setattr(slr, "get_subscription_pressure", _pressure)
    monkeypatch.setattr(slr, "reorder_for_subscription_local", _boom)

    chain = await chain_builder.build_chain(
        TaskType.CODE, "moderate", RoutingProfile.SUBSCRIPTION_LOCAL
    )
    assert len(chain) >= 2, (
        f"a failing reorder collapsed the chain to {chain!r} — the fail-open "
        f"path must return the chain it was handed"
    )
