"""Iteration 11 acceptance — SSE policy-change push (pub/sub core)."""
from __future__ import annotations

import asyncio

import pytest

from llm_router.control_plane.api import create_control_plane_app, publish_policy_change
from llm_router.control_plane.events import PolicyEventBus, get_event_bus, reset_event_bus_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_event_bus_for_tests()
    yield
    reset_event_bus_for_tests()


@pytest.mark.asyncio
async def test_publish_delivers_to_same_tenant_only() -> None:
    bus = PolicyEventBus()
    qa = bus.subscribe("t1")
    qb = bus.subscribe("t2")
    n = bus.publish("t1", {"version": 5})
    assert n == 1
    assert (await asyncio.wait_for(qa.get(), 1))["version"] == 5
    assert qb.empty()  # other tenant unaffected


@pytest.mark.asyncio
async def test_two_subscribers_same_tenant_both_receive() -> None:
    bus = PolicyEventBus()
    q1 = bus.subscribe("t1")
    q2 = bus.subscribe("t1")
    assert bus.publish("t1", {"v": 1}) == 2
    assert (await q1.get())["v"] == 1
    assert (await q2.get())["v"] == 1


@pytest.mark.asyncio
async def test_sequential_publishes_queue_in_order() -> None:
    bus = PolicyEventBus()
    q = bus.subscribe("t1")
    bus.publish("t1", {"version": 1})
    bus.publish("t1", {"version": 2})
    assert (await q.get())["version"] == 1
    assert (await q.get())["version"] == 2


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = PolicyEventBus()
    q = bus.subscribe("t1")
    bus.unsubscribe("t1", q)
    assert bus.subscriber_count("t1") == 0
    assert bus.publish("t1", {"v": 1}) == 0


@pytest.mark.asyncio
async def test_publish_policy_change_helper_delivers() -> None:
    bus = get_event_bus()
    q = bus.subscribe("t1")
    delivered = publish_policy_change("t1", 7, "digest7")
    assert delivered == 1
    evt = await asyncio.wait_for(q.get(), 1)
    assert evt["type"] == "policy_change" and evt["version"] == 7 and evt["digest"] == "digest7"


def test_sse_endpoint_registered() -> None:
    app = create_control_plane_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/cp/v1/tenants/{tenant_id}/policy/events" in paths
