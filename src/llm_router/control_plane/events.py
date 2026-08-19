"""In-process per-tenant policy-change event bus.

The control plane publishes a policy-change event when a new version is
activated; per-tenant sidecars subscribe over SSE and pull the new bundle
immediately (the fast path that meets the <5s distribution SLO). This module
is the pub/sub core — testable without any HTTP streaming.
"""
from __future__ import annotations

import asyncio
import threading

__all__ = ["PolicyEventBus", "get_event_bus", "reset_event_bus_for_tests"]


class PolicyEventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, tenant_id: str, maxsize: int = 100) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.setdefault(tenant_id, set()).add(q)
        return q

    def unsubscribe(self, tenant_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subs.get(tenant_id)
            if subs and q in subs:
                subs.remove(q)
                if not subs:
                    del self._subs[tenant_id]

    def publish(self, tenant_id: str, event: dict) -> int:
        # Snapshot the subscriber set under the lock so concurrent
        # subscribe/unsubscribe can't mutate it mid-iteration.
        with self._lock:
            subs = list(self._subs.get(tenant_id, ()))
        delivered = 0
        for q in subs:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                continue  # best-effort: never block on a slow subscriber
        return delivered

    def subscriber_count(self, tenant_id: str) -> int:
        with self._lock:
            return len(self._subs.get(tenant_id, ()))


_bus: PolicyEventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> PolicyEventBus:
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = PolicyEventBus()
        return _bus


def reset_event_bus_for_tests() -> None:
    global _bus
    with _bus_lock:
        _bus = PolicyEventBus()
