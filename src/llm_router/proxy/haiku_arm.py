"""D-31 = A: a scoped Haiku experiment arm that runs despite ``pinned_models``.

The owner pinned Opus (decision Q1), which left GE4-a (Haiku-vs-Frontier shadow pairs)
and P0.11-b (the daily Haiku watch) with 0 Haiku rows. This arm sends a small share of
*simple Q&A* turns to Haiku anyway, logs each as an arm, and leaves every other pinned
turn pinned. It is opt-in: ``haiku_arm_share`` in ``claude_tiers.yaml`` (default 0 = off).

Assignment is a hash, not a draw: ``sha256(session_id | turn_id | salt)`` read as a
fraction of 1. The same turn always lands in the same place, so a run is reproducible
and a ledger row can be re-checked by recomputing :func:`bucket`. The turn id is the
count of non-system messages in the request (the same number on a retried request, and
different for each human turn of a session).

Eligibility (``tiers.ClaudeTierPolicy._pinned_or_arm``) reuses the existing rules and adds
no classifier: the call must be a main-thread human turn (``steps.step_kind ==
turn_first``; since TURNFIRST-1 a sub-agent follow-up is ``subagent_turn`` ->
``not_main_thread`` and a notification-only turn ``harness_turn`` -> ``harness_turn``), past the first call, with no ``opus:`` pin, ``/model`` pin or correction
signal, a body Haiku can take, and the router's own classifier must say
``query`` / ``simple``. Anything else stays pinned.
"""
from __future__ import annotations

import hashlib

from llm_router.proxy import steps

ARM_NAME = "haiku_simple_qa"
SALT = "d31-haiku-arm-v1"

ASSIGNED = "treatment"
INELIGIBLE = "ineligible"

# Why an in-bucket turn stayed pinned (the ``tier_arm_reason`` of an ineligible row).
WHY_SIDE_CALL = "side_call"
WHY_NOT_TURN_FIRST = "not_turn_first"
WHY_FIRST_CALL = "first_call"
WHY_LONG_FIRST_PROMPT = "long_first_prompt"
WHY_OPUS_PIN = "explicit_opus_pin"
WHY_USER_PIN = "user_pinned"
WHY_CORRECTION = "correction_signal"
WHY_NO_HAIKU_TIER = "no_haiku_tier"
WHY_NOT_ABOVE_HAIKU = "requested_not_above_haiku"
WHY_BODY = "haiku_body_blocked"
WHY_NOT_SIMPLE_QA = "not_simple_qa"
WHY_CLASSIFY_ERROR = "classify_error"
WHY_NOT_MAIN_THREAD = "not_main_thread"
WHY_HARNESS_TURN = "harness_turn"  # main thread, newest turn only a notification or command echo
WHY_SESSION_KIND = "session_kind"
WHY_MATCHED = "matched:"  # + the pattern, the reason of an assigned row
TREATMENT_RETRIED = "treatment_retried_original"  # Haiku 4xx'd; the original (pinned model) body was sent
DEFAULT_ELIGIBLE = (("query", "simple"),)
ORGANIC_KINDS = (None, "organic")  # None = never tagged; harness / research / headless are never armed


def parse_eligible(value: object) -> tuple[tuple[str, str], ...]:
    """``haiku_arm_eligible`` -> (task_type, complexity) pairs; missing = ``query/simple`` only."""
    if value is None:
        return DEFAULT_ELIGIBLE
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"haiku_arm_eligible must be a non-empty list of 'task/complexity' (complexity may be '*'), got {value!r}")
    pairs = []
    for v in value:
        parts = str(v).split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"haiku_arm_eligible entry must look like 'query/simple', got {v!r}")
        if parts[0] == "*":
            raise ValueError(f"haiku_arm_eligible: task_type must be named (only complexity may be '*'), got {v!r}")
        pairs.append((parts[0], parts[1]))
    return tuple(pairs)


def match(eligible: tuple[tuple[str, str], ...], task: str | None, cx: str | None) -> str | None:
    """The first pattern ``task/complexity`` (complexity may be ``*``) that covers the pair, else None."""
    for t, c in eligible:
        if t == task and c in ("*", cx):
            return f"{t}/{c}"
    return None


def is_main_thread(body: dict) -> bool:
    """The proxy's one main-thread test (``steps.is_main_thread``): an ``Agent``/``Task``
    launcher and no sub-agent marker. A general-purpose sub-agent holds the launcher too
    (TURNFIRST-2), so the launcher alone is not the test."""
    return steps.is_main_thread(body)


def parse_share(value: object) -> float:
    """``haiku_arm_share`` -> a fraction in [0, 1]; missing/None = 0 (off)."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"haiku_arm_share must be a number between 0 and 1, got {value!r}")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"haiku_arm_share must be between 0 and 1, got {value!r}")
    return float(value)


def bucket(session_id: str, turn_id: int | str) -> float:
    """The turn's position in [0, 1): the first 8 bytes of the hash over 2**64."""
    digest = hashlib.sha256(f"{session_id}|{turn_id}|{SALT}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def in_arm(session_id: str | None, turn_id: int | str, share: float) -> float | None:
    """The turn's bucket when it falls inside ``share``, else None (also for no session)."""
    if share <= 0.0 or not session_id:
        return None
    b = bucket(session_id, turn_id)
    return b if b < share else None
