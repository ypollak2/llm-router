"""PR-7: agent-turn AUTHORITY for the embedding classifier (a capability, OFF by default).

``LLM_ROUTER_CLS_EMBED_AUTHORITY=decide`` lets a decided ``local`` verdict from :mod:`cls_embed` turn a rules
"Claude" decision into "local" for ONE kind of refusal: a plain conversation pin to Claude (not a structural pin,
not a hard pin, not ``started_elsewhere``). Any other value, or the flag unset, is off and nothing here runs.

Where it plugs in: ``server._local_step`` asks :func:`cls_decide.eligible_verdict` with ``embed=True`` (the
backend swap), and re-runs the whole rule set (``decide(..., cls_local=True)``), so every hard limit is checked
again: kill switch, ``claude_marker``, exempt session, hard pins, media, prompt cap, backend, breaker, veto; the
cap guard and the cold guard run after the flip and can still refuse. This module adds no refusal of its own to
lift and has no way to lift one: it only answers "local", "claude" or abstain.

Never local, by construction:

* permission checks / ``not_eligible`` side calls: they never reach ``_local_step`` (tool-less), and
  :func:`allowed_row` refuses ``step_class == side_call`` and ``reason == not_eligible`` as well (D-594).
* a ``claude`` verdict, an abstain, a timeout, a missing artifact, an open breaker, a full queue: the rules decide.

Budget (G4): the classify runs on this module's own ``ThreadPoolExecutor`` (never the loop's default executor)
inside ``asyncio.wait_for`` at the embed budget (``cls_embed.budget_s()``, 250 ms default); over it the answer is
an abstain (``timeout``). A thread already running cannot be killed, so it keeps its slot until it ends; with
all slots busy the next call abstains (``queue_full``) instead of queueing. A thread ends within the embed
socket deadline (``cls_embed._post_embed`` enforces a TOTAL deadline), so interpreter exit is bounded (F2).

Kill file ``cls_embed_authority.off`` in the state dir, checked on every call (a plist key needs a restart).

Records are numbers and codes only (``cls_emb_*`` from ``Verdict.as_record``, ``cls_opened``); the text, the
vector and the artifact path never leave :mod:`cls_embed`.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from llm_router import failopen
from llm_router.paths import state_path
from llm_router.proxy import cls_embed
from llm_router.proxy.cls_input import assemble
from llm_router.proxy.steps import STEP_SIDE_CALL

FLAG_ENV = "LLM_ROUTER_CLS_EMBED_AUTHORITY"
KILL_FILE = "cls_embed_authority.off"
MAX_PENDING = cls_embed.MAX_IN_FLIGHT
FO_AUTHORITY = "LR-FO-CLS-EMBED-AUTHORITY"

#: Ledger fields set only when the flag is on (flag off: no new key on any row, goldens unchanged).
OPENED_FIELD = "cls_opened"
OPENED_BY_FIELD = "cls_opened_by"
OPENED_BY = "embed"


def flag_on() -> bool:
    return os.environ.get("LLM_ROUTER_CLS_EMBED_AUTHORITY", "").strip().lower() == "decide"


def killed() -> bool:
    try:
        return state_path(KILL_FILE).exists()
    except OSError:
        return True  # cannot tell: stay off


def active() -> bool:
    """Authority is live: its flag, the embed backend on (``LLM_ROUTER_CLS_EMBED=shadow``) and no kill file."""
    return flag_on() and cls_embed.enabled() and not killed()


def allowed_row(row: dict) -> bool:
    """False for a permission check / side call (D-594). Defence in depth: they never reach the local step."""
    return row.get("step_class") != STEP_SIDE_CALL and row.get("reason") != "not_eligible"


class _Lane:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=MAX_PENDING, thread_name_prefix="cls-embed-authority")
        self.pending = 0


_lane: _Lane | None = None
_lane_lock = threading.Lock()
_closed = False


def _get_lane() -> _Lane | None:
    global _lane
    with _lane_lock:
        if _closed:
            return None
        if _lane is None:
            _lane = _Lane()
        return _lane


def reopen() -> None:
    """Proxy start: a lane closed by an earlier shutdown in this process (tests) accepts work again."""
    global _closed
    with _lane_lock:
        _closed = False


def reset() -> None:  # tests (no loop running) and shutdown
    global _lane, _closed
    with _lane_lock:
        lane, _lane = _lane, None
        _closed = False
    if lane is not None:
        lane.pool.shutdown(wait=False, cancel_futures=True)


async def aclose() -> None:
    """Shutdown: stop the pool without waiting for a running embed (its own deadline ends it) and refuse new work."""
    global _lane, _closed
    with _lane_lock:
        lane, _lane = _lane, None
        _closed = True
    if lane is not None:
        lane.pool.shutdown(wait=False, cancel_futures=True)


def _job(snapshot: dict) -> cls_embed.Verdict:
    return cls_embed.classify(str(assemble(snapshot)))


def _abstain(source: str, t0: float, depth: int = 0) -> cls_embed.Verdict:
    return cls_embed.Verdict(None, source, round((time.monotonic() - t0) * 1000.0, 1), queue_depth=depth)


async def verdict_for(snapshot: dict) -> cls_embed.Verdict:
    """The embed verdict for ``snapshot`` (``{"messages", "system"}``), hard-budgeted. Never raises; every
    non-decision is an abstain whose ``source`` says why. The caller has already checked the secret scan and
    the human-turn rule (``cls_decide.eligible_verdict``)."""
    t0 = time.monotonic()
    try:
        if not active():
            return _abstain(cls_embed.SRC_KILLED if killed() else cls_embed.SRC_DISABLED, t0)
        lane = _get_lane()
        if lane is None:
            return _abstain(cls_embed.SRC_DISABLED, t0)
        with lane.lock:
            if lane.pending >= MAX_PENDING:
                return _abstain(cls_embed.SRC_QUEUE_FULL, t0, lane.pending)
            lane.pending += 1
        try:
            cf = lane.pool.submit(_job, snapshot)
        except RuntimeError:  # pool shut down between the check and the submit
            with lane.lock:
                lane.pending -= 1
            return _abstain(cls_embed.SRC_DISABLED, t0)

        def _release(_f) -> None:  # the slot frees when the THREAD ends, not when the budget does
            with lane.lock:
                lane.pending -= 1
        cf.add_done_callback(_release)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(cf), timeout=cls_embed.budget_s())
        except asyncio.TimeoutError:
            return _abstain(cls_embed.SRC_TIMEOUT, t0, lane.pending)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - authority must never cost a call
        failopen.record(FO_AUTHORITY, exc)
        return _abstain(cls_embed.SRC_ERROR, t0)
