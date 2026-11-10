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
returns ``None`` and the caller keeps its previous behaviour. 
DROPPED LAYERS (trim path only, ledger ``layout.dropped``): the rolling summary, the session context, the
context_pack per-turn text caps and the 6000-token full-mode cap (``LLM_ROUTER_PROXY_FULL_CAP``) are NOT
applied: tool results go verbatim and a prompt may reach 50-80% of the window (cold-prefill territory; the P22
cold guard is off by default). No latency claim is made. When LLM_ROUTER_SESSION_CONTEXT forbids session
content for ollama, ``arrange`` returns None and the old packing (which honours it) runs. It also returns None
when the first message or the newest unit alone exceed the SOFT share, instead of moving every turn.
Sizes use token_estimate x EST_CORRECTION (the guard's math) and reserve OKF_RESERVE for the OKF block.

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
from contextvars import ContextVar
from collections import OrderedDict
from dataclasses import dataclass, field

from llm_router.proxy import mr_summary
from llm_router.proxy.steps import non_system, session_id_of
from llm_router.token_estimate import est_tokens

DEFAULT_K = 4
SOFT = 0.50
HARD = 0.80
MAX_CONVERSATIONS = 64
#: The guard's estimate (3.5 chars/token x 1.15 margin, local_context_guard) runs ~18% above token_estimate (3.6): measured by the #900 review.
EST_CORRECTION = 1.18
#: Reserved for the OKF block attached after the trim (its ceiling is 300 tokens).
OKF_RESERVE = 300
#: Layers the old ``fast`` packing added or capped that this layout does not (ledger ``dropped``).
DROPPED_TRIM = ("rolling_summary", "session_context", "context_pack_text_caps", "full_mode_cap")
#: Set by server.py local_step: the local-agent path owns this request's state, the ``fast`` trim must not also write it.
LOCAL_AGENT_OWNS = ContextVar("p14_local_agent_owns", default=False)
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


def _system_text(system) -> str:
    if isinstance(system, list):
        system = "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in system)
    lines = [ln for ln in str(system or "").split("\n") if not ln.startswith("x-anthropic-billing-header")]
    return "\n".join(lines)


def conv_key(body: dict) -> str | None:
    """Key of a conversation: session id, system prompt (billing header line dropped: it varies), tool names,
    the first human message WITHOUT P9's summary blocks (so a chunk close keeps the key) and the first
    reply. ``None`` when the body has no human first message: no state is kept for it."""
    msgs = non_system(body.get("messages") or [])
    if not msgs or msgs[0].get("role") != "user" or not _human(msgs[0]):
        return None
    base, _ = split_head(msgs[0])
    names = sorted(str(t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict))
    return sig([session_id_of(body), _system_text(body.get("system")), names, sig(base),
                sig(msgs[1]) if len(msgs) > 1 else ""])


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
    head_src: str = ""          # sig of the first message WITHOUT summaries: which conversation the head belongs to
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


def _commit(key: str | None, msgs: list, *, moved: bool, reason: str, fixed_est: int = 0, freeze: bool = False,
            dropped: tuple = ()) -> None:
    """Remember what was sent for the conversation and compute the counters. ``freeze``: ``msgs[0]`` and the
    boundary ``msgs[1]`` become the layout later turns hold."""
    if key is None:
        return
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
            conv.head_src = sig(split_head(msgs[0])[0])
        elif not freeze:
            conv.head, conv.head_sig, conv.boundary_sig = None, "", ""
        conv.last, conv.last_sig, conv.last_est = hashes, hashes[-1], est
        conv.stats = {"moved": moved, "reason": reason, "age": conv.age, "held_before_move": held, "msgs": len(msgs), "tokens_est": est, "dropped": list(dropped),
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


def _locate(rest: list, boundary: str) -> int | None:
    """Index into ``rest`` of the FIRST unit start whose message hashes to ``boundary`` (one rule for both paths)."""
    return next((a for a, _ in mr_summary.units_of(rest) if sig(rest[a]) == boundary), None)


# ------------------------------------------------------------------ local-agent path (with P9)
def hold(key: str | None, messages: list, fits) -> list | None:
    """The previous turn's frozen layout applied to this turn's full history, or ``None`` (move needed).

    ``messages``: the client's full message list. ``fits(candidate_messages) -> bool``: does it still fit the
    local cap. The candidate is a COPY of the stored head (first message plus the summaries P9 made at the
    last move) and every message from the stored boundary on. ``None`` when nothing is frozen, the stored head
    belongs to another first message, the boundary message is gone (the client rewrote history), the candidate
    is not paired, or it no longer fits."""
    try:
        if key is None or len(messages) < 2:
            return None
        with _LOCK:
            conv = _STATE.get(key)
            if conv is None or conv.head is None or not conv.boundary_sig:
                return None
            _STATE.move_to_end(key)
            head, boundary, src = copy.deepcopy(conv.head), conv.boundary_sig, conv.head_src
        if src != sig(split_head(messages[0])[0]):
            return None  # never apply another conversation's summaries
        rest = messages[1:]
        at = _locate(rest, boundary)
        if at is None:
            return None
        cand = [head] + rest[at:]
        return cand if paired(cand) and fits(cand) else None
    except Exception:  # noqa: BLE001 - an optimisation: a failure is a move, never a 500
        return None


def move_goal(key: str | None, cap: int) -> int:
    """The prompt size a move should bring the history down to: ``cap`` less K turns of the growth this
    conversation showed, never below half the cap. Passed to P9 as its goal, so the next K turns tend to fit."""
    with _LOCK:
        conv = _STATE.get(key) if key is not None else None
        growth = conv.growth if conv is not None else DEFAULT_GROWTH
    return max(int(cap * SOFT), cap - k_turns() * growth)


def observe(key: str | None, messages: list, *, moved: bool, reason: str, freeze: bool = False,
            fixed_est: int = 0) -> None:
    """Record the messages actually sent this turn (local-agent path). ``freeze`` after a move; never raises."""
    try:
        if key is not None and len(messages) >= 2:
            _commit(key, messages, moved=moved, reason=reason, fixed_est=fixed_est, freeze=freeze)
    except Exception:  # noqa: BLE001 - measurement and bookkeeping only
        pass


# ------------------------------------------------------------------ trim path (the ``fast`` trim)
def arrange(turns: list, *, key: str | None, window: int, fixed_est: int, reserved: int = 0) -> list | None:
    """The messages to send, or ``None`` when this layout cannot or must not arrange the turns (the caller keeps
    its packing): no key, the privacy rule withholds session content, the local-agent path owns this request,
    or the head plus the newest unit exceed the SOFT share (a huge first message would otherwise move every turn) of the window."""
    try:
        if key is None or LOCAL_AGENT_OWNS.get():
            return None
        from llm_router.session_store import allows_session_content
        if not allows_session_content("ollama"):  # the one rule: LLM_ROUTER_SESSION_CONTEXT=off sends no history
            return None
        return _arrange(turns, key, window, fixed_est, reserved)
    except Exception:  # noqa: BLE001 - an optimisation: any failure is the previous packing, never a 500
        return None


def _arrange(turns: list, key: str, window: int, fixed_est: int, reserved: int) -> list | None:
    if len(turns) < 2 or not all(isinstance(m, dict) for m in turns):
        return None
    head = turns[0]
    if head.get("role") != "user" or not _human(head) or turns[-1].get("role") != "user":
        return None
    rest = turns[1:]
    base, summaries = split_head(head)
    head_out = _head_out(head, base, summaries)
    head_sig = sig(head_out)
    # the guard's math: our estimate x EST_CORRECTION must fit the window less the reserve and the OKF block
    usable = max((window - reserved - OKF_RESERVE) / EST_CORRECTION, 1)
    head_est = fixed_est + est_tokens(head_out)
    rest_est = [est_tokens(m) for m in rest]
    starts = _starts(rest)

    def total(s: int) -> int:
        return head_est + sum(rest_est[s:])

    with _LOCK:
        conv = _STATE.get(key)
        if conv is not None:
            _STATE.move_to_end(key)
        frozen_sig = conv.boundary_sig if conv is not None else ""
        conv_head_sig, conv_age = (conv.head_sig, conv.age) if conv is not None else ("", 0)
    frozen = _locate(rest, frozen_sig) if frozen_sig else None
    if frozen is not None and frozen not in starts:
        frozen = None
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
        s = next((a for a in starts if total(a) <= SOFT * usable), None)
        if s is None:
            return None  # even the newest unit does not fit the SOFT share: the old packing, not a move every turn
    msgs = [head_out] + rest[s:]
    while not paired(msgs):  # defensive: units are fused, so this only walks on malformed input
        earlier = [a for a in starts if a < s]
        if not earlier:
            return None
        s = earlier[-1]
        msgs = [head_out] + rest[s:]
    _commit(key, msgs, moved=reason != REASON_HOLD, reason=reason, fixed_est=fixed_est, freeze=True,
            dropped=DROPPED_TRIM)
    return msgs
