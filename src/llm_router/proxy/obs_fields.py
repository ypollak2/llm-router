"""Observability fields for rows written while local serving is on (``--serve local-agent``).

Additive only: nothing here changes a decision, a reason or an existing field. ``add_fields`` is called from the
local path only, so rows of ``--serve off`` never gain these keys (tests/test_proxy_off_golden.py).

``conv``                first 12 hex of sha256 of the conversation key (never the raw key)
``pin_cause``           why the conversation was pinned to Claude: the reason of the row that set the pin
                        (``local_mode.pin_change == "local->claude"``), ``<reason>:<loop_guard_trigger>`` when
                        a loop guard tripped. Repeated on every later row whose reason is a pinned reason.
``pin_age_steps``       rows of the conversation since that pin row (0 on it)
``served_model``        the backend model that produced a local-served reply (not written over a value a tier
                        decision already put there)
``pop``                 population label: LLM_ROUTER_POP_LABEL, else side_call / marker / <kind>_main|_sub
"""

from __future__ import annotations

import hashlib
import os
import re

from llm_router.proxy import local_mode
from llm_router.proxy.steps import STEP_SIDE_CALL

_POP_LABEL = re.compile(r"^[a-z0-9_]{1,24}$")
PINNED_REASONS = frozenset({local_mode.REASON_PINNED_CLAUDE, local_mode.REASON_PINNED_TURN,
                            local_mode.REASON_PINNED_STICKY})


def conv_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def population(row: dict) -> str:
    label = os.environ.get("LLM_ROUTER_POP_LABEL", "")
    if _POP_LABEL.match(label):
        return label
    if row.get("step_class") == STEP_SIDE_CALL:
        return "side_call"
    if row.get("reason") == local_mode.REASON_CLAUDE_MARKER:
        return "marker"
    kind = row.get("session_kind") or "unknown"
    main = row.get("is_main_thread")
    return f"{kind}_{'main' if main is True else 'sub' if main is False else 'unknown'}"


def add_fields(row: dict, key: str, lm, served, backend_model: str | None = None) -> None:
    """Write the fields on ``row``. ``lm`` is the LocalModeState (holds ``pin_obs``); ``backend_model`` the model
    the local backend runs (the reply message echoes the REQUESTED model, so it cannot name it)."""
    row["conv"] = conv_id(key)
    row["pop"] = population(row)
    is_local = served is not None and row.get("reason") is None
    if is_local and not row.get("served_model") and backend_model:
        row["served_model"] = backend_model
    reason = row.get("reason")
    if (row.get("local_mode") or {}).get("pin_change") == "local->claude":
        trigger = row.get("loop_guard_trigger")
        cause = f"{reason}:{trigger}" if trigger else str(reason)
        lm.set_pin_obs(key, cause)
        row["pin_cause"], row["pin_age_steps"] = cause, 0
        return
    st = lm.pin_obs.get(key)
    if st is None:
        return
    if is_local:
        lm.pin_obs.pop(key, None)  # served local again: the pin is gone
        return
    st["age"] += 1
    if reason in PINNED_REASONS:
        row["pin_cause"], row["pin_age_steps"] = st["cause"], st["age"]


def is_candidate(row: dict) -> bool:
    """A step that reached the local decision as an eligible candidate: not forced, exempt, a side call, a
    marker, a kill switch or a preflight refusal."""
    lmr = row.get("local_mode")
    if not isinstance(lmr, dict) or lmr.get("forced") or row.get("step_class") == STEP_SIDE_CALL:
        return False
    return lmr.get("reason") not in (local_mode.REASON_KILL_SWITCH, local_mode.REASON_CLAUDE_MARKER,
                                     local_mode.REASON_EXEMPT_SESSION, local_mode.REASON_PREFLIGHT_REFUSED,
                                     local_mode.REASON_NOT_ELIGIBLE)
