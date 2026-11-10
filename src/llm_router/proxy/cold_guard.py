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
import threading

from llm_router.proxy import prefix_hash

ENV_FLAG = "LLM_ROUTER_COLD_GUARD"
REASON = "cold_over_deadline"


def enabled() -> bool:
    # literal name below: env_registry's scan needs it
    return os.environ.get("LLM_ROUTER_COLD_GUARD", "").strip().lower() in ("1", "on", "true", "yes")


class ColdGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resident: tuple[str, list[str]] | None = None
        self._inflight: str | None = None  # key of the one warm-up running; one slot, one warm-up

    @staticmethod
    def observe(payload: dict) -> dict | None:
        return prefix_hash.observe_payload(payload)

    def is_warm(self, obs: dict | None) -> bool:
        with self._lock:
            res = self._resident
        if not obs or res is None:
            return False
        sys_h, msgs = res
        cur = obs["msgs"]
        return sys_h == obs["sys"] and len(msgs) <= len(cur) and cur[:len(msgs)] == msgs

    def note_resident(self, obs: dict | None) -> None:
        if obs:
            with self._lock:
                self._resident = (obs["sys"], list(obs["msgs"]))

    def begin_warm_up(self, obs: dict | None) -> str | None:
        """A key when this call may start the warm-up, None when one is running (for any prefix) or obs is unusable."""
        if not obs:
            return None
        key = obs["sys"] + ":" + str(len(obs["msgs"])) + ":" + (obs["msgs"][-1] if obs["msgs"] else "")
        with self._lock:
            if self._inflight is not None:
                return None
            self._inflight = key
        return key

    def end_warm_up(self, key: str, obs: dict | None, ok: bool) -> None:
        with self._lock:
            if self._inflight == key:
                self._inflight = None
        if ok:
            self.note_resident(obs)


GUARD = ColdGuard()
