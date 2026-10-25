"""Stamp ``layer``, ``confidence``, ``reason`` and ``inventory_sig`` on a routing decision row (W0-1).

Every writer of a routing decision (``router.route_and_call``, the DIRECT hook / SDK / agent-route
path in ``hooks.savings_logger``, the error row in ``cost``, the proxy ledger) takes these four
values from here, so "routes to an ineligible model" and the ``llm-router why`` layer are countable.

* ``layer``: which classifier layer decided. The engine's own layer (``L1``, ``L3``, ``cache``,
  ``fallback``...) when the engine decided, otherwise the classifier label the caller already has
  (``heuristic``, ``llm``, ``cached``, ``override``, ``fallback``, a fast-path name, ``prefix``).
  ``unhinted`` = the caller named the task itself, so no classifier ran.
* ``confidence``: the classifier's own number, or None. Never fabricated: ``0.0`` is the
  classifier's "nothing succeeded" value and an override / fallback / unhinted call has none, so all
  of those are NULL.
* ``inventory_sig``: sha256[:12] of the resource map's inventory (which resources are connected, and
  their priority order, plus the routing policy). Quota readings and timestamps are left out, so the
  signature moves when the inventory changes, not on every refresh. NULL = no usable map.

``inventory_sig`` is cached for :data:`TTL_S` seconds of process time, so a warm call does no file
I/O at all (not even a ``stat``); a cold call is one ``map_reader.load_data`` (itself mtime-cached).
Fail-open: any error returns None and is never raised into a routing path.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time

#: How long a computed signature is trusted before the map file is looked at again.
TTL_S = 30.0
#: Labels with no classifier confidence by definition.
_NO_CONFIDENCE = frozenset({"override", "fallback", "unhinted", "prefix"})

_lock = threading.Lock()
_cache: dict = {"key": None, "until": 0.0, "sig": None}


def _sig_of(data: dict) -> str:
    rows = sorted((str(r.get("chain_id")), str(r.get("status")), r.get("effective_priority"))
                  for r in data.get("resources") or [] if isinstance(r, dict))
    blob = json.dumps({"policy": data.get("policy"), "rows": rows}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def inventory_sig(path=None) -> str | None:
    """The cached inventory signature; None when there is no usable resource map."""
    try:
        key = str(path) if path is not None else ""
        now = time.monotonic()
        with _lock:
            if _cache["key"] == key and now < _cache["until"]:
                return _cache["sig"]
        from llm_router import map_reader

        data = map_reader.load_data(path)
        sig = _sig_of(data) if isinstance(data, dict) else None
        with _lock:
            _cache.update(key=key, until=now + TTL_S, sig=sig)
        return sig
    except Exception:  # noqa: BLE001 - a stamp must never cost a routing row
        return None


def reset_cache() -> None:
    with _lock:
        _cache.update(key=None, until=0.0, sig=None)


def layer_for(label: str | None) -> str | None:
    """The layer name for a classifier label; None when the caller has none."""
    if not isinstance(label, str) or not label.strip():
        return None
    s = label.strip()
    if s.startswith("prefix:"):
        return "prefix"
    return s.split("+", 1)[0][:48]


def confidence_for(layer: str | None, confidence) -> float | None:
    """``confidence`` as measured, or None: not a number, not in (0, 1], or a layer that has none."""
    if layer in _NO_CONFIDENCE or isinstance(confidence, bool):
        return None
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return None
    return c if math.isfinite(c) and 0.0 < c <= 1.0 else None


def stamp(label: str | None, confidence=None, reason: str | None = None, *, layer: str | None = None) -> dict:
    """Keyword arguments for ``cost.log_routing_decision``: layer, confidence, reason, inventory_sig."""
    lyr = layer_for(layer) or layer_for(label)
    return {"layer": lyr, "confidence": confidence_for(lyr, confidence), "reason": reason,
            "inventory_sig": inventory_sig()}
