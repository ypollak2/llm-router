"""Stamp ``layer``, ``confidence``, ``reason`` and ``inventory_sig`` on a routing decision row (W0-1).

Every writer of a routing decision (``router.route_and_call``, the DIRECT hook / SDK / agent-route
path in ``hooks.savings_logger``, the error row in ``cost``, the proxy ledger) takes these four
values from here, so "routes to an ineligible model" and the ``llm-router why`` layer are countable.

* ``layer``: which classifier layer decided. The engine's own layer (``L1``, ``L3``, ``cache``,
  ``fallback``...) when the engine decided, otherwise the classifier label the caller already has
  (``heuristic``, ``llm``, ``cached``, ``override``, ``fallback``, ``prefix``). The set is closed
  (:data:`LAYERS`); a door name (``agent-route``) or a method / fast-path name is NULL, not a layer.
  ``unhinted`` = the caller named the task itself, so no classifier ran.
* ``confidence``: the classifier's own number, or None. Never fabricated: ``0.0`` is the
  classifier's "nothing succeeded" value and an override / fallback / unhinted call has none, so all
  of those are NULL.
* ``inventory_sig``: sha256[:12] of the resource map's inventory, resolvable to that inventory. NULL = no
  usable map. Quota readings and timestamps are left out, so it moves when the inventory changes.

``inventory_sig`` is the hash of the inventory's EFFECTIVE facts: per resource its chain id, status,
effective priority, capabilities (tools, vision) and context window as ``map_reader`` indexes them for
the eligibility filter, plus the routing policy. A signature alone cannot say which models were
eligible, so the first time a signature is computed its canonical body is stored in the
``inventory_snapshots`` table of ``usage.db`` (:func:`resolve_inventory` reads it back). A snapshot is
never deleted: ``routing_decisions`` has no TTL either (see ``docs/RETENTION.md``), and a snapshot is
one small row per distinct inventory, so every row's signature stays resolvable.

The signature is cached per process and keyed on ``map_reader.token()`` (the map's mtime, itself
cached), so a map rewrite changes the signature on the very next call; a warm call costs one ``stat``
and no read. A cold call is one ``map_reader.load_data`` plus, when the signature is new to
``usage.db``, one insert. Fail-open: any error returns None and is never raised into a routing path.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time

from llm_router.decision_ledger import LAYERS as _LEDGER_LAYERS

#: Layers a routing row may name. The ledger's own layers, plus the classifier labels that really
#: decide a hook / SDK / proxy turn. Anything else (a door name such as ``agent-route``, a fast-path
#: or method name) is NOT a layer and is stored as NULL.
LAYERS = _LEDGER_LAYERS + ("heuristic", "llm", "cached", "override", "unhinted", "rules", "knn")
#: Labels with no classifier confidence by definition.
_NO_CONFIDENCE = frozenset({"override", "fallback", "unhinted", "prefix"})

_SNAPSHOT_DDL = (
    "CREATE TABLE IF NOT EXISTS inventory_snapshots ("
    "sig TEXT PRIMARY KEY, first_seen_ts TEXT NOT NULL, body_json TEXT NOT NULL)"
)

_lock = threading.Lock()
_cache: dict = {"key": None, "token": None, "sig": None}


def _body_of(data: dict) -> dict:
    """The canonical, JSON-able inventory the signature hashes and the snapshot stores."""
    from llm_router import map_reader

    rows = []
    for r in data.get("resources") or []:
        if not isinstance(r, dict):
            continue
        caps: dict = {}
        window: dict = {}
        map_reader._index_caps(r, caps, {}, window)  # the same facts the eligibility filter reads
        cid = r.get("chain_id")
        pair = caps.get(cid)
        rows.append({"chain_id": str(cid), "status": str(r.get("status")),
                     "effective_priority": r.get("effective_priority"),
                     "tools": pair[0] if pair else None, "vision": pair[1] if pair else None,
                     "window": window.get(cid)})
    rows.sort(key=lambda x: (x["chain_id"], x["status"]))
    return {"policy": data.get("policy"), "rows": rows}


def _canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _sig_of(data: dict) -> str:
    return hashlib.sha256(_canonical(_body_of(data)).encode("utf-8")).hexdigest()[:12]


def _db_file(db_path=None):
    if db_path is not None:
        return db_path
    from llm_router.config import get_config

    return get_config().llm_router_db_path


def _store_snapshot(sig: str, body: dict, db_path=None) -> None:
    """INSERT the snapshot once (a SELECT first, so a known signature costs no write lock)."""
    from llm_router import state_files

    conn = state_files.connect_sqlite(_db_file(db_path), timeout=2.0)
    try:
        conn.execute(_SNAPSHOT_DDL)
        if conn.execute("SELECT 1 FROM inventory_snapshots WHERE sig = ?", (sig,)).fetchone() is None:
            conn.execute("INSERT OR IGNORE INTO inventory_snapshots (sig, first_seen_ts, body_json) VALUES (?, ?, ?)",
                         (sig, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), _canonical(body)))
            conn.commit()
    finally:
        conn.close()


def resolve_inventory(sig: str | None, db_path=None) -> dict | None:
    """The inventory a signature hashed (``{"policy", "rows": [...]}``), or None when unknown."""
    if not isinstance(sig, str) or not sig:
        return None
    try:
        from llm_router import state_files

        file = _db_file(db_path)
        if not os.path.exists(file):
            return None
        conn = state_files.connect_sqlite(file, timeout=2.0)
        try:
            row = conn.execute("SELECT body_json FROM inventory_snapshots WHERE sig = ?", (sig,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001 - no table / no file / unreadable: the signature is unknown
        return None


def inventory_sig(path=None, db_path=None) -> str | None:
    """The cached inventory signature; None when there is no usable resource map."""
    try:
        from llm_router import map_reader

        key = str(path) if path is not None else ""
        token = map_reader.token(path)
        if token is None:
            return None
        with _lock:
            if _cache["key"] == key and _cache["token"] == token:
                return _cache["sig"]
        data = map_reader.load_data(path)
        if not isinstance(data, dict):
            return None
        body = _body_of(data)
        sig = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:12]
        try:
            _store_snapshot(sig, body, db_path)
        except Exception as exc:  # noqa: BLE001 - an unresolvable sig is recorded, never a lost row
            from llm_router import failopen

            failopen.record("W01-SNAPSHOT", exc)
        with _lock:
            _cache.update(key=key, token=token, sig=sig)
        return sig
    except Exception:  # noqa: BLE001 - a stamp must never cost a routing row
        return None


def reset_cache() -> None:
    with _lock:
        _cache.update(key=None, token=None, sig=None)


def layer_for(label: str | None) -> str | None:
    """The layer for a classifier label; None when it has none or is not a layer (closed set :data:`LAYERS`)."""
    if not isinstance(label, str) or not label.strip():
        return None
    s = label.strip()
    if s.startswith("prefix:"):
        return "prefix"
    s = s.split("+", 1)[0]
    return s if s in LAYERS else None


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
