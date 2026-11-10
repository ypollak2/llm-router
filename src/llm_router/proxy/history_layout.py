"""P14: append-only history layout for local requests (flag ``LLM_ROUTER_PROXY_LAYOUT``, default off).

llama.cpp reuses its context checkpoint only up to the first changed token (probe 2, 2026-11-10: restored
n_past >= 0.8x the previous prompt in 7/7 growing-prefix requests, 0 full re-process; source
coord/local-levers/probes-128k/RESULTS.md). A history that is re-cut every turn (the ``fast`` trim's sliding
last-7 window; P9 re-closing the oldest chunks) starts differently every turn. This module freezes the cut.

Layout of the messages sent to the local model (system and the 4 fixed tools come from the trim, fixed)::

    first message (+ P9 ``<chunk-summary`` blocks; the P11 pinned facts sit inside each)  <- frozen head
    tail: whole tool_use + tool_result units, from a boundary that is FROZEN between moves

Two entry points share one per-conversation state:

* Local-agent path (``--serve local-agent`` + P9, trim ``none``): ``hold`` rebuilds the previous move's head and
  the history from the frozen boundary, and server.py uses it instead of calling P9 while it still fits the
  local cap. Zero map calls on a held turn. When it no longer fits (a chunk must close) P9 runs ONCE: a MOVE,
  with ``move_goal`` as its goal (cap less K turns of this conversation's observed growth, never below half the
  cap) so the next K turns tend to fit; the new head carries the old and the new summaries, the boundary is
  the new cut. K is therefore a target the ledger measures (``held_before_move``), not a guarantee: P9 closes
  whole ~8k chunks and cannot always reach the goal.
* ``fast`` trim path (``arrange``, replaces the sliding window of backends.py ``_pack_messages``): moves only
  when ``first`` (no state), ``history_changed`` (the client rewrote history), ``chunk_closed`` (the head
  carries another summary), ``over_hard`` (the layout exceeds HARD of the usable window) or ``k_turns`` (K turns
  since the last move AND the layout exceeds SOFT). A move cuts the tail to SOFT (~50% of the usable window).
  Here "early tokens change at most every K turns" holds by construction.

This module does not summarize. On the trim path the messages before the boundary are dropped, as the sliding
window dropped them. A turn it cannot arrange (no first human message, an orphan tool_result in the input)
returns ``None`` and the caller keeps its previous behaviour. The ``fast`` trim's mutable context layers
(rolling summary, session context) are not added in this layout: they change every turn.

Never emitted: a tool_result without its tool_use, or a tool_use (outside the last message) without its
result. Units are the same fused pairs P9 closes (mr_summary.units_of); every layout is validated again.

Ledger (``take_stats``, counts only, never text): ``moved``, ``reason``, ``age``, ``held_before_move``, ``msgs``,
``tokens_est``, ``msgs_same_prev`` and ``tokens_same_prev`` (leading messages / their estimated tokens identical
to the conversation's previous request; None on the first). This is the proxy-side signal of checkpoint reuse.
Whether the runner really restored a checkpoint is in its log ("restored" / "forcing full re-processing").

Known limit (MEASURED, 49 and 587 "erased" lines in the plan): an unrelated prompt on the single slot erases
all checkpoints. No layout can prevent that.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from llm_router.proxy import mr_summary
from llm_router.token_estimate import est_tokens

DEFAULT_K = 4
SOFT = 0.50
HARD = 0.80
MAX_CONVERSATIONS = 64
SUMMARY_OPEN = "<chunk-summary"

REASON_FIRST = "first"
REASON_HISTORY_CHANGED = "history_changed"
REASON_CHUNK_CLOSED = "chunk_closed"
REASON_OVER_HARD = "over_hard"
REASON_K_TURNS = "k_turns"
REASON_HOLD = "hold"
REASON_OVER_CAP = "over_cap"  # local-agent path: the held layout no longer fits the cap
REASON_UNDER_CAP = "under_cap"  # local-agent path: the whole history fits, sent as is (already append-only)


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_PROXY_LAYOUT", "").strip().lower() in ("1", "true", "yes", "on")


def k_turns() -> int:
    raw = os.environ.get("LLM_ROUTER_PROXY_LAYOUT_K", "").strip()
    try:
        return int(raw) if int(raw) > 0 else DEFAULT_K
    except ValueError:
        return DEFAULT_K


# ------------------------------------------------------------------ hashing
def _plain(node):
    """Drop ``cache_control``: Claude Code moves the marker between turns; the model never sees it."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items() if k != "cache_control"}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def sig(message) -> str:
    raw = json.dumps(_plain(message), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _blocks(message: dict) -> list:
    c = message.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return list(c) if isinstance(c, list) else []


def _is_summary(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).startswith(SUMMARY_OPEN)


def split_head(head: dict) -> tuple[list, list]:
    """(base blocks, P9 summary blocks) of the first message."""
    blocks = _blocks(head)
    return [b for b in blocks if not _is_summary(b)], [b for b in blocks if _is_summary(b)]


def conv_key(messages: list) -> str:
    """Hash of the first human message WITHOUT P9's summary blocks, so a chunk close keeps the key."""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user" and _human(m):
            base, _ = split_head(m)
            return sig(base)
    return "-"


def _human(m: dict) -> bool:
    c = m.get("content")
    if isinstance(c, str):
        return bool(c)
    return isinstance(c, list) and any(not isinstance(b, dict) or b.get("type") != "tool_result" for b in c)


# ------------------------------------------------------------------ pairing
def paired(messages: list) -> bool:
    """No orphan: every tool_result follows its tool_use, every tool_use (outside the last message) has its result."""
    uses: dict[str, bool] = {}
    for i, m in enumerate(messages):
        for b in _blocks(m):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and m.get("role") == "assistant":
                uses[str(b.get("id"))] = i == len(messages) - 1
            elif b.get("type") == "tool_result":
                if str(b.get("tool_use_id")) not in uses:
                    return False
                uses[str(b.get("tool_use_id"))] = True
    return all(uses.values())


# ------------------------------------------------------------------ state
DEFAULT_GROWTH = 2_000  # est tokens a turn adds, until the conversation shows its own


@dataclass
class _Conv:
    head: dict | None = None      # the frozen first message as sent (P9 summaries and their pins included)
    head_sig: str = ""
    boundary_sig: str = ""        # content hash of the first tail message; "" = nothing frozen
    age: int = 0                  # turns held since the last move
    last_sig: str = ""
    last: list[str] = field(default_factory=list)
    last_est: int = 0
    growth: int = DEFAULT_GROWTH
    stats: dict | None = None


_STATE: OrderedDict[str, _Conv] = OrderedDict()
_LOCK = threading.Lock()


def clear() -> None:
    with _LOCK:
        _STATE.clear()


def take_stats(key: str) -> dict | None:
    """The stats of the conversation's latest layout, once (counts only)."""
    with _LOCK:
        conv = _STATE.get(key)
        if conv is None or conv.stats is None:
            return None
        out, conv.stats = conv.stats, None
        return out


def _commit(key: str, msgs: list, *, moved: bool, reason: str, fixed_est: int = 0, freeze: bool = False) -> None:
    """Remember what was sent for the conversation and compute the counters. ``freeze``: ``msgs[0]`` and the
    boundary ``msgs[1]`` become the layout later turns hold."""
    hashes = [sig(m) for m in msgs]
    est = fixed_est + est_tokens(msgs)
    with _LOCK:
        conv = _STATE.pop(key, None) or _Conv()
        prev = conv.last if conv.last else None
        retry = prev is not None and conv.last_sig == hashes[-1] and len(prev) == len(hashes)
        common = None
        if prev is not None:
            common = 0
            for a, b in zip(prev, hashes):
                if a != b:
                    break
                common += 1
        held = conv.age if moved and conv.boundary_sig else None  # turns the layout just replaced had been held
        if moved:
            conv.age = 0
        elif not retry:
            conv.age += 1
        if prev is not None and not moved and not retry:
            conv.growth = max(est - conv.last_est, int(conv.growth * 0.75), 1)
        if freeze and len(msgs) > 1:
            conv.head, conv.head_sig, conv.boundary_sig = copy.deepcopy(msgs[0]), hashes[0], hashes[1]
        elif not freeze:
            conv.head, conv.head_sig, conv.boundary_sig = None, "", ""
        conv.last, conv.last_sig, conv.last_est = hashes, hashes[-1], est
        conv.stats = {"moved": moved, "reason": reason, "age": conv.age, "held_before_move": held, "msgs": len(msgs), "tokens_est": est,
                      "msgs_same_prev": common,
                      "tokens_same_prev": None if common is None else fixed_est + est_tokens(msgs[:common])}
        _STATE[key] = conv
        while len(_STATE) > MAX_CONVERSATIONS:
            _STATE.popitem(last=False)


def _head_out(head: dict, base: list, summaries: list) -> dict:
    if not summaries:
        return head
    out = dict(head)
    out["content"] = base + summaries
    return out


def _starts(rest: list, prefer_assistant: bool = True) -> list[int]:
    """Indices into ``rest`` where a tail may begin: unit starts (a fused tool pair is one unit)."""
    starts = [a for a, _ in mr_summary.units_of(rest)]
    asst = [a for a in starts if rest[a].get("role") == "assistant"]
    return (asst or starts) if prefer_assistant else starts


# ------------------------------------------------------------------ local-agent path (with P9)
def hold(messages: list, fits) -> list | None:
    """The previous turn's frozen layout applied to this turn's full history, or ``None`` (move needed).

    ``messages``: the client's full message list. ``fits(candidate_messages) -> bool``: does it still fit the
    local cap. The candidate is the stored head (first message plus the summaries P9 made at the last move)
    and every message from the stored boundary on. ``None`` when nothing is frozen, the boundary message is
    gone (the client rewrote history), the candidate is not paired, or it no longer fits."""
    try:
        with _LOCK:
            conv = _STATE.get(conv_key(messages))
            head, boundary = (conv.head, conv.boundary_sig) if conv is not None else (None, "")
        if head is None or not boundary or len(messages) < 2:
            return None
        rest = messages[1:]
        at = next((a for a, _ in mr_summary.units_of(rest) if sig(rest[a]) == boundary), None)
        if at is None:
            return None
        cand = [head] + rest[at:]
        return cand if paired(cand) and fits(cand) else None
    except Exception:  # noqa: BLE001 - an optimisation: a failure is a move, never a 500
        return None


def move_goal(messages: list, cap: int) -> int:
    """The prompt size a move should bring the history down to: ``cap`` less K turns of the growth this
    conversation showed, never below half the cap. Passed to P9 as its goal, so the next K turns fit."""
    with _LOCK:
        conv = _STATE.get(conv_key(messages))
        growth = conv.growth if conv is not None else DEFAULT_GROWTH
    return max(int(cap * SOFT), cap - k_turns() * growth)


def observe(messages: list, *, moved: bool, reason: str, freeze: bool = False, fixed_est: int = 0) -> None:
    """Record the messages actually sent this turn (local-agent path). ``freeze`` after a move; never raises."""
    try:
        if len(messages) >= 2:
            _commit(conv_key(messages), messages, moved=moved, reason=reason, fixed_est=fixed_est, freeze=freeze)
    except Exception:  # noqa: BLE001 - measurement and bookkeeping only
        pass


# ------------------------------------------------------------------ trim path (the ``fast`` trim)
def arrange(turns: list, *, window: int, fixed_est: int, reserved: int = 0) -> list | None:
    """The messages to send, or ``None`` when this layout cannot arrange the turns (caller keeps its packing)."""
    try:
        return _arrange(turns, window, fixed_est, reserved)
    except Exception:  # noqa: BLE001 - an optimisation: any failure is the previous packing, never a 500
        return None


def _arrange(turns: list, window: int, fixed_est: int, reserved: int) -> list | None:
    if len(turns) < 2 or not all(isinstance(m, dict) for m in turns):
        return None
    head = turns[0]
    if head.get("role") != "user" or not _human(head) or turns[-1].get("role") != "user":
        return None
    rest = turns[1:]
    base, summaries = split_head(head)
    head_out = _head_out(head, base, summaries)
    head_sig = sig(head_out)
    key = conv_key(turns)
    usable = max(window - reserved, 1)
    head_est = fixed_est + est_tokens(head_out)
    rest_est = [est_tokens(m) for m in rest]
    starts = _starts(rest)
    by_sig = {sig(rest[a]): a for a in starts}

    def total(s: int) -> int:
        return head_est + sum(rest_est[s:])

    with _LOCK:
        conv = _STATE.get(key)
        frozen_sig = conv.boundary_sig if conv is not None else ""
        conv_head_sig, conv_age = (conv.head_sig, conv.age) if conv is not None else ("", 0)
    frozen = by_sig.get(frozen_sig) if frozen_sig else None
    reason, s = REASON_HOLD, frozen
    if not frozen_sig:
        reason = REASON_FIRST
    elif frozen is None or conv_head_sig != head_sig:
        reason = REASON_CHUNK_CLOSED if conv_head_sig != head_sig else REASON_HISTORY_CHANGED
    elif total(frozen) > HARD * usable:
        reason = REASON_OVER_HARD
    elif conv_age >= k_turns() and total(frozen) > SOFT * usable:
        reason = REASON_K_TURNS
    if reason != REASON_HOLD:
        s = next((a for a in starts if total(a) <= SOFT * usable), starts[-1])
    msgs = [head_out] + rest[s:]
    while not paired(msgs):  # defensive: units are fused, so this only walks on malformed input
        earlier = [a for a in starts if a < s]
        if not earlier:
            return None
        s = earlier[-1]
        msgs = [head_out] + rest[s:]
    _commit(key, msgs, moved=reason != REASON_HOLD, reason=reason, fixed_est=fixed_est, freeze=True)
    return msgs
