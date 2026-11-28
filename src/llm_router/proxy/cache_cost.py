"""Cache-aware tier stickiness for the proxy's Claude-tier rewrite (basic).

Anthropic's prompt cache is per model. When a conversation moves from one
Claude tier to another, the new tier has no cache for the conversation's
prefix and writes it again, at its own cache-write rate. Claude Code writes its
cache with the 1-hour TTL, so the toll is priced at the 1-hour write rate.

So a conversation stays on the model it was last served on, and moves only:
  * when the classified complexity class changes, or
  * at a cold point: no call for ``cold_gap_s`` (the cache has expired, so the
    switch costs nothing extra), or, only with ``switch_after_first_call``,
    the call right after the conversation's first call (held on the requested
    model by the no-downgrade rule): the "plan on the big model, execute on
    the cheaper one" boundary.

``switch_after_first_call`` is OFF by default because it measured net
negative. Live smoke, 2026-09-29 (3 golden fixture tasks, real ``claude -p
--model opus``, 23 calls, n=3 switches): each switch re-wrote about 25-34k
prefix tokens on Sonnet 5.5 ($0.34 of cache writes across the three), and the
estimated cost came to $1.07 against $0.78 had every call stayed on Opus 5.5.
Opus 5.5 and Sonnet 5.5 read cache at the same $0.20/M, so after the switch
only output and new-content writes are cheaper, at a few tenths of a cent per
call. A 3-6 call task never earns the re-write back.

A conversation is keyed by Claude Code's session id plus a hash of its first
user turn, so a sub-agent (same session id, its own first turn) keeps its own
state. State is in memory and lost when the proxy restarts; a conversation the
proxy has not seen before is treated as last served on the model it requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

from llm_router.proxy.steps import non_system

DEFAULT_COLD_GAP_S = 3600.0
_MAX_CONVERSATIONS = 4096


def cold_cache_only() -> bool:
    """``LLM_ROUTER_TIER_COLD_CACHE_ONLY`` (default off): a tier rewrite of a PINNED model to a cheaper
    tier (the D-31 arm, Opus -> Haiku) runs only when the requested model's prompt cache for the
    conversation is cold; a warm cache keeps the requested model (reason ``tier_skip_warm_cache``).
    Why: the rewrite writes the whole prefix again on the target model (owner D-643; 77 Opus->Haiku
    rewrites, 30 d to 2026-11-26, 46 lose -$7.81, ledger tier_stats estimate -$5.97 net).
    With the flag on the arm is effectively disabled on pinned Opus (only first calls and calls
    after > 1 h idle can be rewritten). Gated turns are logged ``arm_assignment=ineligible`` with
    ``tier_arm_reason=tier_skip_warm_cache``: arm analyses MUST exclude those rows."""
    return os.environ.get("LLM_ROUTER_TIER_COLD_CACHE_ONLY", "").strip().lower() in ("1", "on", "true", "yes")


def _without_cache_control(node):
    """Drop ``cache_control`` markers: Claude Code moves them between turns, and a marker
    on the first turn's last block must not change the key of the next turn."""
    if isinstance(node, dict):
        return {k: _without_cache_control(v) for k, v in node.items() if k != "cache_control"}
    if isinstance(node, list):
        return [_without_cache_control(v) for v in node]
    return node


