"""Loop guard: stop the proxy from serving the same broken step forever.

Evidence (live trial, 2026-09-28, 3 real ``claude -p`` tasks through the
proxy, golden fixtures c001-c003): one Claude Code session (``ec919061``)
made 62 calls through the proxy, 54 served, with a run of 44 CONSECUTIVE
served replies where the local model (``ollama/qwen3-coder:30b``) kept
re-issuing the same ``Read`` of the same file rather than making progress.
The task still finished — all golden tests passed at the end — but took
195s, and the loop inflated the routed share (63/81 = 77.8%) and the
"est. avoided $2.93" metric (the pre-2026-09-28 formula; see ``ledger``'s
"NET AVOIDED" docstring for why it was replaced) with calls that exist only
because of the loop.
Two healthy sessions in the same trial never exceeded a served run of 6
(``cf5ef91c``: runs of 2, 3, 1; ``d9743217``: runs of 1, 1, 1).

Two guards, both scoped per Claude Code session (``session_id``), both
cleared by ANY non-served step for that session (a real Anthropic call, or a
policy decision to keep the step on Anthropic, is "progress" and breaks the
streak):

``LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE`` (default 8)
    Caps served-in-a-row for a session before the *next* eligible step is
    forced to Anthropic without even trying the backend, whether or not
    anything repeats. Set above the largest healthy consecutive-served run
    observed in the evidence (6) with margin, and far below the runaway run
    (44) this guard exists to cut short.

``LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW`` (default 3)
    How many of the session's most recently served tool calls (name + input,
    exact match) a new served tool call is checked against before it is
    allowed to serve. The runaway session repeated the identical ``Read`` on
    the very next step, so even the smallest useful window (the immediately
    prior step) would have caught it on step 2 of the run; 3 adds a small
    margin for a call that repeats with one different step in between,
    without over-triggering on legitimately revisiting a file two turns
    apart. ``0`` disables the repeat check.

Both guards are in-memory only, scoped to one proxy process, keyed by
``session_id``. A session's state is dropped once its streak breaks, so
memory does not grow with the number of sessions the proxy has ever seen —
only with the sessions currently mid-streak.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from dataclasses import dataclass, field

REASON_LOOP_GUARD = "loop_guard"
# Which trigger tripped; the proxy reads this structured value (row["loop_guard_trigger"]), never the detail string.
TRIGGER_CAP = "cap"
TRIGGER_REPEAT = "repeat"
# Flag-on only: a second cap trip whose window mostly re-issued the previous window's calls is a loop.
TRIGGER_CAP_CYCLE = "cap_cycle"
# Share of the new window's tool signatures that must also be in the previous window for "cap_cycle".
CYCLE_OVERLAP = 0.5
DEFAULT_MAX_CONSECUTIVE = 8
DEFAULT_REPEAT_WINDOW = 3


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _tool_signatures(message: dict) -> list[str]:
    """One signature per ``tool_use`` block: name + canonical JSON of its
    input. Text/thinking blocks carry no repeatable "step" and are ignored."""
    sigs = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            try:
                canon = json.dumps(block.get("input"), sort_keys=True, default=str)
            except (TypeError, ValueError):
                canon = str(block.get("input"))
            sigs.append(f"{block.get('name')}:{canon}")
    return sigs


@dataclass
class _SessionState:
    consecutive_served: int = 0
    recent_tool_sigs: deque = field(default_factory=deque)
    window_sigs: list = field(default_factory=list)  # every tool signature served since the last reset


class LoopGuard:
    """Per-process, per-session loop tracking. One instance per proxy app."""

    def __init__(self, max_consecutive: int | None = None, repeat_window: int | None = None):
        self.max_consecutive = (_env_int("LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE", DEFAULT_MAX_CONSECUTIVE)
                                 if max_consecutive is None else max_consecutive)
        self.repeat_window = (_env_int("LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW", DEFAULT_REPEAT_WINDOW)
                               if repeat_window is None else repeat_window)
        self._sessions: dict[str, _SessionState] = {}
        self._prev_window: dict[str, frozenset] = {}  # signatures of the window a cap trip just ended

    def _state(self, session_id: str) -> _SessionState:
        st = self._sessions.get(session_id)
        if st is None:
            st = _SessionState(recent_tool_sigs=deque(maxlen=max(self.repeat_window, 1)))
            self._sessions[session_id] = st
        return st

    def exhausted(self, session_id: str | None) -> str | None:
        """A fallback detail string when this session already hit the
        consecutive-served cap (the caller must not even try the backend),
        else ``None``."""
        if not session_id or self.max_consecutive <= 0:
            return None
        n = self._sessions.get(session_id).consecutive_served if session_id in self._sessions else 0
        if n >= self.max_consecutive:
            return f"consecutive_served_exceeded:{n}"
        return None

    def repeat_hit(self, session_id: str | None, message: dict) -> tuple[str, str] | None:
        """``(tool name, sha256[:10] of the repeated signature)`` when this served
        candidate repeats a recent one, else ``None``. The hash makes a repeat
        auditable without writing the call's arguments (user text) anywhere. The
        10-hex sha256 prefix is UNSALTED: a short input (a file path, a brief
        command) can be confirmed by guessing it. It stays in the local ledger."""
        if not session_id or self.repeat_window <= 0:
            return None
        recent = self._sessions[session_id].recent_tool_sigs if session_id in self._sessions else ()
        for sig in _tool_signatures(message):
            if sig in recent:
                return sig.split(":", 1)[0], hashlib.sha256(sig.encode("utf-8", "replace")).hexdigest()[:10]
        return None

    def repeat_reason(self, session_id: str | None, message: dict) -> str | None:
        """A fallback detail string naming the tool call this served
        candidate repeats, or ``None`` if it is new (or cannot be checked)."""
        hit = self.repeat_hit(session_id, message)
        return None if hit is None else f"repeat:{hit[0]}"

    def record_served(self, session_id: str | None, message: dict) -> None:
        """The candidate was actually served: extend the streak."""
        if not session_id:
            return
        st = self._state(session_id)
        st.consecutive_served += 1
        sigs = _tool_signatures(message)
        st.recent_tool_sigs.extend(sigs)
        st.window_sigs.extend(sigs)

    def reset(self, session_id: str | None) -> None:
        """A non-served step for this session: the streak is broken."""
        if session_id:
            self._sessions.pop(session_id, None)
            self._prev_window.pop(session_id, None)

    def cap_trip(self, session_id: str | None) -> str:
        """The cap tripped: end the window like ``reset`` but remember its tool
        signatures. Returns ``TRIGGER_CAP_CYCLE`` when at least ``CYCLE_OVERLAP`` of
        this window's signatures were also in the previous window's (a loop with a
        period longer than the repeat window), else ``TRIGGER_CAP``. A window with
        no tool signatures (text-only) is never a cycle."""
        if not session_id:
            return TRIGGER_CAP
        st = self._sessions.pop(session_id, None)
        cur = list(st.window_sigs) if st else []
        prev = self._prev_window.pop(session_id, None)
        if cur and prev is not None and sum(1 for x in cur if x in prev) / len(cur) >= CYCLE_OVERLAP:
            return TRIGGER_CAP_CYCLE
        if cur:
            self._prev_window[session_id] = frozenset(cur)
        return TRIGGER_CAP
