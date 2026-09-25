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
    sample_rate = float(os.environ.get("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.1"))
    if random.random() > sample_rate:
        return False

    entry = {
        "routing_decision_id": routing_decision_id,
        "prompt": prompt,
        "response": response,
        "task_type": task_type,
        "answering_model": answering_model,
        "queued_at": time.time(),
    }
    try:
        path = _queue_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _enforce_queue_cap(path)
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


def _enforce_queue_cap(path) -> int:
    """Keep at most `_queue_max_entries()` newest lines in the queue file.

    Drops the OLDEST entries first — a queue nobody has drained in a while
    should still prefer grading recent traffic once something finally does
    drain it. Runs on every enqueue (not just at drain time) so growth stays
    bounded even if nothing ever drains the queue.

    Never raises — a failure here must not break the enqueue that triggered
    it. Returns the number of entries dropped (0 if already within cap).
    """
    cap = _queue_max_entries()
    if cap <= 0:
        return 0
    dropped = 0
    try:
        with open(path) as f:
            lines = [line for line in f.readlines() if line.strip()]
        if len(lines) <= cap:
            return 0
        dropped = len(lines) - cap
        kept = lines[-cap:]
        tmp_path = f"{path}.capping.{os.getpid()}.{time.monotonic_ns()}"
        with open(tmp_path, "w") as f:
            f.writelines(kept)
        os.replace(tmp_path, str(path))
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
    """Pick an INDEPENDENT judge model — one that never matches the model
    that answered.

    Grading a response with the same model that produced it is not
    independent verification; it is the model checking its own homework, and
    a plausible-sounding score from that is worse than no score at all.

    Prefers what's available with zero API keys: a locally installed Ollama
    model (via :mod:`llm_router.discover`, the same module the router uses to
    find what's actually reachable) that differs from the answering model.

    Returns:
        A judge model id, or None if no independent judge is available —
        callers must leave the row ungraded in that case, never fabricate a
        score and never write 0.
    """
    try:
        from llm_router.discover import get_cached_ollama_models, is_ollama_available
    except Exception:
        return None

    try:
        if not is_ollama_available():
            return None
        candidates = get_cached_ollama_models()
    except Exception:
        return None

    def _norm(m: str) -> str:
        return m.split("/", 1)[-1] if "/" in m else m

    answering_norm = _norm(answering_model or "")
    for candidate in candidates:
        if _norm(candidate) != answering_norm:
            return candidate
    return None


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
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
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
    # Independence guard: even if _select_judge_model's own filtering were
    # ever bypassed or misconfigured, grading must refuse outright rather
    # than let a model grade its own answer.
    if judge_model is None or judge_model == answering_model:
        return "ungraded"

    ok = await _evaluate_background(
        item.get("prompt", ""),
        item.get("response", ""),
        item.get("task_type", "query"),
        item.get("routing_decision_id"),
        model=judge_model,
    )
    return "graded" if ok else "failed"


async def drain_queue(batch_size: int = 50, time_budget_s: float = 20.0) -> dict:
    """Drain the judge queue out of band, grading with an independent judge.

    Bounded on two axes so this can safely run detached from session start
    or on a schedule: at most ``batch_size`` items claimed per call, and at
    most ``time_budget_s`` wall-clock seconds spent grading (measured with
    ``time.monotonic()`` — see this repo's CLAUDE.md on why wall-clock timing
    must never use ``time.time()`` for a duration). Anything left over —
    past the batch size, or not reached before the time budget — is appended
    back to the queue for the next drain.

    Returns:
        dict with counts: graded, ungraded, failed, requeued.
    """
    path = _queue_path()
    _recover_orphaned_claims(path)
    items = _claim_queue(path)
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

    return {
        "graded": graded,
        "ungraded": ungraded,
        "failed": failed,
        "requeued": len(overflow),
    }


async def _evaluate_background(
    prompt: str,
    response: str,
    task_type: str,
    routing_decision_id: int | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> bool:
    """Background evaluation task — runs independently without blocking caller.

    Args:
        model: The JUDGE model to call. Defaults to the historical hardcoded
            Haiku model for backward compatibility with direct callers; the
            queue drain (:func:`_grade_one`) always passes an independently
            selected model instead.

    Returns:
        True if a score was parsed and stored, False otherwise (including on
        any failure — the caller never learns *which* failure from the return
        value; that detail is in the failopen record).
    """
    try:
        judge_prompt = _build_judge_prompt(prompt, response, task_type)

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

        # Parse score from response
        score = _parse_judge_score(judge_response.content)

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


def _build_judge_prompt(prompt: str, response: str, task_type: str) -> str:
    """Build prompt for LLM judge evaluation.

    Returns JSON with relevance, completeness, correctness scores (0–1).
    """
    return f"""You are an expert quality evaluator. Rate this response on three dimensions:

USER PROMPT:
{prompt}

RESPONSE:
{response}

TASK TYPE: {task_type}

Evaluate on:
1. Relevance (0–1): Does response address the prompt?
2. Completeness (0–1): Is response sufficiently thorough?
3. Correctness (0–1): Is factual content accurate?

Respond ONLY with valid JSON (no markdown, no explanation):
{{"relevance": 0.X, "completeness": 0.X, "correctness": 0.X}}"""


def _parse_judge_score(response_text: str) -> float | None:
    """Parse composite score from judge response.

    Expects JSON with relevance, completeness, correctness (0–1 each).
    Returns average of three scores, or None if parsing fails.
    """
    import json

    try:
        # Extract JSON from response (may contain extra text)
        response_text = response_text.strip()
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start == -1 or end == 0:
            return None

        json_str = response_text[start:end]
        data = json.loads(json_str)

        # Average the three scores
        relevance = float(data.get("relevance", 0.5))
        completeness = float(data.get("completeness", 0.5))
        correctness = float(data.get("correctness", 0.5))

        composite = (relevance + completeness + correctness) / 3.0
        # Clamp to [0, 1]
        return max(0.0, min(1.0, composite))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


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
        dict with avg_score, sample_count, min_score, max_score
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
                "avg_score": 0.0,
                "sample_count": 0,
                "min_score": 0.0,
                "max_score": 0.0,
                "days": days,
            }

        return {
            "model": model,
            "avg_score": float(row[1]),
            "sample_count": int(row[0]),
            "min_score": float(row[2]) if row[2] is not None else 0.0,
            "max_score": float(row[3]) if row[3] is not None else 0.0,
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
        model_quality: dict[str, float] = {}
        model_samples: dict[str, int] = {}

        for model in models:
            try:
                scores = await get_judge_scores_for_model(model, days=days)
                model_quality[model] = scores.get("avg_score", 0.0)
                model_samples[model] = scores.get("sample_count", 0)
            except Exception:
                # If a single model fails, just skip its quality data
                model_quality[model] = 0.0
                model_samples[model] = 0

        # Partition: high quality (≥0.7 or insufficient data) vs low quality (<0.7 with ≥3 samples)
        high_quality = []
        low_quality = []

        for model in models:
            samples = model_samples.get(model, 0)
            quality = model_quality.get(model, 0.0)

            # Keep in original position if: no samples, or quality is acceptable
            if samples < 3 or quality >= 0.7:
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
