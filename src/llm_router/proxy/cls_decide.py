"""North Star PR-A1 + A2: the per-turn classifier decision, recorded (A1) and enforced as a
per-turn veto (A2).

A2: a decided verdict (source ``llm`` or ``cache``) is a veto when the tier is opus or
sonnet (``veto_tier``) or when the tier is haiku but no passing ``proxy`` / ``local`` lane
batch exists (``veto_lane``, read directly from ``lane_quality``; ``door_verdict.
local_lane_for`` cannot see the proxy door). A veto keeps THAT TURN on Claude: the verdict
is stored under the turn key, so every continuation of the turn inherits it with no second
classifier call, and the next human turn is classified again. A veto never sets the
conversation pin (``server.py`` ``local_step``): the ``started_elsewhere`` test asks the
local-mode state whether the conversation was seen (``LocalModeState.seen``), not whether a
pin exists. Failopen sources are not decided and behave as before. Active only with the
decision model set and door mode ``on``; in ``shadow`` / ``off`` there is no inline verdict
and so no veto (rollback: ``LLM_ROUTER_LOCAL_CLASSIFIER=shadow``, applied on the next
request, stored vetoes included).

RESIDUAL (rv17c S3, the ``lm.set_pin(key, PIN_CLAUDE)`` after a failed local step in
``local_step``): a local step that fails validation still pins the whole conversation to
Claude. Kept on purpose: it is the existing "one logged move, never back and forth" safety
rule (a failing local model is not retried every turn; flapping would also burn Claude's
prompt cache), it is not a classifier veto, and changing it would alter serving for runs
with no classifier at all. Consequence: after such a failure later human turns are still
classified and recorded (``allow``) but Claude serves them (``pinned_claude``); ``ns-check``
shows decided versus served.

``cls_applied`` was always false in A1. It is now true exactly on rows where a veto decided
the serving (``decision.reason == classifier_veto``).

What a row gets (all additive, text-free; hashes, counters and reason codes only):

* ``supported`` / ``unsupported_reason``: :func:`supported` (one predicate, computed
  from the request before ``local_step``). A label only in A1; it gates nothing.
* ``cls_turn_idx`` / ``cls_turn_sha``: which human turn the call belongs to. ``turn_idx``
  is new code (the count of user messages that carry human text); ``turn_sha`` is the
  ledger's ``text_sha`` of the newest human text.
* ``cls_decision_id`` / ``cls_turn_source`` / ``cls_turn_reason`` / ``cls_turn_tier`` /
  ``cls_turn_decided``: the verdict for that turn. The names carry ``turn`` because the
  rule engine already owns ``cls_source`` (always ``"rules"``) and ``cls_reason`` on the
  same row, and tests pin both.

A human turn is a call whose :func:`steps.turn_origin` is typed, command or
subagent_brief. It is classified once (inline, up to the classifier's own 2.0 s budget)
and stored under ``(conversation_key, turn_idx, turn_sha)``; every later call of the same
tool loop reads the store (``cls_turn_source = "inherit"``). Decided means the stored
source is ``llm`` or ``cache``; any failopen source is recorded and is NOT decided.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict

from llm_router import door_verdict, failopen, local_classifier, prompt_key
from llm_router.proxy import local_mode
from llm_router.proxy.cache_cost import conversation_key
from llm_router.proxy.cls_input import assemble
from llm_router.proxy.steps import (
    ORIGIN_COMMAND, ORIGIN_SUBAGENT_BRIEF, ORIGIN_TYPED, _human_text, is_thread_continue, newest_human_text,
    non_system, turn_origin,
)

# -- supported -----------------------------------------------------------------

R_NO_TOOLS = "no_tools"
R_FORCED_TOOL_CHOICE = "forced_tool_choice"
R_NOT_USER_TURN = "not_user_turn"
R_MEDIA = "media"
R_THREAD_CONTINUE = "thread_continue"

# -- cls_turn_source (beyond the classifier's own llm/cache/failopen sources) ----
S_INHERIT = "inherit"
S_NONE = "none"

# -- cls_turn_reason ---------------------------------------------------------------
REASON_ALLOW = "allow"
REASON_VETO_TIER = "veto_tier"
REASON_VETO_LANE = "veto_lane"
REASON_UNSUPPORTED = "unsupported"
REASON_NOT_LOCAL_AGENT = "not_local_agent"
REASON_CLASSIFIER_OFF = "classifier_off"
REASON_PREFIX_CLAUDE = "prefix_claude"
REASON_PREFIX_LOCAL = "prefix_local"
REASON_STARTED_ELSEWHERE = "started_elsewhere"
REASON_SECRET = "withheld_secret"
REASON_NO_TEXT = "no_text"
REASON_ERROR = "error"

VETO_REASONS = (REASON_VETO_TIER, REASON_VETO_LANE)
HUMAN_ORIGINS = (ORIGIN_TYPED, ORIGIN_COMMAND, ORIGIN_SUBAGENT_BRIEF)
DECIDED_SOURCES = ("llm", "cache")
MAX_TURNS = 4096  # as the conversation pins (local_mode)

#: The additive row keys and the value of each when nothing was decided. Set on every proxy row.
TURN_FIELD_DEFAULTS = {
    "supported": None, "unsupported_reason": None,
    "cls_turn_idx": None, "cls_turn_sha": None,
    "cls_decision_id": None, "cls_turn_source": S_NONE, "cls_turn_reason": None,
    "cls_turn_tier": None, "cls_turn_decided": False, "cls_turn_ms": None,
}


def supported(body: dict) -> tuple[bool, str | None]:
    """The one supported-call predicate: an agent turn (client tools, no forced tool
    choice, newest turn is the user's), no media, not a Message Threads continue.
    Returns ``(True, None)`` or ``(False, reason)``. Pure; never raises."""
    try:
        if is_thread_continue(body):
            return False, R_THREAD_CONTINUE
        if not any(isinstance(t, dict) and t.get("name") and "input_schema" in t for t in body.get("tools") or []):
            return False, R_NO_TOOLS
        tc = body.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
            return False, R_FORCED_TOOL_CHOICE
        msgs = non_system(body.get("messages") or [])
        if not msgs or msgs[-1].get("role") != "user":
            return False, R_NOT_USER_TURN
        if local_mode.has_media(body):
            return False, R_MEDIA
        return True, None
    except Exception as exc:  # noqa: BLE001 - a label must never cost a call
        failopen.record("LR-FO-PROXY-CLS-DECIDE", exc)
        return False, REASON_ERROR


def turn_idx(body: dict) -> int:
    """New code: the number of user messages in the history that carry human text once
    tool results and harness blocks are stripped. It rises by one at each new human turn
    and does not move inside a tool loop."""
    return sum(1 for m in non_system(body.get("messages") or [])
               if m.get("role") == "user" and _human_text(m.get("content")))


def turn_key(body: dict, session_id: str | None) -> tuple[str, int, str]:
    """``(conversation_key, turn_idx, turn_sha)``. ``turn_sha`` is the ledger's ``text_sha``."""
    return (conversation_key(body, session_id), turn_idx(body), prompt_key.key(newest_human_text(body)))


def decision_id(key: tuple[str, int, str]) -> str:
    return "d" + hashlib.sha256("|".join(str(p) for p in key).encode()).hexdigest()[:12]


class TurnDecisionStore:
    """Bounded, memory-only map from a turn key to the decision recorded for it."""

    def __init__(self, cap: int = MAX_TURNS):
        self._cap, self._d = cap, OrderedDict()

    def get(self, key: tuple) -> dict | None:
        entry = self._d.get(key)
        if entry is not None:
            self._d.move_to_end(key)
        return entry

    def put(self, key: tuple, entry: dict) -> None:
        self._d[key] = entry
        self._d.move_to_end(key)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def __len__(self) -> int:
        return len(self._d)


def safe_turn_key(body: dict, session_id: str | None) -> tuple[str, int, str] | None:
    """:func:`turn_key`, or ``None`` on any error. The handler computes it once per call and
    hands it to :func:`static_fields` and :func:`decide_turn`."""
    try:
        return turn_key(body, session_id)
    except Exception as exc:  # noqa: BLE001
        failopen.record("LR-FO-PROXY-CLS-DECIDE", exc)
        return None


def static_fields(body: dict, session_id: str | None, key: tuple | None = None) -> dict:
    """The fields that need no classifier: ``supported``, its reason, ``cls_turn_idx`` and
    ``cls_turn_sha``. Pure; on any error the defaults stay (nulls), the call goes on.
    ``key`` is the call's :func:`turn_key` when the caller already has it."""
    out = dict(TURN_FIELD_DEFAULTS)
    try:
        ok, why = supported(body)
        out["supported"], out["unsupported_reason"] = ok, why
        _, out["cls_turn_idx"], out["cls_turn_sha"] = key if key is not None else turn_key(body, session_id)
    except Exception as exc:  # noqa: BLE001
        failopen.record("LR-FO-PROXY-CLS-DECIDE", exc)
    return out


def active() -> bool:
    """The classifier runs inline only when the decision model is configured and the door
    mode is ``on`` (``shadow`` and ``off`` make no inline call)."""
    return door_verdict._decision_model_configured() and door_verdict.mode() == "on"


def proxy_lane_passes() -> bool:
    """A quality batch for the ``proxy`` door with served class ``local`` currently passes
    (any task group: the proxy sees no task type). No evidence, or an unreadable lane file,
    is a veto: a veto only keeps a turn on Claude, so it is the safe side."""
    try:
        from llm_router import lane_quality

        lanes = lane_quality.sampled_lanes(lane_quality.read_rows(), time.time())
        return any(door == "proxy" and served == "local" for (door, served, _grp) in lanes)
    except Exception as exc:  # noqa: BLE001
        failopen.record("LR-FO-PROXY-CLS-DECIDE", exc)
        return False


def veto_of(row: dict) -> str | None:
    """The veto reason (``veto_tier`` / ``veto_lane``) that applies to this call, else
    ``None``: a decided verdict (this turn's or inherited) while the classifier is active."""
    reason = row.get("cls_turn_reason")
    if row.get("cls_turn_decided") is True and reason in VETO_REASONS and active():
        return reason
    return None


def _tier_reason(tier: str | None) -> str:
    return REASON_ALLOW if tier in ("haiku", "local") else REASON_VETO_TIER


def _set(row: dict, *, source: str, reason: str, decided: bool = False, decision: str | None = None,
         tier: str | None = None) -> None:
    row.update(cls_turn_source=source, cls_turn_reason=reason, cls_turn_decided=decided,
               cls_decision_id=decision, cls_turn_tier=tier)


async def decide_turn(body: dict, row: dict, store: TurnDecisionStore, *, lm_on: bool,
                      override: str | None = None, parent_main: bool | None = None,
                      key: tuple | None = None) -> None:
    """Fill the ``cls_*`` turn fields of ``row`` for this call. It never changes
    ``row['decision']``; ``local_step`` applies a veto through :func:`veto_of`. Never raises.

    ``lm_on`` is whether ``--serve local-agent`` is on. ``override`` is ``"claude"`` or
    ``"local"`` for a user prefix row (owner overrides are never counted as decided)."""
    try:
        await _decide(body, row, store, lm_on, override, parent_main, key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - recording must never cost a call
        failopen.record("LR-FO-PROXY-CLS-DECIDE", exc)
        _set(row, source=S_NONE, reason=REASON_ERROR)


async def _decide(body, row, store, lm_on, override, parent_main, key=None) -> None:
    if not row.get("supported"):
        return _set(row, source=S_NONE, reason=REASON_UNSUPPORTED)
    if not lm_on:
        return _set(row, source=S_NONE, reason=REASON_NOT_LOCAL_AGENT)
    if override == "claude":
        return _set(row, source=S_NONE, reason=REASON_PREFIX_CLAUDE)
    if override == "local":
        return _set(row, source=S_NONE, reason=REASON_PREFIX_LOCAL)
    key = key if key is not None else turn_key(body, row.get("session_id"))
    did = decision_id(key)
    stored = store.get(key)
    if stored is not None:
        return _set(row, source=S_INHERIT, reason=stored["reason"], decided=stored["decided"],
                    decision=stored["decision_id"], tier=stored["tier"])
    if turn_origin(body, parent_main) not in HUMAN_ORIGINS:
        # A continuation whose turn this process never classified (restart, or the turn's
        # first call was not seen): supported, not decided, reported.
        return _set(row, source=S_NONE, reason=REASON_STARTED_ELSEWHERE, decision=did)
    if not active():
        return _set(row, source=S_NONE, reason=REASON_CLASSIFIER_OFF, decision=did)
    text = newest_human_text(body)
    if not text.strip():
        return _set(row, source=S_NONE, reason=REASON_NO_TEXT, decision=did)
    if door_verdict._holds_secret(text):
        entry = dict(decision_id=did, source=S_NONE, reason=REASON_SECRET, decided=False, tier=None)
        store.put(key, entry)
        return _set(row, source=S_NONE, reason=REASON_SECRET, decision=did)
    t0 = time.monotonic()
    assembled = await asyncio.to_thread(assemble, {"messages": list(body.get("messages") or []),
                                                   "system": body.get("system")})
    verdict = await local_classifier.classify_async(
        assembled, session_id=row.get("session_id"), text_sha=key[2])
    ok = verdict.source in DECIDED_SOURCES
    reason = _tier_reason(verdict.tier) if ok else f"failopen:{verdict.source}"
    if reason == REASON_ALLOW and not await asyncio.to_thread(proxy_lane_passes):
        reason = REASON_VETO_LANE
    tier = verdict.tier if ok else None
    store.put(key, dict(decision_id=did, source=verdict.source, reason=reason, decided=ok, tier=tier))
    _set(row, source=verdict.source, reason=reason, decided=ok, decision=did, tier=tier)
    row["cls_turn_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
