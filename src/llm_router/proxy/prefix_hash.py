"""Prefix-reuse evidence for local (Ollama ``/api/chat``) requests.

llama.cpp reuses its context checkpoint only when a request starts with the
previous request's prompt, byte for byte. Measured elsewhere: a warm append-only
turn of 17k-50k tokens answers in about 8-10 s, a cold or rewritten 41k prompt in
about 62 s. This module records, per request, whether the conversation really is
append-only. Measurement only: nothing here changes a request.

Row fields (hashes and counts, never prompt text): ``prefix_sha_sys`` (sha256[:16] of
the canonical JSON of the system message plus tools), ``n_msgs``,
``prefix_common_with_prev`` (leading messages identical to the conversation's previous
request), ``appended_only``, ``keep_alive``. The first request of a conversation (or
one after the proxy restarted or the LRU evicted it) has no previous: ``prefix_common_with_prev``
and ``appended_only`` are None, not False, so "unknown" is not counted as a rewrite.

Every entry point swallows its own errors: a failure here must cost the request nothing.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

MAX_CONVERSATIONS = 64


def _h(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def observe_payload(payload: dict) -> dict | None:
    """Hashes of the payload actually sent to Ollama. None on any failure."""
    try:
        msgs = payload.get("messages") or []
        sys_msgs = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
        return {"sys": _h([sys_msgs, payload.get("tools")]),
                "msgs": [_h(m) for m in msgs],
                "keep_alive": payload.get("keep_alive")}
    except Exception:  # noqa: BLE001 - measurement must never raise into the request
        return None


class PrefixTracker:
    """Last request's per-message hashes per conversation; LRU-bounded, thread-safe."""

    def __init__(self, max_conversations: int = MAX_CONVERSATIONS) -> None:
        self._max = max_conversations
        self._last: OrderedDict[str, tuple[str, list[str]]] = OrderedDict()
        self._lock = threading.Lock()

    def fields(self, conv_key: str, obs: dict | None) -> dict:
        """Ledger fields for one observation; remembers it as the conversation's latest."""
        try:
            if not obs:
                return {}
            msgs = obs["msgs"]
            with self._lock:
                prev = self._last.pop(conv_key, None)
                self._last[conv_key] = (obs["sys"], msgs)
                while len(self._last) > self._max:
                    self._last.popitem(last=False)
            out = {"prefix_sha_sys": obs["sys"], "n_msgs": len(msgs),
                   "prefix_common_with_prev": None, "appended_only": None,
                   "keep_alive": obs.get("keep_alive")}
            if prev is not None:
                prev_sys, prev_msgs = prev
                common = 0
                for a, b in zip(prev_msgs, msgs):
                    if a != b:
                        break
                    common += 1
                out["prefix_common_with_prev"] = common
                out["appended_only"] = bool(prev_sys == obs["sys"] and common == len(prev_msgs)
                                            and len(msgs) >= len(prev_msgs))
            return out
        except Exception:  # noqa: BLE001
            return {}


TRACKER = PrefixTracker()
