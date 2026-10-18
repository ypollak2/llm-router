"""Deterministic per-door sampler for the judge queue (P3.1).

The old gate was ``random.random() > rate``: global, non-deterministic, and
blind to which door (proxy, sdk, ...) the call came through, so "10% per door"
could not be measured. This replaces it with systematic sampling per door:
the k-th eligible call on a door is picked when ``floor((k+o+1)*r) >
floor((k+o)*r)``. Over any window of ``ceil(1/r)`` consecutive calls on one
door exactly one is picked for r=0.1, and over any n calls the count is
within one of ``n*r``.

The offset ``o`` is a hash of the door (``sha256("judge:<door>:0")``), not of
the block. A per-block offset would let a window straddling two blocks hold
zero or two picks; a per-door constant keeps the picks exactly periodic while
still decorrelating the doors from each other.

Counters live in ``judge_sampler.json`` in the router state dir:
``{door: {seen, picked, lock_skipped}}``, updated under the shared file lock
with an atomic replace. O(1) per call. ``hook`` callers use a non-blocking
lock: on contention the call is skipped, counted in ``lock_skipped`` (held in
memory and flushed on the next locked update) and recorded via failopen.

Rate 0 keeps its historical meaning, "judge off". Any other value outside
(0, 1] or non-numeric falls back to the default, recorded via failopen.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from fractions import Fraction

from llm_router import failopen, paths
from llm_router.file_lock import exclusive_lock

DOORS = ("proxy", "sdk", "gateway", "mcp", "hook", "agent", "technical_ops")
UNKNOWN = "unknown"
DEFAULT_RATE = 0.1
_STATE_FILENAME = "judge_sampler.json"
_NONBLOCKING_DOORS = frozenset({"hook"})
_BLOCKING_TIMEOUT_S = 2.0

#: lock_skipped counts not yet persisted (the lock was the thing that failed).
_pending_skips: dict[str, int] = {}


def normalize_door(door: str | None) -> str:
    """Closed door set; anything else is reported as ``unknown``, never folded."""
    return door if door in DOORS else UNKNOWN


def sample_rate() -> float:
    """The configured rate: (0, 1], or 0.0 for an explicit "off"."""
    raw = os.environ.get("LLM_ROUTER_JUDGE_SAMPLE_RATE")
    if raw is None or raw.strip() == "":
        return DEFAULT_RATE
    try:
        rate = float(raw)
    except ValueError as exc:
        failopen.record("CHZ-FO-JUDGE-SAMPLE-RATE", exc, detail=f"unparseable rate {raw!r}")
        return DEFAULT_RATE
    if rate == 0.0:
        return 0.0
    if not (0.0 < rate <= 1.0) or math.isnan(rate):
        failopen.record("CHZ-FO-JUDGE-SAMPLE-RATE", None, detail=f"rate {raw!r} outside (0,1]")
        return DEFAULT_RATE
    return rate


def _state_path():
    return paths.state_path(_STATE_FILENAME)


def _is_pick(door: str, k: int, rate: float) -> bool:
    r = Fraction(str(rate)).limit_denominator(10_000)
    period = math.ceil(1 / r)
    offset = int.from_bytes(hashlib.sha256(f"judge:{door}:0".encode()).digest()[:8], "big") % period
    n = k + offset
    return (n + 1) * r.numerator // r.denominator > n * r.numerator // r.denominator


def _load(path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _load_for_update(path) -> dict:
    """The state to update under the lock. A corrupt or wrong-shaped file must not
    stop judging for good (rv-598): it is moved aside, recorded, and counting
    restarts from empty; door entries that are not count dicts are dropped."""
    try:
        state = _load(path)
    except (ValueError, UnicodeDecodeError) as exc:
        failopen.record("CHZ-FO-JUDGE-SAMPLER-CORRUPT", exc)
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError as move_exc:
            failopen.record("CHZ-FO-JUDGE-SAMPLER-CORRUPT", move_exc)
        return {}
    clean: dict = {}
    for d, c in state.items():
        if isinstance(c, dict):
            clean[d] = {k: v for k, v in c.items() if isinstance(v, int) and not isinstance(v, bool)}
        else:
            failopen.record("CHZ-FO-JUDGE-SAMPLER-CORRUPT", None, detail=f"door={d} entry dropped")
    return clean


def pick(door: str | None) -> bool:
    """Count one eligible call on ``door``; True if it is the sampled one.

    Never raises: any failure is recorded via failopen and the call is skipped.
    """
    rate = sample_rate()
    if rate <= 0.0:
        return False
    d = normalize_door(door)
    timeout = 0.0 if d in _NONBLOCKING_DOORS else _BLOCKING_TIMEOUT_S
    try:
        path = _state_path()
        status: dict = {}
        with exclusive_lock(path.with_name(path.name + ".lock"), timeout=timeout, status=status) as locked:
            if not locked:
                _pending_skips[d] = _pending_skips.get(d, 0) + 1
                failopen.record("CHZ-FO-JUDGE-SAMPLER-LOCK", None, detail=f"door={d} lock {status.get('state')}")
                return False
            state = _load_for_update(path)
            for skipped_door, n in _pending_skips.items():
                state.setdefault(skipped_door, {})["lock_skipped"] = (
                    state.get(skipped_door, {}).get("lock_skipped", 0) + n
                )
            _pending_skips.clear()
            c = state.setdefault(d, {})
            seen = c.get("seen", 0)
            hit = _is_pick(d, seen, rate)
            c["seen"] = seen + 1
            c["picked"] = c.get("picked", 0) + (1 if hit else 0)
            c.setdefault("lock_skipped", 0)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state))
            os.replace(tmp, path)
            return hit
    except Exception as exc:
        failopen.record("CHZ-FO-JUDGE-SAMPLER", exc)
        return False


def counters() -> dict | None:
    """Persisted per-door counters, or None if unreadable (unknown, not zero)."""
    try:
        return _load(_state_path())
    except Exception as exc:
        failopen.record("CHZ-FO-JUDGE-SAMPLER-READ", exc)
        return None


def record_result(
    *, door: str | None, answering_model: str, judge_model: str | None,
    status: str, score: float | None,
) -> None:
    """Append one text-free outcome row to ``judge_results.jsonl``."""
    import time
    row = {
        "ts": time.time(), "door": normalize_door(door),
        "answering_model": answering_model, "judge_model": judge_model,
        "status": status, "score": score,
    }
    try:
        path = paths.state_path("judge_results.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        failopen.record("CHZ-FO-JUDGE-RESULTS-WRITE", exc)
