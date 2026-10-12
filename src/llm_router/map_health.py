"""Per-resource model health for the resource map."""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE = "temporarily_unavailable"
WINDOW_S = 60
FAILURES_TO_ENTER = 3
BASE_BACKOFF_S = 300
MAX_BACKOFF_S = 3600
PROBATION_S = 120
PROBATION_SUCCESSES = 3
ABSENT_AFTER_S = 86400


def _health(row: dict, *, create: bool = False) -> dict | None:
    h = row.get("health")
    if not isinstance(h, dict):
        if not create:
            return None
        h = {}
        row["health"] = h
    h.setdefault("failures", [])
    h.setdefault("backoff_s", 0)
    h.setdefault("previous_status", None)
    h.setdefault("since", None)
    h.setdefault("probation_until", None)
    h.setdefault("probation_successes", 0)
    h.setdefault("absent_for_compile", False)
    return h


def _clear(row: dict) -> None:
    h = row["health"]
    h["probation_until"] = None
    h["probation_successes"] = 0
    h["backoff_s"] = 0
    h["since"] = None
    h["absent_for_compile"] = False


def _enter(row: dict, now: float, *, backoff: float | None = None) -> str:
    h = _health(row, create=True)
    assert h is not None
    already = row.get("status") == STATE
    if h.get("previous_status") is None and not already:
        h["previous_status"] = row.get("status")
    if h.get("since") is None:
        h["since"] = now
    b = int(backoff)
    b = max(BASE_BACKOFF_S, min(MAX_BACKOFF_S, b))
    h["backoff_s"] = b
    h["failures"] = []
    h["probation_until"] = None
    h["probation_successes"] = 0
    row["status"] = STATE
    row["unavailable_until"] = now + b
    return "reenter" if already else "enter"


def _in_probation(h: dict, now: float) -> bool:
    until = h.get("probation_until")
    return isinstance(until, (int, float)) and now < float(until)


def _expire_probation_if_needed(h: dict, now: float) -> None:
    until = h.get("probation_until")
    if isinstance(until, (int, float)) and now >= float(until):
        h["probation_until"] = None
        h["probation_successes"] = 0
        h["backoff_s"] = 0


def record(row: dict, ok: bool, now: float) -> str | None:
    """Record one outcome into a map row's pure state machine."""
    if row.get("status") == STATE:
        return None
    h = _health(row, create=not ok)
    if h is None:
        return None
    if _in_probation(h, now):
        if ok:
            h["probation_successes"] = int(h.get("probation_successes") or 0) + 1
            if h["probation_successes"] >= PROBATION_SUCCESSES:
                _clear(row)
                return "probation_end"
            return None
        prev = int(h.get("backoff_s") or BASE_BACKOFF_S)
        _enter(row, now, backoff=min(MAX_BACKOFF_S, max(BASE_BACKOFF_S, prev * 2)))
        return "reenter"
    _expire_probation_if_needed(h, now)
    if ok:
        return None
    failures = [float(t) for t in h.get("failures", []) if isinstance(t, (int, float))]
    failures = [t for t in failures if now - t <= WINDOW_S]
    failures.append(now)
    h["failures"] = failures
    if len(failures) >= FAILURES_TO_ENTER:
        return _enter(row, now, backoff=BASE_BACKOFF_S)
    return None


def try_leave(row: dict, now: float, check_ok: bool) -> bool:
    """Leave temporary unavailability after the backoff if a live check passes."""
    until = row.get("unavailable_until")
    if row.get("status") != STATE or not isinstance(until, (int, float)) or until > now:
        return False
    if not check_ok:
        return False
    h = _health(row, create=True)
    assert h is not None
    row["status"] = h.get("previous_status") or "connected"
    row["unavailable_until"] = None
    h["previous_status"] = None
    h["probation_until"] = now + PROBATION_S
    h["probation_successes"] = 0
    return True


def is_routable(row: dict) -> bool:
    return row.get("status") != STATE


def mark_absent_for_compile(row: dict, now: float) -> bool:
    h = _health(row)
    if h is None:
        return False
    old = bool(h.get("absent_for_compile"))
    h["absent_for_compile"] = (row.get("status") == STATE and h.get("since") is not None
                               and now - float(h["since"]) >= ABSENT_AFTER_S)
    return bool(h["absent_for_compile"]) != old


def _provider(model_id: str) -> str:
    return model_id.split("/", 1)[0]


def _find_row(data: dict, model_id: str) -> dict | None:
    provider = _provider(model_id)
    for r in data.get("resources", []):
        if r.get("kind") in ("local", "api") and r.get("chain_id") == model_id:
            return r
    for r in data.get("resources", []):
        if r.get("kind") == "api" and r.get("chain_id") == f"{provider}/api":
            return r
    return None


def record_outcome(model_id: str, ok: bool, *, now: float | None = None, path: Path | None = None) -> str | None:
    """Persist one row outcome into resource_map.json. Fail-open on all errors."""
    try:
        from llm_router import resource_map
        from llm_router.file_lock import exclusive_lock

        p = path or resource_map.map_path()
        initial = resource_map.load_map(p)
        if initial is None or _find_row(initial, model_id) is None:
            return None
        with exclusive_lock(p.with_suffix(".lock"), timeout=2.0) as locked:
            if not locked:
                raise TimeoutError("resource map health lock timed out")
            data = resource_map.load_map(p)
            if data is None:
                return None
            row = _find_row(data, model_id)
            if row is None:
                return None
            before = json.dumps(row, sort_keys=True)
            event = record(row, ok, time.time() if now is None else now)
            after = json.dumps(row, sort_keys=True)
            if before != after:
                resource_map.write_map(data, p)
            return event
    except Exception as exc:  # noqa: BLE001
        try:
            from llm_router import failopen
            failopen.record("CHZ-FO-MAPHEALTH-RECORD", exc)
        except Exception:  # noqa: BLE001
            pass
        return None
