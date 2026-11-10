"""P22 cold-start rule (flag ``LLM_ROUTER_COLD_GUARD``, off by default).

A live turn whose prefix is not warm on the local runner and whose predicted prefill (deadline.py, the same
math the size-aware deadline uses) exceeds the deadline cap goes to Claude for that turn only, and a
prefill-only warm-up (num_predict 1, same model and num_ctx as live turns, background queue priority) is
started so a later turn of the conversation finds the prefix warm and goes local.

Warmth: the runner has one slot (NUM_PARALLEL=1), so one prefix is resident, the last one served locally or
warmed. A turn is warm when that prefix (system+tools hash, per-message hashes from prefix_hash) is a prefix
of the turn's own messages. Per-process knowledge: after a proxy restart everything is cold, which costs one
Claude turn, never a wrong answer.
"""
from __future__ import annotations

import os
import re
import threading
import time

from llm_router.proxy import prefix_hash
from llm_router.proxy.local_mode import REASON_COLD_OVER_DEADLINE as REASON  # noqa: F401 -- one source for the reason

ENV_FLAG = "LLM_ROUTER_COLD_GUARD"
#: Resident-prefix lifetime when the backend keep_alive is not a finite duration (-1, None, unparsable).
DEFAULT_TTL_S = 1800.0
#: After a failed warm-up the same prefix key is not tried again for this long.
FAIL_BACKOFF_S = 600.0
#: A warm-up prefill may run this long at most (nobody waits for it; it occupies the single slot meanwhile).
WARM_MAX_S = 1800.0
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def enabled() -> bool:
    # literal name below: env_registry's scan needs it
    return os.environ.get("LLM_ROUTER_COLD_GUARD", "").strip().lower() in ("1", "on", "true", "yes")


def ttl_from_keep_alive(keep_alive) -> float:
    """Seconds the runner keeps its model (and prefix) after the last call. 0 (or any non-forever value of zero
    length) means unload at once: TTL 0, never warm. -1, None and anything unparsable: DEFAULT_TTL_S."""
    try:
        if isinstance(keep_alive, bool) or keep_alive is None:
            return DEFAULT_TTL_S
        if isinstance(keep_alive, (int, float)):
            return float(keep_alive) if keep_alive >= 0 else DEFAULT_TTL_S
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(keep_alive))
        if m:
            return float(m.group(1)) * _UNITS[m.group(2) or "s"]
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_TTL_S


def warm_deadline_s(cap_s: float, predicted_prefill_s: float) -> float:
    """The warm-up's own hedge, queue deadline and HTTP timeout: well above the prefill that triggered it."""
    return min(WARM_MAX_S, max(cap_s, 2.0 * predicted_prefill_s) + 60.0)


class ColdGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resident: tuple[str, list[str], float] | None = None  # sys hash, message hashes, expiry (monotonic)
        self._inflight: str | None = None  # key of the one warm-up running; one slot, one warm-up
        self._failed: dict[str, float] = {}  # warm-up key -> monotonic time of its last failure
        self.seq = 0  # bumped by every note_resident / clear
        self.ttl_s = DEFAULT_TTL_S

    @staticmethod
    def observe(payload: dict) -> dict | None:
        return prefix_hash.observe_payload(payload)

    def is_warm(self, obs: dict | None, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            res = self._resident
        if not obs or res is None or t >= res[2]:
            return False
        sys_h, msgs, _exp = res
        cur = obs["msgs"]
        return sys_h == obs["sys"] and len(msgs) <= len(cur) and cur[:len(msgs)] == msgs

    def note_resident(self, obs: dict | None, now: float | None = None) -> None:
        if obs:
            t = time.monotonic() if now is None else now
            with self._lock:
                self._resident = (obs["sys"], list(obs["msgs"]), t + self.ttl_s)
                self.seq += 1

    def clear(self) -> None:
        """The runner's state is unknown (a local call failed or timed out): nothing is warm."""
        with self._lock:
            self._resident = None
            self.seq += 1

    def begin_warm_up(self, obs: dict | None, now: float | None = None) -> tuple[str, int] | None:
        """``(key, generation)`` when this call may start the warm-up. None when one is running (any prefix),
        obs is unusable, or this prefix's last warm-up failed within FAIL_BACKOFF_S."""
        if not obs:
            return None
        t = time.monotonic() if now is None else now
        key = obs["sys"] + ":" + str(len(obs["msgs"])) + ":" + (obs["msgs"][-1] if obs["msgs"] else "")
        with self._lock:
            if self._inflight is not None:
                return None
            if t - self._failed.get(key, -FAIL_BACKOFF_S - 1.0) < FAIL_BACKOFF_S:
                return None
            self._inflight = key
            return key, self.seq

    def end_warm_up(self, key: str, obs: dict | None, ok: bool, generation: int, now: float | None = None) -> bool:
        """Releases the slot. On success records ``obs`` as resident unless a live turn recorded (or a failure
        cleared) the runner's state since the warm-up began. Returns whether it was recorded."""
        t = time.monotonic() if now is None else now
        with self._lock:
            if self._inflight == key:
                self._inflight = None
            if not ok:
                self._failed[key] = t
                if len(self._failed) > 256:
                    self._failed.pop(next(iter(self._failed)))
                self._resident = None  # a failed prefill leaves the runner in an unknown state
                self.seq += 1
                return False
            if obs and self.seq == generation:
                self._resident = (obs["sys"], list(obs["msgs"]), t + self.ttl_s)
                self.seq += 1
                return True
        return False


GUARD = ColdGuard()
