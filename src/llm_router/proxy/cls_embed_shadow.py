"""PR-4: the embedding classifier's SHADOW lane (``LLM_ROUTER_CLS_EMBED=shadow``; off = nothing here runs).

After the rules have decided a human turn (``server._local_step``), :func:`schedule` starts ONE detached
task that asks :mod:`cls_embed` for a verdict and appends it to ``cls_embed_shadow.jsonl``. The verdict never
reaches the decision: rules decide, the shadow only measures (gates G2 latency, G2b serving harm, G3, G3b).

Design (REVIEW-PR956 section 4, each point tested):

* **never awaited by the request.** ``schedule`` is synchronous, creates a task and returns; the forward and the
  hedge do not know it exists.
* **dedicated executor.** The assemble + embed work runs on its own small ``ThreadPoolExecutor``
  (``MAX_PENDING`` workers), never the loop's default executor, so a slow embed server cannot starve
  ``to_thread`` callers (warm-up, token estimates, ledger work).
* **hard budget.** The task is ``asyncio.wait_for(..., budget)``; at the budget it records an abstain
  (``timeout``). A thread already running cannot be killed; it keeps its slot until it ends, so a stuck server
  fills the lane and later turns are dropped (``queue_full``) instead of queueing without bound.
* **one embed per human turn.** Only rows with a human ``turn_origin`` are embedded (a tool-result
  continuation of the same turn is not), plus a bounded ``(session_id, text_sha)`` set against retries.
* **never for** a ``claude_marker`` row, a side call / ``not_eligible`` call (permission checks, D-594: they
  never reach ``_local_step``; ``step_class == side_call`` is also refused here), a secret-holding text.
* **record choice: a follow-up record in its own file, not a ledger-row field.** A row is written when the
  response ends, often before the embed has answered; stamping the row "only if ready" would drop every slow
  call from G2 (survivorship: the latency tail is the thing G2 measures) and mutate rows after the fact. The
  follow-up record is written whether the verdict is ready in time or not (an abstain carries ``timeout``), is
  joined by ``(session_id, text_sha, ts)`` (``msg_id`` is added when the reply has named it by then), and leaves
  every ledger row, the off-golden and the tier-decision golden untouched.
* the record holds numbers and codes (``cls_emb_*`` from ``Verdict.as_record``), hashes and step labels: no text.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_router import failopen, state_files
from llm_router.paths import state_path
from llm_router.proxy import cls_embed
from llm_router.proxy.cls_input import assemble
from llm_router.proxy.steps import ORIGIN_COMMAND, ORIGIN_SUBAGENT_BRIEF, ORIGIN_TYPED, STEP_SIDE_CALL, newest_human_text

SHADOW_NAME = "cls_embed_shadow.jsonl"
KIND = "cls_embed_shadow"
MAX_PENDING = cls_embed.MAX_IN_FLIGHT  # workers = pending cap: a pending job always has a worker
SEEN_MAX = 2048
HUMAN_ORIGINS = (ORIGIN_TYPED, ORIGIN_COMMAND, ORIGIN_SUBAGENT_BRIEF)
FO_SHADOW = "LR-FO-CLS-EMBED-SHADOW"

# What ``schedule`` returns (the proxy ignores it; tests read it).
OFF = "off"
SKIP_MARKER = "skipped_marker"
SKIP_SIDE_CALL = "skipped_side_call"
SKIP_NOT_HUMAN = "skipped_not_human_turn"
SKIP_SECRET = "skipped_secret"
SKIP_KNOWN = "skipped_known"
SKIP_NO_LOOP = "skipped_no_loop"
SCHEDULED = "scheduled"
DROPPED = "dropped_queue_full"


class _Lane:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=MAX_PENDING, thread_name_prefix="cls-embed-shadow")
        self.pending = 0
        self.seen: OrderedDict = OrderedDict()
        self.tasks: set = set()


_lane: _Lane | None = None
_lane_lock = threading.Lock()


def _get_lane() -> _Lane:
    global _lane
    with _lane_lock:
        if _lane is None:
            _lane = _Lane()
        return _lane


def shutdown() -> None:
    """Stop the pool without waiting for a running embed (its own timeout ends it)."""
    global _lane
    with _lane_lock:
        lane, _lane = _lane, None
    if lane is not None:
        lane.pool.shutdown(wait=False, cancel_futures=True)


def reset() -> None:  # tests
    shutdown()


def shadow_path() -> Path:
    return state_path(SHADOW_NAME)


def _job(snapshot: dict) -> cls_embed.Verdict:
    """Runs on the lane's own thread: assemble the classifier input, then embed it. Returns the verdict."""
    assembled = assemble(snapshot)
    return cls_embed.classify(str(assembled))


