"""Read side of the resource map: what the routing paths ask of it (P1.8 task 6).

The router, the hooks, the proxy and the dynamic table only ever READ
``resource_map.json``; the map is built by ``llm-router map`` or the MCP
``llm_router_status(view="map")`` door (:mod:`llm_router.resource_map`).

Every function here is fail-open. A missing, unreadable, other-schema or stale
(older than :data:`MAX_AGE_S`) file gives ``None`` and the caller keeps today's
chain unchanged, so an install that has never written a map routes exactly as
before. A filter that would empty a non-empty chain is not applied.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")

# Readers never rebuild, so this is not the 300 s refresh TTL: it only bounds how
# long a map nobody refreshes may keep steering routing.
MAX_AGE_S = 6 * 3600
_FUTURE_SKEW_S = 300

# profile.yaml tier name -> map row kind
TIER_KINDS = {"free_local": "local", "free_subscriptions": "subscription", "cheap_apis": "api"}


@dataclass(frozen=True)
class MapView:
    """What a chain needs from the map, indexed once per file version."""
    known: frozenset          # providers with at least one row, any status
    connected: frozenset      # providers with at least one connected row
    kinds: frozenset          # kinds with at least one connected row
    rank_by_id: dict          # chain_id -> effective_priority (connected rows)
    rank_by_provider: dict    # provider -> best effective_priority

    def provider_ok(self, provider: str) -> bool:
        """False only when the map knows the provider and none of its rows is connected."""
        return provider not in self.known or provider in self.connected

    def rank(self, model_id: str) -> int | None:
        if model_id in self.rank_by_id:
            return self.rank_by_id[model_id]
        return self.rank_by_provider.get(_provider(model_id))


def _provider(model_id: str) -> str:
    return model_id.split("/", 1)[0]


def _index(data: dict) -> MapView:
    known, connected, kinds = set(), set(), set()
    by_id: dict[str, int] = {}
    by_prov: dict[str, int] = {}
    for r in data["resources"]:
        prov = _provider(r["chain_id"])
        known.add(prov)
        if r["status"] != "connected":
            continue
        connected.add(prov)
        kinds.add(r["kind"])
        p = r.get("effective_priority")
        if isinstance(p, int):
            by_id[r["chain_id"]] = p
            by_prov[prov] = min(p, by_prov.get(prov, p))
    return MapView(frozenset(known), frozenset(connected), frozenset(kinds), by_id, by_prov)


_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int], dict, MapView | None]] = {}


def _path(path: Path | None) -> Path:
    if path is not None:
        return path
    from llm_router import resource_map
    return resource_map.map_path()


def token(path: Path | None = None) -> int | None:
    """The map file's mtime in ns (``None`` when absent): changes when the map is rewritten."""
    try:
        return _path(path).stat().st_mtime_ns
    except Exception:
        return None


def load_view(path: Path | None = None, now: float | None = None) -> MapView | None:
    """The fresh map as a :class:`MapView`, re-read only when the file's mtime changes."""
    try:
        from llm_router import resource_map as rm

        p = _path(path)
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
        with _lock:
            hit = _cache.get(str(p))
        if hit is not None and hit[0] == sig:
            data, view = hit[1], hit[2]
        else:
            data = rm.load_map(p)
            view = None
            if data is not None:
                try:
                    view = _index(data)
                except (KeyError, TypeError, AttributeError):
                    view = None
            with _lock:
                _cache[str(p)] = (sig, data, view)
        if data is None or view is None:
            return None
        age = rm.age_seconds(data, now)
        if age is None or age > MAX_AGE_S or age < -_FUTURE_SKEW_S:
            return None
        return view
    except Exception:
        return None


def apply_to_chain(
    chain: Sequence[T],
    key: Callable[[T], str] = lambda m: m,  # type: ignore[assignment,return-value]
    *,
    reorder: bool = True,
    view: MapView | None = None,
) -> list[T]:
    """*chain* without entries whose resource is not connected, in map priority order.

    ``key`` gives the ``provider/model`` id of an entry. Entries of providers the
    map has no row for are left alone; ranked entries trade places among the slots
    ranked entries already hold, so unranked entries stay where they were. With no
    usable map, or if the filter would empty the chain, the chain is returned as is.
    """
    out = list(chain)
    try:
        v = view or load_view()
        if v is None or not out:
            return out
        kept = [m for m in out if v.provider_ok(_provider(key(m)))]
        if not kept:
            return out
        if reorder:
            ranks = [v.rank(key(m)) for m in kept]
            slots = [i for i, r in enumerate(ranks) if r is not None]
            ordered = sorted((kept[i] for i in slots), key=lambda m: v.rank(key(m)))
            for i, m in zip(slots, ordered):
                kept[i] = m
        return kept
    except Exception:
        return list(chain)


def filter_providers(providers: set[str]) -> set[str]:
    """*providers* minus those the map knows and has no connected row for."""
    v = load_view()
    if v is None:
        return providers
    kept = {p for p in providers if v.provider_ok(p)}
    return kept or providers
