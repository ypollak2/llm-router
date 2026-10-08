"""The proxy's classifier shadow seam (M1.6): log the local LLM verdict next to the rules'.

With ``LLM_ROUTER_LOCAL_CLASSIFIER`` = ``shadow``, ``server.decide_tier`` hands each
turn-first request to :meth:`ShadowScheduler.maybe_schedule` AFTER the rules have
decided. The scheduler never changes that decision and never waits for the model:

* ``off`` (or anything unknown): returns at once, zero Ollama calls, no state touched;
* a continuation or a side call (no client tools) never schedules anything;
* a turn already classified (cache hit) or being classified (same session and text)
  is skipped, so one text is one call;
* at most :data:`MAX_PENDING` classifications are pending (running or waiting for the
  single Ollama slot); a turn beyond that is dropped and counted, never queued;
* the classification itself is a detached task: the input is assembled in a worker
  thread (``assemble`` reads at most the last 400 messages and stops once it has its
  context, P1.7-c) and the call goes through ``local_classifier.classify_async``, so
  the event loop only schedules a task. ``cls_applied`` stays false on every ledger row.

The classifier backend is ``local_classifier``'s: ``LLM_ROUTER_CLASSIFIER_BACKEND=systemone``
makes the shadow ask the decision model (``decision_classifier``, nimble:9b by default).
Each record carries the backend, the tier the client really ``requested`` on the call and
whether the input was ``assemble_capped``; ``shadow_eval`` scores records against the rules.

Each finished task appends one record to ``classifier_shadow.jsonl`` in the state
dir: hashes, tiers, numbers and reason codes, never prompt text. The one exception is
opt-in and separate: with ``LLM_ROUTER_SHADOW_TEXT_SAMPLE`` set, a sampled turn also
leaves its text in ``shadow_text.jsonl`` (0600, see ``shadow_text``) for an offline labeller. A dropped turn
appends a ``classifier_shadow_drop`` record. ``llm-router kpi`` reads both
(``classifier_shadow`` line); nothing else does, so NS, D1 and D2 cannot move.

``on`` is M2's mode. In M1 it behaves exactly like ``shadow``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from llm_router import failopen, local_classifier, shadow_eval
from llm_router.proxy import shadow_text
from llm_router.proxy.cls_input import assemble
from llm_router.proxy.steps import STEP_CONTINUATION, has_client_tools

SHADOW_NAME = "classifier_shadow.jsonl"
KIND = "classifier_shadow"
KIND_DROP = "classifier_shadow_drop"
MAX_PENDING = 4

# What the scheduler returns (the proxy ignores it; tests and logs read it).
OFF = "off"
SKIPPED_CONTINUATION = "skipped_continuation"
SKIPPED_SIDE_CALL = "skipped_side_call"
SKIPPED_NO_KEY = "skipped_no_key"
SKIPPED_KNOWN = "skipped_known"
SCHEDULED = "scheduled"
DROPPED = "dropped"


def shadow_path() -> Path:
    from llm_router import paths

    return paths.state_path(SHADOW_NAME)


def read_records(path: Path | None = None, days: float | None = None) -> list[dict]:
    """Every record of either kind in the window, oldest first as written. A line that
    is not a JSON object, or has no numeric ``ts`` when a window is asked, is skipped."""
    target = path or shadow_path()
    if not target.exists():
        return []
    cutoff = time.time() - days * 86400 if days else None
    out: list[dict] = []
    try:
        with target.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("kind") not in (KIND, KIND_DROP):
                    continue
                if cutoff is not None and not (isinstance(rec.get("ts"), (int, float)) and rec["ts"] >= cutoff):
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


class ShadowScheduler:
    """One per proxy app. ``path`` overrides the record file (tests)."""

    def __init__(self, path: Path | None = None, *, max_pending: int = MAX_PENDING) -> None:
        self.path = path
        self.max_pending = max_pending
        self.drops = 0
        self._tasks: set[asyncio.Task] = set()
        self._keys: set[tuple] = set()
        self._slot: asyncio.Semaphore | None = None  # one Ollama slot; made on first use, inside the loop

    # -- the request path -----------------------------------------------------------

    def maybe_schedule(self, body: dict, row: dict) -> str:
        """Called by ``decide_tier`` after the decision is on ``row``. Constant time,
        never awaits, never raises into the request. Returns what it did."""
        try:
            return self._maybe_schedule(body, row)
        except Exception as exc:  # noqa: BLE001 - shadow must never cost a call
            failopen.record("LR-FO-PROXY-CLASSIFIER-SHADOW", exc)
            return OFF

    def _maybe_schedule(self, body: dict, row: dict) -> str:
        if local_classifier.mode() == "off":
            return OFF
        if row.get("step_class") == STEP_CONTINUATION:
            return SKIPPED_CONTINUATION
        if not has_client_tools(body):
            return SKIPPED_SIDE_CALL
        text_sha, session_id = row.get("text_sha"), row.get("session_id")
        if not text_sha:
            return SKIPPED_NO_KEY
        key = (session_id or "", text_sha)
        if key in self._keys or local_classifier.is_cached(session_id, text_sha):
            return SKIPPED_KNOWN
        fields = self._fields(row)
        if len(self._tasks) >= self.max_pending:
            self.drops += 1
            self._append({"kind": KIND_DROP, **{k: fields[k] for k in
                          ("ts", "session_id", "text_sha", "step_class", "session_kind")}})
            return DROPPED
        # Shallow copies of the two lists the assembler reads: nothing downstream
        # edits a message in place (every rewrite builds a new body).
        snapshot = {"messages": list(body.get("messages") or []), "system": body.get("system")}
        task = asyncio.get_running_loop().create_task(self._run(key, snapshot, fields))
        self._keys.add(key)
        self._tasks.add(task)
        task.add_done_callback(lambda t, k=key: (self._tasks.discard(t), self._keys.discard(k)))
        return SCHEDULED

    @staticmethod
    def _fields(row: dict) -> dict:
        """What the record keeps from the ledger row, copied now: ``row`` goes on to
        change (usage, status) after the decision."""
        return {
            "ts": row.get("ts"), "session_id": row.get("session_id"), "text_sha": row.get("text_sha"),
            "session_kind": row.get("session_kind"), "step_class": row.get("step_class"),
            "rules": {"task_type": row.get("tier_task_type"), "complexity": row.get("tier_complexity"),
                      "tier": row.get("tier_proposed")},
            "tier_reason_live": row.get("tier_reason"), "tier_live": row.get("tier"),
            "requested_tier": row.get("requested_model"), "policy_version": row.get("tier_policy_version"),
            # The tier the client really requested on THIS call (P1.7, M1-12 clamp-aware cost):
            # the record is its own join to the proxy row, no timestamp window needed.
            "requested": shadow_eval.tier_name(row.get("requested_model")),
        }

    # -- the detached task ----------------------------------------------------------

    async def _run(self, key: tuple, snapshot: dict, fields: dict) -> None:
        try:
            assembled = await asyncio.to_thread(assemble, snapshot)
            if not assembled.prompt.strip():
                return  # nothing typed: nothing to classify
            if self._slot is None:
                self._slot = asyncio.Semaphore(1)
            async with self._slot:
                verdict = await local_classifier.classify_async(
                    assembled, session_id=fields["session_id"], text_sha=fields["text_sha"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify_async never raises; assemble or the thread might
            failopen.record("LR-FO-PROXY-CLASSIFIER-SHADOW-RUN", exc)
            return
        if verdict.source in ("off", "cache"):
            return  # mode switched off while queued, or another caller already logged this turn
        await self._save_text(snapshot, assembled, fields)
        self._append({"kind": KIND, **fields, "llm": verdict.as_log(), "source": verdict.source,
                      "ms": verdict.ms, "model": verdict.model, "prompt_version": verdict.prompt_version,
                      "backend": local_classifier.backend(),
                      "assemble_capped": getattr(assembled, "capped", None)})

    async def _save_text(self, snapshot: dict, assembled, fields: dict) -> None:
        """The text sidecar (``shadow_text``): off unless ``LLM_ROUTER_SHADOW_TEXT_SAMPLE`` is set, and then
        only a sampled turn does any work. Fail open: it never costs the shadow record its write."""
        try:
            if not shadow_text.wanted(fields.get("text_sha"), fields.get("ts")):
                return
            await asyncio.to_thread(shadow_text.maybe_record, shadow_text.sidecar_path(self.path),
                                    snapshot, assembled.context, fields)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the exception type is recorded, never its message
            failopen.record("LR-FO-PROXY-CLASSIFIER-SHADOW-TEXT", exc)

    def _append(self, rec: dict) -> None:
        target = self.path or shadow_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError as exc:
            failopen.record("LR-FO-PROXY-CLASSIFIER-SHADOW-WRITE", exc)

    # -- lifecycle ------------------------------------------------------------------

    async def drain(self) -> None:
        """Wait for every pending task (tests, graceful shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel what is pending, wait for it to end, and close the process's one
        classifier HTTP session (it is made again on the next call)."""
        for t in list(self._tasks):
            t.cancel()
        await self.drain()
        await local_classifier.aclose()