def conversation_key(body: dict, session_id: str | None) -> str:
    """Session id plus a hash of the WHOLE first user message. Claude Code opens every thread
    of a session (main and sub-agents) with the same long preamble (CLAUDE.md, memory index),
    so a hash of a prefix of it gave all of them one key and one shared local pin
    (2026-11-08: 97 sub-agent rows served ``pinned_claude`` off the main thread's pin)."""
    msgs = non_system(body.get("messages") or [])
    first = _without_cache_control(msgs[0].get("content") if msgs else "")
    digest = hashlib.sha256(json.dumps(first, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{session_id or '-'}:{digest}"


def context_tokens(usage: dict | None) -> int:
    """Tokens of prefix a call read or wrote: what a switch would re-write."""
    u = usage or {}
    return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(
        u.get("cache_creation_input_tokens") or 0)


def switch_cost_usd(target_model: str, prefix_tokens: int | None) -> float | None:
    """Estimated cost of moving a conversation to ``target_model``: its prefix
    written again at the target's 1-hour cache-write rate. ``None`` when the
    prefix size or the target's rate is unknown."""
    if not prefix_tokens:
        return None
    from llm_router import pricing

    rate = pricing.cache_write_1h_rate(target_model)
    if rate is None:
        return None
    return round(prefix_tokens * rate / 1_000_000, 6)


@dataclass
class ConvState:
    model: str
    complexity: str | None
    ts: float
    reason: str
    prefix_tokens: int | None = None


class ReadOnlyStickiness:
    """A view of a ``Stickiness`` that reads and never records (authority ``advise``, P2.2-A:
    a tier proposal that is never applied must not pin the conversation's later turns)."""

    def __init__(self, inner: "Stickiness") -> None:
        self._inner = inner

    def get(self, key: str):
        return self._inner.get(key)

    def is_cold(self, state) -> bool:
        return self._inner.is_cold(state)

    def record(self, *args, **kwargs) -> None:
        return None

    def record_usage(self, *args, **kwargs) -> None:
        return None

    def note_served(self, *args, **kwargs) -> None:
        return None

    def served_warm(self, *args, **kwargs):
        return self._inner.served_warm(*args, **kwargs)


class Stickiness:
    """Per-conversation memory of the last model served. Thread-safe; bounded."""

    def __init__(self, cold_gap_s: float = DEFAULT_COLD_GAP_S, clock=time.time) -> None:
        self.cold_gap_s = cold_gap_s
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, ConvState] = {}
        # key -> (model, ts) of the last call actually SERVED on that model, written for pinned
        # conversations (which have no ConvState) so the cold-cache gate can read cache age.
        self._served: dict[str, tuple[str, float]] = {}

    def note_served(self, key: str, model: str | None) -> None:
        """Remember that ``model`` served a call of conversation ``key`` now (it re-armed that
        model's cache for the conversation, 1-hour TTL, refreshed on every read)."""
        if not model:
            return
        with self._lock:
            if len(self._served) >= _MAX_CONVERSATIONS and key not in self._served:
                del self._served[min(self._served, key=lambda k: self._served[k][1])]
            self._served[key] = (model, self._clock())

    def served_warm(self, key: str, model: str | None, unseen_warm: bool) -> bool:
        """Is ``model``'s cache for conversation ``key`` warm: its last served call was less than
        ``cold_gap_s`` ago. A conversation not seen (proxy restart, or the model was not the last
        one serving it) is ``unseen_warm`` (a mid-conversation call, as ``tiers`` treats UNSEEN)."""
        with self._lock:
            seen = self._served.get(key)
        if seen is None or seen[0] != model:
            return unseen_warm
        return self._clock() - seen[1] < self.cold_gap_s

    def get(self, key: str) -> ConvState | None:
        with self._lock:
            return self._state.get(key)

    def is_cold(self, state: ConvState) -> bool:
        return self._clock() - state.ts >= self.cold_gap_s

    def record(self, key: str, model: str, complexity: str | None, reason: str) -> None:
        with self._lock:
            prev = self._state.get(key)
            prefix = prev.prefix_tokens if prev is not None else None
            if len(self._state) >= _MAX_CONVERSATIONS and key not in self._state:
                oldest = min(self._state, key=lambda k: self._state[k].ts)
                del self._state[oldest]
            self._state[key] = ConvState(model, complexity, self._clock(), reason, prefix)

    def record_usage(self, key: str, usage: dict | None) -> None:
        tokens = context_tokens(usage)
        if not tokens:
            return
        with self._lock:
            if key in self._state:
                self._state[key].prefix_tokens = tokens