def _append(rec: dict, path: Path | None) -> None:
    target = path or shadow_path()
    try:
        with state_files.open_private(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError as exc:
        failopen.record(FO_SHADOW, exc, detail="write")


def _record(row: dict, decision, verdict: cls_embed.Verdict, *, late: bool, path: Path | None) -> None:
    reason = getattr(decision, "reason", None)
    rec = {"kind": KIND, "ts": row.get("ts"), "wrote_ts": time.time(), "session_id": row.get("session_id"),
           "text_sha": row.get("text_sha"), "msg_id": row.get("msg_id"), "pop": row.get("turn_origin"),
           "step_class": row.get("step_class"),
           "rules_decision": "local" if getattr(decision, "local", False) else (reason or "claude"),
           "late": late}
    rec.update(verdict.as_record())
    _append(rec, path)


def schedule(body: dict, row: dict, decision, path: Path | None = None) -> str:
    """Start the shadow verdict for this call and return at once. Never raises, never awaits, never changes
    ``decision``. Call it after the rules have decided; flag off returns before anything is read."""
    try:
        if not cls_embed.enabled():
            return OFF
        if getattr(decision, "reason", None) == "claude_marker":
            return SKIP_MARKER
        if row.get("step_class") == STEP_SIDE_CALL or row.get("reason") == "not_eligible":
            return SKIP_SIDE_CALL
        if row.get("turn_origin") not in HUMAN_ORIGINS:
            return SKIP_NOT_HUMAN
        text = newest_human_text(body)
        if not text.strip():
            return SKIP_NOT_HUMAN
        from llm_router import door_verdict
        if door_verdict._holds_secret(text):
            return SKIP_SECRET
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return SKIP_NO_LOOP
        lane = _get_lane()
        key = (row.get("session_id") or "", row.get("text_sha") or "")
        with lane.lock:
            if key[1] and key in lane.seen:
                return SKIP_KNOWN
            lane.seen[key] = True
            while len(lane.seen) > SEEN_MAX:
                lane.seen.popitem(last=False)
            full = lane.pending >= MAX_PENDING
            if not full:
                lane.pending += 1
        t0 = time.monotonic()
        if full:
            _record(row, decision, cls_embed.Verdict(None, cls_embed.SRC_QUEUE_FULL, 0.0, queue_depth=MAX_PENDING),
                    late=False, path=path)
            return DROPPED
        snapshot = {"messages": list(body.get("messages") or []), "system": body.get("system")}
        task = loop.create_task(_run(lane, snapshot, row, decision, t0, path))
        lane.tasks.add(task)
        task.add_done_callback(lane.tasks.discard)
        return SCHEDULED
    except Exception as exc:  # noqa: BLE001 - a shadow must never cost a call
        failopen.record(FO_SHADOW, exc, detail="schedule")
        return OFF


async def _run(lane: _Lane, snapshot: dict, row: dict, decision, t0: float, path: Path | None) -> None:
    try:
        cf = lane.pool.submit(_job, snapshot)
    except RuntimeError:  # pool shut down
        with lane.lock:
            lane.pending -= 1
        return

    def _release(_f) -> None:  # the slot frees when the THREAD ends, not when the budget does
        with lane.lock:
            lane.pending -= 1
    cf.add_done_callback(_release)
    budget = cls_embed.budget_s()
    try:
        verdict = await asyncio.wait_for(asyncio.wrap_future(cf), timeout=budget)
        late = False
    except asyncio.TimeoutError:
        verdict = cls_embed.Verdict(None, cls_embed.SRC_TIMEOUT, round((time.monotonic() - t0) * 1000.0, 1),
                                    queue_depth=lane.pending)
        late = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        failopen.record(FO_SHADOW, exc, detail="run")
        verdict = cls_embed.Verdict(None, cls_embed.SRC_ERROR, round((time.monotonic() - t0) * 1000.0, 1))
        late = False
    try:
        _record(row, decision, verdict, late=late, path=path)
    except Exception as exc:  # noqa: BLE001
        failopen.record(FO_SHADOW, exc, detail="record")


async def drain() -> None:
    """Wait for the pending tasks (tests, shutdown)."""
    lane = _lane
    if lane is not None and lane.tasks:
        await asyncio.gather(*list(lane.tasks), return_exceptions=True)
