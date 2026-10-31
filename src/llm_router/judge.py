"""LLM-as-Judge quality evaluation — queue on the hot path, grade out of band.

CHZ-JUDGE-QUEUE (2026-09-24). The live ledger has graded 0 of 1,608 routing
decisions. Three causes, all on the path that used to call
``evaluate_response_async`` synchronously from the hot path:

  a. Some callers (the DIRECT/hook path) never passed ``response=`` to
     ``cost.log_routing_decision``, so the judge trigger's ``if success and
     response:`` gate never fired for them at all.
  b. The judge hardcoded a PAID model (``claude-haiku-4-5-20251001``). With
     no Anthropic key configured, ``call_llm`` raised — and the bare
     ``except Exception: pass`` this module used to have swallowed it with
     no record anywhere.
  c. ``evaluate_response_async`` only *creates* an asyncio task; a caller
     running under ``asyncio.run(...)`` (as the hooks do) tears down the
     event loop on return, cancelling that task before it can run.

Fix, per the owner decision: QUEUE AND GRADE LATER. The hot path
(:func:`enqueue_for_grading`) makes no network call and creates no asyncio
task — it is a synchronous, sampled, local file append. Grading happens out
of band via :func:`drain_queue`, spawned detached from session start (see
``hooks/session-start.py::_drain_judge_queue_bg``) and reachable directly via
``llm-router judge drain``. The grader picks an INDEPENDENT judge model (never
the model that answered — see :func:`_select_judge_model`) and, when none is
available with zero API keys, leaves the row ungraded rather than writing a
fabricated or zero score.

Sample rate: LLM_ROUTER_JUDGE_SAMPLE_RATE (default 0.1 = 10% of calls).
Scores stored in routing_decisions table for aggregation and quality penalties.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta

from llm_router import paths
from llm_router.cost import _get_db
from llm_router.providers import call_llm

#: Filename of the on-disk judge queue, inside LLM_ROUTER_HOME when isolated
#: (see llm_router.paths). One JSON object per line: routing_decision_id,
#: prompt, response, task_type, answering_model, queued_at.
_QUEUE_FILENAME = "judge_queue.jsonl"


def _queue_path():
    """Path to the judge queue. Resolved fresh on every call (paths.py)."""
    return paths.state_path(_QUEUE_FILENAME)

# GH#75: fire-and-forget tasks created below outlive the test that spawned
# them. pytest-asyncio's `asyncio_default_fixture_loop_scope = "session"`
# means every test shares ONE event loop, so a judge task created by test A
# (via `asyncio.create_task`, never awaited) can still be pending when test B
# starts. Since `call_llm` -> `litellm.acompletion` is patched globally by
# whichever test currently holds a `with patch("litellm.acompletion", ...)`
# block, test A's orphaned judge call gets dispatched through test B's mock
# and silently overwrites test B's captured request kwargs with
# `model="claude-haiku-4-5-20251001"` — the exact "wrong provider" flake
# reported in GH#75 (test_integration.py comparing captured["model"] against
# the test's own override). Tracking every scheduled task here lets a test
# fixture (`drain_pending_judge_tasks`) await/cancel them before the next
# test runs, instead of letting them survive into it.
_pending_tasks: set[asyncio.Task] = set()


async def evaluate_response_async(
    prompt: str,
    response: str,
    task_type: str,
    routing_decision_id: int | None = None,
) -> None:
    """Fire-and-forget background evaluation of LLM response.

    Runs asynchronously without blocking the primary call. Scores the response
    on relevance, completeness, and correctness using a cheap model, then
    stores the composite score in routing_decisions.

    Args:
        prompt: Original user prompt that generated the response
        response: The LLM response to evaluate
        task_type: Type of task (query, code, generate, analyze, research)
        routing_decision_id: ID of the routing_decisions row to update with score
    """
    # Sample rate check: only evaluate sample_rate% of calls
    sample_rate = float(__import__("os").environ.get("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.1"))
    if random.random() > sample_rate:
        return

    # Fire background task without awaiting
    task = asyncio.create_task(_evaluate_background(prompt, response, task_type, routing_decision_id))
    # Track real Task/Future objects only — tests that patch
    # `asyncio.create_task` itself get a MagicMock back, which is neither
    # awaitable by `asyncio.wait` nor a real leak risk.
    if isinstance(task, asyncio.Future):
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)


async def drain_pending_judge_tasks(timeout: float = 2.0) -> None:
    """Test-only helper: await (or cancel) every in-flight judge task.

    GH#75: without this, a judge task scheduled by one test can execute
    during a LATER test's `await`s, on the session-wide event loop, and hit
    whatever mock that later test currently has installed. Call this from an
    autouse fixture after each test so no task ever crosses a test boundary.
    """
    if not _pending_tasks:
        return
    pending = list(_pending_tasks)
    _done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for t in still_pending:
        t.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
    _pending_tasks.difference_update(pending)


def enqueue_for_grading(
    *,
    routing_decision_id: int | None,
    prompt: str,
    response: str,
    task_type: str,
    answering_model: str,
    door: str | None = None,
) -> bool:
    """Hot-path-safe: sample and append one response to the judge queue.

    No network call, no asyncio task — a single local file append, gated by
    the same ``LLM_ROUTER_JUDGE_SAMPLE_RATE`` knob ``evaluate_response_async``
    used to read. Call this from the routing hot path instead of the old
    ``evaluate_response_async``; :func:`drain_queue` does the actual grading
    out of band.

    Returns:
        True if an entry was written, False if skipped (sampled out, no
        routing_decision_id, or the write itself failed — recorded via
        failopen rather than raised, exactly like every other hot-path
        telemetry write in this codebase).
    """
    if routing_decision_id is None:
        return False
    from llm_router import local_privacy
    if local_privacy.private_dispatch_active():
        # LC-12g / D-277: a local-only text job's prompt (the user's diff) is never queued; LC-12h: nor a prompt
        # the secret door kept local. The drain sends a
        # queued prompt to another model through LiteLLM's shared client (proxy env), or to Haiku when
        # LLM_ROUTER_JUDGE_HAIKU_FALLBACK is on.
        return False
    from llm_router import judge_sampler
    if not judge_sampler.pick(door):
        return False

    from llm_router import judge_hygiene
    text = judge_hygiene.prepare(prompt, response)
    if text is None:
        return False  # secret or unredactable text: the whole entry is skipped, recorded without text
    entry = {
        "door": judge_sampler.normalize_door(door),
        "routing_decision_id": routing_decision_id,
        "prompt": text[0],
        "response": text[1],
        "task_type": task_type,
        "answering_model": answering_model,
        "queued_at": time.time(),
        judge_hygiene.PREPARED_KEY: True,
    }
    try:
        path = _queue_path()
        try:
            size_before = path.stat().st_size
        except FileNotFoundError:
            size_before = 0
        judge_hygiene.append_private(path, [json.dumps(entry)])
        _enforce_queue_cap(path, size_before)
        return True
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-JUDGE-QUEUE-WRITE", exc)
        return False


#: Hard cap on the number of queued (undrained) entries. Enforced at ENQUEUE
#: time, not only at drain — an operator who never runs `llm-router judge
#: drain` and has LLM_ROUTER_JUDGE_AUTODRAIN=0 (or whose session-start drain
#: never fires) must still get bounded disk growth, not an unbounded log.
#: Overridable via LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES.
_DEFAULT_QUEUE_MAX_ENTRIES = 2000


def _queue_max_entries() -> int:
    raw = os.environ.get("LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES", "")
    try:
        return int(raw) if raw else _DEFAULT_QUEUE_MAX_ENTRIES
    except ValueError:
        return _DEFAULT_QUEUE_MAX_ENTRIES


#: A queue file this small is checked on every enqueue (reading it costs well under
#: a millisecond); a larger one only when an append crosses a multiple of the stride.
#: Size comes from one ``stat``; the queue is never re-read per enqueue on the hook
#: path. Between checks the queue can overshoot the entry cap by at most a stride.
_EXACT_CHECK_BYTES = 64 * 1024
_CHECK_STRIDE_BYTES = 256 * 1024
#: JUDGECAP: a cap trim on a large queue leaves at least this much room below the next
#: stride line, dropping extra oldest entries when it must. Without it a queue AT the cap
#: sitting just under a line was fully read and rewritten on every enqueue (each append
#: crossed the line, each trim went back under it). With it, the next full check after a
#: trim needs at least this many appended bytes. An expiry purge does NOT get the headroom
#: (it would evict unexpired entries, even with the cap off); see the docstring below.
_TRIM_HEADROOM_BYTES = _CHECK_STRIDE_BYTES // 4


def _enforce_queue_cap(path, size_before: int = 0) -> int:
    """Keep at most `_queue_max_entries()` newest lines, and drop expired ones.

    Drops the OLDEST entries first — a queue nobody has drained in a while
    should still prefer grading recent traffic once something finally does
    drain it. Runs at enqueue time (not just at drain time) so growth stays
    bounded even if nothing ever drains the queue; the same pass physically
    purges entries past the retention bound (``judge_hygiene.purge_expired``).

    On a queue above ``_EXACT_CHECK_BYTES`` the cap trim also drops the oldest
    entries needed to end at least ``_TRIM_HEADROOM_BYTES`` below the next stride
    line: less than that plus one entry beyond the cap (up to ~25% of a queue near
    the 256 KiB line, ~6% near 1 MiB; one entry of two at cap 2 with entries over
    64 KiB). The newest entry is always kept. After a trim the next full read needs
    ``_TRIM_HEADROOM_BYTES`` of appends; after a check that removed nothing, a stride.

    Not bounded (pre-existing, kept on purpose): the expiry purge removes only expired
    entries, so a queue held just under a stride line by about one expiry per enqueue
    is still fully read on every enqueue, as before
    (``test_expiry_purge_never_drops_an_unexpired_entry``: 400 reads in 400 enqueues).
    Giving the purge the headroom stopped that but evicted unexpired entries under
    ordinary traffic, even with the cap off (PR #800 review). Correctness over speed.

    Never raises — a failure here must not break the enqueue that triggered
    it. Returns the number of entries dropped for the cap (0 if within cap or
    not checked this time).
    """
    cap = _queue_max_entries()
    dropped = 0
    try:
        size = path.stat().st_size
        if size > _EXACT_CHECK_BYTES and size_before // _CHECK_STRIDE_BYTES == size // _CHECK_STRIDE_BYTES:
            return 0
        from llm_router import judge_hygiene
        judge_hygiene.purge_expired(path)  # retention holds even when the entry cap is off
        judge_hygiene.purge_orphans(path)
        if cap <= 0:
            return 0
        with open(path) as f:
            lines = [line for line in f.readlines() if line.strip()]
        if len(lines) <= cap:
            return 0
        kept = lines[-cap:]
        kept_bytes = sum(len(line.encode("utf-8")) + (not line.endswith("\n")) for line in kept)
        start = 0
        while (start < len(kept) - 1 and kept_bytes > _EXACT_CHECK_BYTES
               and _CHECK_STRIDE_BYTES - kept_bytes % _CHECK_STRIDE_BYTES < _TRIM_HEADROOM_BYTES):
            kept_bytes -= len(kept[start].encode("utf-8")) + (not kept[start].endswith("\n"))
            start += 1
        kept = kept[start:]
        dropped = len(lines) - len(kept)
        judge_hygiene.rewrite_private(path, kept)
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-JUDGE-QUEUE-CAP", exc)
        return 0

    # Visible, not silent (per this repo's failopen convention): the count
    # itself lives in the `detail` string since failopen.record's own
    # counters are per-CODE occurrence counts, not accumulators.
    from llm_router import failopen
    failopen.record("CHZ-FO-JUDGE-QUEUE-CAP-DROPPED", detail=f"dropped={dropped}")
    return dropped


def _select_judge_model(answering_model: str) -> str | None:
    """Pick an INDEPENDENT judge model, or None when none may grade this answer.

    Grading a response with the model that produced it is not independent
    verification. The decision lives in :mod:`llm_router.judge_select` (canonical
    identity, strongest other local model, config-only Haiku fallback behind a
    flag); no model or family is named in code (D-205). None means: leave the row
    ungraded, never fabricate a score and never write 0.
    """
    from llm_router import judge_select
    return judge_select.select_for(answering_model).judge_model


#: Marker inserted into every claim's work-file name, so orphan recovery can
#: find them by prefix (`_recover_orphaned_claims`) without also picking up
#: unrelated files.
_DRAINING_MARKER = ".draining."

#: An orphaned `.draining.*` file (left by a drain that crashed between the
#: rename and the read+delete) is only recovered once it's older than this —
#: comfortably longer than drain_queue's own default time budget plus
#: grading latency, so a recovery pass never steals a claim a LIVE drain is
#: still actively reading. Wall-clock, deliberately: file mtimes are
#: wall-clock timestamps, so this must compare against time.time(), not
#: time.monotonic() (see this repo's CLAUDE.md on the two clocks — that rule
#: is about measuring a DURATION across a possible sleep, not about matching
#: an mtime's own clock domain).
_ORPHAN_RECOVERY_AGE_S = 120.0


def _work_path(path) -> str:
    """A claim work-file name unique to THIS call, THIS process.

    CHZ-JUDGE-QUEUE-CONCURRENCY. This used to be a fixed `<queue>.draining`
    name shared by every claim. Two drains running at once (two
    `llm-router judge drain` invocations, or a manual run racing the
    session-start auto-drain) could then interleave: drain A renames the
    queue aside, the hot path writes a fresh queue file, drain B renames
    THAT aside to the SAME work path — silently overwriting drain A's
    still-unread claim. `os.replace` provides no warning when it clobbers an
    existing destination. Including the pid and a monotonic-nanosecond
    timestamp makes every claim's work file unique, so two concurrent claims
    can only ever land on disjoint sets of items, never overwrite each
    other's.
    """
    return f"{path}{_DRAINING_MARKER}{os.getpid()}.{time.monotonic_ns()}"


def _recover_orphaned_claims(path) -> int:
    """Requeue any `.draining.*` files stranded by a crashed drain.

    `_claim_queue` renames the queue aside before reading it; if the process
    is killed between that rename and the read+delete, the claimed items are
    stranded in a uniquely-named work file forever — invisible to the next
    drain unless something looks for them. Called at the start of every
    `drain_queue()` so a crash loses no rows, only delays them.

    Skips anything younger than `_ORPHAN_RECOVERY_AGE_S` — a fresh
    `.draining.*` file most likely belongs to a drain that is still running
    right now, not a crashed one, and recovering it out from under a live
    claim would grade the same rows twice.

    Never raises. Returns the number of rows recovered.
    """
    recovered = 0
    try:
        parent = path.parent
        if not parent.is_dir():
            return 0
        prefix = path.name + _DRAINING_MARKER
        now = time.time()
        for entry in parent.iterdir():
            if not entry.name.startswith(prefix):
                continue
            try:
                age_s = now - entry.stat().st_mtime
            except OSError:
                continue
            if age_s < _ORPHAN_RECOVERY_AGE_S:
                continue  # plausibly still in flight — leave it alone
            try:
                with open(entry) as f:
                    lines = f.readlines()
            except OSError:
                continue
            items = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if items:
                _requeue(path, items)
                recovered += len(items)
            try:
                entry.unlink()
            except OSError as exc:
                # The rows are already safely requeued at this point — a
                # failed cleanup only leaves a stale orphan file behind, it
                # does not lose data. Still recorded: an orphan that never
                # gets removed is otherwise a silent, permanent leak, and
                # this repo's rule is that a swallowed exception must leave
                # a trace (see llm_router.failopen's module docstring).
                from llm_router import failopen
                failopen.record("CHZ-FO-JUDGE-QUEUE-ORPHAN-CLEANUP", exc)
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-JUDGE-QUEUE-RECOVER", exc)
        return recovered
    return recovered


def _claim_queue(path) -> list[dict]:
    """Atomically claim every entry currently in the queue file.

    Renames the queue file aside (atomic on the same filesystem) before
    reading it, so a concurrent hot-path append lands in a fresh file rather
    than racing a reader that is also truncating. Malformed lines are
    dropped rather than aborting the whole claim — one bad line must not cost
    every other queued item its grade. The work-file name is unique per call
    (`_work_path`) so a second, concurrent claim can never collide with —
    and overwrite — this one's still-unread file.
    """
    work_path = _work_path(path)
    try:
        os.replace(str(path), work_path)
    except OSError:
        return []  # nothing queued (or a concurrent drain already claimed it)

    try:
        with open(work_path) as f:
            lines = f.readlines()
    finally:
        try:
            os.remove(work_path)
        except OSError as exc:
            # Already read at this point — a failed cleanup leaves a stale
            # work file behind (which _recover_orphaned_claims will requeue
            # and clean up later), it does not lose the rows. Recorded so a
            # cleanup that always fails on some filesystem is discoverable
            # instead of silently leaking `.draining.*` files forever.
            from llm_router import failopen
            failopen.record("CHZ-FO-JUDGE-QUEUE-CLAIM-CLEANUP", exc)

    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _requeue(path, items: list[dict]) -> None:
    """Append items back to the queue (e.g. overflow past one drain's batch
    size, or work left when the time budget ran out). Append is safe even if
    the hot path is concurrently writing new entries."""
    if not items:
        return
    try:
        from llm_router import judge_hygiene
        judge_hygiene.append_private(path, [json.dumps(item) for item in items])
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-JUDGE-QUEUE-WRITE", exc)


async def _grade_one(item: dict) -> str:
    """Grade a single queued item. Returns 'graded', 'ungraded', or 'failed'.

    'ungraded' means no independent judge was available — the row is left
    alone (judge_score stays NULL), never scored 0. 'failed' means a judge
    model WAS selected but the call itself failed (recorded via failopen
    inside ``_evaluate_background``).
    """
    answering_model = item.get("answering_model") or ""
    judge_model = _select_judge_model(answering_model)
    from llm_router import judge_select, judge_sampler, judge_ledger
    judge_kind = judge_select.kind_of(judge_model)

    def _record(status: str, score: float | None = None, ledger: dict | None = None) -> str:
        shown = ledger["outcome"] if ledger and status == "ungraded" else status
        judge_sampler.record_result(
            door=item.get("door"), answering_model=answering_model,
            judge_model=judge_model, judge_kind=judge_kind,
            status=judge_select.STATUS_NO_JUDGE if shown == "ungraded" else shown,
            score=score,
        )
        if ledger is not None:
            judge_ledger.record(
                door=item.get("door"), answering_model=answering_model, judge_model=judge_model,
                judge_kind=judge_kind, tokens=ledger.get("tokens"), cost_usd=ledger.get("cost_usd"),
                outcome=ledger["outcome"],
            )
        return status

    # Independence guard on canonical identity (name, tag, digest, family); it
    # refuses on doubt, even if selection were bypassed or misconfigured.
    if judge_model is None or not judge_select.independent(answering_model, judge_model):
        return _record("ungraded")

    text_len = len(item.get("prompt", "") or "") + len(item.get("response", "") or "")
    if judge_kind == judge_select.KIND_SUBSCRIPTION:
        # The only judge that spends quota and sends a local answer's prompt off
        # the machine: privacy flag, quota reading and daily caps all fail closed.
        # F97: caps are checked and the call's spend reserved atomically, then released below.
        stop, rid = judge_ledger.reserve_haiku(time.time(), est_tokens=text_len // 4)
        if stop is not None:
            return _record("ungraded", ledger={"outcome": stop})
    else:
        rid = None

    outcome: dict = {}
    try:
        ok = await _evaluate_background(
            item.get("prompt", ""),
            item.get("response", ""),
            item.get("task_type", "query"),
            item.get("routing_decision_id"),
            model=judge_model,
            outcome=outcome,
        )
        status = "graded" if ok else "failed"
        return _record(status, outcome.get("score"), ledger={
            "outcome": status, "tokens": outcome.get("tokens"), "cost_usd": outcome.get("cost_usd"),
        })
    finally:
        if rid is not None:
            judge_ledger.release_reservation(rid, outcome.get("tokens"))


async def drain_queue(batch_size: int = 50, time_budget_s: float = 20.0, *, force: bool = False) -> dict:
    """Drain the judge queue out of band, grading with an independent judge.

    Bounded on two axes so this can safely run detached from session start
    or on a schedule: at most ``batch_size`` items claimed per call, and at
    most ``time_budget_s`` wall-clock seconds spent grading (measured with
    ``time.monotonic()`` — see this repo's CLAUDE.md on why wall-clock timing
    must never use ``time.time()`` for a duration). Anything left over —
    past the batch size, or not reached before the time budget — is appended
    back to the queue for the next drain.

    F101: while the benchmark marker (``daily_offload.bench_marker()``) exists, nothing is claimed and no
    model is called; the result carries ``deferred="benchmark active"``. ``force=True`` overrides.

    Returns:
        dict with counts: graded, ungraded, failed, requeued.
    """
    if not force:
        from llm_router import judge_view
        if judge_view.bench_active():
            return {"graded": 0, "ungraded": 0, "failed": 0, "requeued": 0, "deferred": "benchmark active"}
    path = _queue_path()
    _recover_orphaned_claims(path)
    from llm_router import judge_hygiene
    judge_hygiene.purge_expired(path)
    judge_hygiene.purge_orphans(path)
    items, legacy_dropped = judge_hygiene.sanitize_claimed(_claim_queue(path))
    to_process, overflow = items[:batch_size], items[batch_size:]
    _requeue(path, overflow)

    graded = ungraded = failed = 0
    start = time.monotonic()
    for i, item in enumerate(to_process):
        if time.monotonic() - start > time_budget_s:
            _requeue(path, to_process[i:])
            break
        outcome = await _grade_one(item)
        if outcome == "graded":
            graded += 1
        elif outcome == "ungraded":
            ungraded += 1
        else:
            failed += 1

    result = {
        "graded": graded,
        "ungraded": ungraded,
        "failed": failed,
        "requeued": len(overflow),
    }
    if legacy_dropped:  # F97: pre-P3.1-4 rows that hygiene refused; counted, never graded
        result["dropped"] = legacy_dropped
    return result


async def _evaluate_background(
    prompt: str,
    response: str,
    task_type: str,
    routing_decision_id: int | None = None,
    model: str | None = None,
    outcome: dict | None = None,
) -> bool:
    """Background evaluation task — runs independently without blocking caller.

    Args:
        model: The JUDGE model to call. No default model is named in code
            (D-205): the queue drain (:func:`_grade_one`) passes an independently
            selected one, and None leaves the row ungraded (recorded via failopen).

    Returns:
        True if a score was parsed and stored, False otherwise (including on
        any failure — the caller never learns *which* failure from the return
        value; that detail is in the failopen record).
    """
    if not model:
        from llm_router import failopen
        failopen.record("CHZ-FO-JUDGE-EVAL", ValueError("no judge model supplied"))
        return False
    try:
        judge_prompt = _build_judge_prompt(prompt, response, task_type)

        from llm_router import judge_ledger
        with judge_ledger.no_proxy_env():  # a judge call never loops through the local proxy
            judge_response = await call_llm(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": judge_prompt,
                    }
                ],
                temperature=0.0,  # Deterministic scoring
                max_tokens=50,  # Score is short JSON
            )
        if outcome is not None:
            outcome.update(_spend_of(judge_response))

        # Parse score from response
        score = _parse_judge_score(judge_response.content)
        if outcome is not None:
            outcome["score"] = score

        # Store in database
        if routing_decision_id and score is not None:
            await _store_judge_score(routing_decision_id, score)
            return True
        return False

    except Exception as exc:
        # CHZ-FO-JUDGE-EVAL. This used to be a bare `except Exception: pass` —
        # the judge is genuinely best-effort and must never block the primary
        # task, but a silently-failing judge is exactly how 1,608 routing
        # decisions went ungraded with nothing anywhere to say why. Record it.
        from llm_router import failopen
        failopen.record("CHZ-FO-JUDGE-EVAL", exc)
        return False


def _spend_of(resp) -> dict:
    """tokens and cost_usd of one judge call; None where the response does not say.

    A reported cost of 0 is kept (a measured local call is 0). A missing or
    non-numeric field is unknown (None), never 0.
    """
    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    tin, tout = _num(getattr(resp, "input_tokens", None)), _num(getattr(resp, "output_tokens", None))
    return {
        "tokens": int(tin + tout) if tin is not None and tout is not None else None,
        "cost_usd": _num(getattr(resp, "cost_usd", None)),
    }


def _build_judge_prompt(prompt: str, response: str, task_type: str) -> str:
    """Build prompt for LLM judge evaluation.

    Returns JSON with correctness, relevance, completeness scores and a short
    rationale.

    feat/judge-discrimination (2026-09-25). The original three-field, evenly
    averaged 0–1 prompt scored a wrong arithmetic answer ("17*23" -> "400")
    at 0.667 and the right one ("391") at 1.0 — it discriminated in the right
    DIRECTION but too weakly to be a useful quality signal, because a fluent,
    on-topic, complete-looking WRONG answer still earned full credit on two
    of the three dimensions. This version fixes three things, each measured
    on a 50-item hand-labelled set (tests/fixtures/judge_eval_set.py) before
    and after, on the n=22 holdout half (untouched during tuning):

      Correctness-dominant scoring raised holdout threshold=0.5 accuracy from
      58.8% (10/17, Wilson 95% CI 36-79%) to 100% (18/18, CI 82-100%) with
      judge=qwen3.5:latest, and from 66.7% (12/18, CI 44-84%) to 100%
      (18/18, CI 82-100%) with the production judge qwen3-coder:30b (n=22
      holdout each; AUC was already 1.0 both before and after — the fix is
      absolute separation, not ranking). mean(wrong) fell to 0.40 (qwen3.5)
      and 0.38 (qwen3-coder); mean(correct) held at ~1.00 (1.000 qwen3.5,
      0.970->1.000 qwen3-coder). The 4 holdout partial-credit items are
      reported separately and excluded from accuracy/AUC. With qwen3.5,
      every wrong item scored EXACTLY 0.40 (correctness 0, full marks on the
      two secondary dimensions) — for that judge the gain is effectively a
      binary correctness gate, not a graded one. qwen3-coder:30b varies on
      completeness (wrong-item scores of 0.4 and 0.325 both occur), so the
      same weighting produces a genuinely graded signal there.

      1. VERIFY-FIRST instruction — tells the judge to work out the right
         answer itself before scoring, instead of pattern-matching on
         fluency. Kept as a best-practice instruction (a judge that
         doesn't verify is a worse bet on models/tasks this set doesn't
         cover), but the red-check run (scripts/judge_discrimination_eval.py
         --red-check-no-verify-first, holdout split, qwen3.5:latest judge)
         found NO measurable drop from removing just this line — mean(wrong)
         stayed at 0.4 and threshold accuracy stayed at 100% (18/18).
         Reported honestly rather than claimed as evidence this line alone
         helps; qwen3.5 is a hybrid-reasoning model that appears to "think"
         through arithmetic/fact checks in its own chain-of-thought
         regardless of whether the prompt asks it to, which likely explains
         why an explicit instruction added nothing further for THIS judge
         model on THIS set. It may still matter for a non-reasoning judge
         model — untested here.
      2. Explicit, per-dimension correctness criteria that name the failure
         mode directly: "a confident, well-written WRONG answer still scores
         0 on correctness". This is the exact case the old prompt missed.
      3. A coarse {0, 0.5, 1} scale instead of an unconstrained 0.X float —
         less room for arbitrary, non-reproducible granularity on a
         dimension that is really "wrong / partially right / right".

    Items 2 and 3, together with the correctness-dominant composite weights
    below, are what the red-check isolates as actually carrying the
    separation gain for this judge model (see the PR description's
    before/after table).

    The composite is no longer an even average (see `_parse_judge_score`):
    correctness is weighted far more heavily than relevance/completeness,
    because a relevant, complete, WRONG answer is still a bad answer for
    the router's quality signal — averaging it with two high, easy-to-earn
    scores was exactly what diluted the old judge's separation.
    """
    return f"""You are a strict, independent verifier grading another model's answer. Your \
job is to catch a wrong answer that a careless reviewer would miss — a fluent, \
on-topic, well-formatted response that gets the actual answer wrong is a BAD response.

STEP 1 — VERIFY FIRST, SILENTLY. Before scoring, work out the correct answer or the \
correct facts yourself (recompute arithmetic, recall the fact, re-derive the code's \
behaviour). Do this internally — do not show your work in the output.

STEP 2 — SCORE each dimension using ONLY 0, 0.5, or 1:

  correctness — 1 if the core claim/answer/result is factually and computationally \
right. 0.5 if it is genuinely partially right (e.g. right on one sub-part of a \
multi-part question, or right approach with a wrong final number). 0 if the final \
answer is wrong. A confident, well-written WRONG answer still scores 0 on \
correctness — fluency and completeness do not earn correctness credit.
  relevance — 1 if it addresses what was actually asked, 0.5 if only partially \
on-topic, 0 if it answers a different question.
  completeness — 1 if nothing material requested is missing, 0.5 if it is a \
reasonable but incomplete attempt, 0 if it is a non-answer.

USER PROMPT:
{prompt}

RESPONSE TO GRADE:
{response}

TASK TYPE: {task_type}

Respond ONLY with valid JSON (no markdown, no text outside the JSON):
{{"correctness": 0|0.5|1, "relevance": 0|0.5|1, "completeness": 0|0.5|1, "rationale": "<one short sentence citing the specific error, or confirming correctness>"}}"""


#: Composite weights. Correctness dominates deliberately — see
#: `_build_judge_prompt`'s docstring for the measured reason. Must sum to 1.0
#: (asserted by test_judge.py) so a response scored 1/1/1 on every dimension
#: still composes to exactly 1.0.
_CORRECTNESS_WEIGHT = 0.6
_RELEVANCE_WEIGHT = 0.25
_COMPLETENESS_WEIGHT = 0.15


def _coerce_dimension(value, default: float) -> float:
    """Best-effort float coercion for a non-load-bearing dimension.

    Used for relevance/completeness only — a bad or missing value there
    degrades gracefully to `default` rather than voiding the whole grade,
    because correctness (see `_parse_judge_score`) is the dimension that
    actually has to be trustworthy or absent.
    """
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _parse_judge_score(response_text: str) -> float | None:
    """Parse a weighted composite score from the judge's response.

    Expects JSON with correctness, relevance, completeness (0/0.5/1 each,
    though any float in range is accepted) and a rationale string.

    `correctness` is load-bearing: a reply that omits it, or whose value
    can't be read as a number, is treated the same as a reply that isn't
    valid JSON at all — this function returns None and the caller
    (`_evaluate_background`) leaves the row ungraded rather than writing a
    score built on a dimension the judge never actually answered. This is a
    behaviour change from the original judge, which silently defaulted
    every missing field to 0.5 — a default correctness score is exactly the
    kind of "plausible-sounding score from a broken read" this repo's
    failopen convention says must be visible, not silent.

    `relevance`/`completeness` are not load-bearing: a missing or malformed
    value there degrades to a neutral 0.5 rather than voiding the grade,
    since they are secondary to correctness in the composite (see
    `_CORRECTNESS_WEIGHT`).

    Returns:
        Weighted composite in [0, 1], or None if parsing failed or
        correctness itself couldn't be read.
    """
    import json

    try:
        # Extract JSON from response (may contain extra text around it).
        response_text = response_text.strip()
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        json_str = response_text[start:end]
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

    if not isinstance(data, dict) or "correctness" not in data:
        return None
    try:
        correctness = max(0.0, min(1.0, float(data["correctness"])))
    except (TypeError, ValueError):
        return None

    relevance = _coerce_dimension(data.get("relevance"), default=0.5)
    completeness = _coerce_dimension(data.get("completeness"), default=0.5)

    composite = (
        _CORRECTNESS_WEIGHT * correctness
        + _RELEVANCE_WEIGHT * relevance
        + _COMPLETENESS_WEIGHT * completeness
    )
    return max(0.0, min(1.0, composite))


async def _store_judge_score(routing_decision_id: int, score: float) -> None:
    """Store judge score in routing_decisions table."""
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE routing_decisions SET judge_score = ? WHERE id = ?",
            (score, routing_decision_id),
        )
        await db.commit()
    except Exception:
        pass
    finally:
        await db.close()


async def get_judge_scores_for_model(
    model: str,
    days: int = 30,
) -> dict:
    """Get average judge scores for a model over the past N days.

    Args:
        model: Model name (e.g., 'gpt-4o', 'claude-opus-4-6')
        days: Number of days to aggregate (default 30)

    Returns:
        dict with avg_score, sample_count, min_score, max_score; the three scores
        are None (unknown, never 0.0) when there is no judged row in the window.
    """
    db = await _get_db()
    try:
        cutoff = datetime.now() - timedelta(days=days)
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) as sample_count,
                AVG(judge_score) as avg_score,
                MIN(judge_score) as min_score,
                MAX(judge_score) as max_score
            FROM routing_decisions
            WHERE final_model = ? AND judge_score IS NOT NULL AND timestamp >= ?
            """,
            (model, cutoff),
        )
        row = await cursor.fetchone()

        if not row or row[1] is None:
            return {
                "model": model,
                "avg_score": None,
                "sample_count": 0,
                "min_score": None,
                "max_score": None,
                "days": days,
            }

        return {
            "model": model,
            "avg_score": float(row[1]),
            "sample_count": int(row[0]),
            "min_score": float(row[2]) if row[2] is not None else None,
            "max_score": float(row[3]) if row[3] is not None else None,
            "days": days,
        }
    finally:
        await db.close()


async def reorder_by_quality(models: list[str], days: int = 7) -> list[str]:
    """Reorder a model chain by average judge scores, deprioritizing low-quality models.

    Models with average judge score < 0.7 over the past N days are moved to the
    end of the chain. Models with insufficient history (< 3 samples) are unaffected.

    This allows the router to automatically learn from quality feedback and avoid
    repeatedly routing to models that produce poor outputs.

    Args:
        models: Ordered list of model identifiers (provider/model format).
        days: Number of days of history to consider (default 7).

    Returns:
        Reordered model list with low-quality models deprioritized.
        Returns original list unchanged if database is unavailable or has no judge data.
    """
    if not models:
        return models

    try:
        # Get quality scores for each model
        model_quality: dict[str, float | None] = {}
        model_samples: dict[str, int] = {}

        for model in models:
            try:
                scores = await get_judge_scores_for_model(model, days=days)
                model_quality[model] = scores.get("avg_score")
                model_samples[model] = scores.get("sample_count", 0)
            except Exception:
                # If a single model fails, just skip its quality data
                model_quality[model] = None
                model_samples[model] = 0

        # Partition: high quality (≥0.7 or insufficient data) vs low quality (<0.7 with ≥3 samples)
        high_quality = []
        low_quality = []

        for model in models:
            samples = model_samples.get(model, 0)
            quality = model_quality.get(model)

            # Keep in original position if: no samples, unknown quality, or acceptable
            if samples < 3 or quality is None or quality >= 0.7:
                high_quality.append(model)
            else:
                low_quality.append(model)

        # Return reordered: high quality first, then low quality (no removal, just demotion)
        if low_quality:
            from llm_router.logging import get_logger
            log = get_logger("llm_router.judge")
            log.info(
                "Quality-based reordering: demoted %d model(s) due to low avg scores ≥%dd",
                len(low_quality), days
            )
            return high_quality + low_quality

        return models
    except Exception:
        # If anything goes wrong, return original chain unchanged
        return models
