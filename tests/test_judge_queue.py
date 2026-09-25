"""Tests for the judge grading queue — CHZ-JUDGE-QUEUE.

Background: `sqlite3 ~/.llm-router/usage.db "select count(*), sum(judge_score
is not null) from routing_decisions"` measured **0 of 1,608** graded. Three
verified causes, all on the path that used to call
`judge.evaluate_response_async` synchronously from the routing hot path:

  a. Some callers (the DIRECT/hook path in `savings_logger.py`) never passed
     `response=` to `cost.log_routing_decision`, so the judge trigger's
     `if success and response:` gate never fired for them.
  b. The judge hardcoded a PAID model with no key configured; `call_llm`
     raised, and a bare `except Exception: pass` swallowed it silently.
  c. `evaluate_response_async` only *creates* an asyncio task, which a caller
     running under `asyncio.run(...)` (the hooks) cancels on return before it
     can execute.

Fix (owner decision): queue-and-grade-later. The hot path
(`judge.enqueue_for_grading`) makes no network call and creates no asyncio
task. Grading happens out of band (`judge.drain_queue`) with an INDEPENDENT
judge model — never the model that answered — and leaves a row ungraded
(never scored 0) when no independent judge is available.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import cost
from llm_router.types import LLMResponse


@pytest.fixture
def judge_home(tmp_path, monkeypatch):
    """Isolate the judge queue file to a per-test temp directory.

    The suite sets one shared LLM_ROUTER_HOME for the whole pytest session
    (see tests/conftest.py), which would otherwise let queue entries from one
    test leak into another's assertions.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


def _queue_lines(judge_home) -> list[dict]:
    # LLM_ROUTER_HOME points directly at the state dir (paths.llm_router_home
    # returns it unmodified when the override is set) — no extra ".llm-router"
    # segment, unlike the default Path.home()/".llm-router" fallback.
    path = judge_home / "judge_queue.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _insert_ungraded_decision(model: str) -> int:
    """Insert a bare routing_decisions row and return its id."""
    db = await cost._get_db()
    try:
        await db.execute(
            """INSERT INTO routing_decisions
               (timestamp, task_type, profile, complexity, final_model, final_provider,
                success, input_tokens, output_tokens, cost_usd, latency_ms, judge_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                "query", "balanced", "simple",
                model, "ollama",
                1, 10, 10, 0.0, 100.0, None,
            ),
        )
        await db.commit()
        cursor = await db.execute("SELECT id FROM routing_decisions ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()
        assert row
        return row[0]
    finally:
        await db.close()


async def _judge_score(decision_id: int):
    db = await cost._get_db()
    try:
        cursor = await db.execute(
            "SELECT judge_score FROM routing_decisions WHERE id = ?", (decision_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


# ── enqueue_for_grading: hot-path append, no network, no async task ────────


def test_enqueue_for_grading_writes_entry_with_response(judge_home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router.judge import enqueue_for_grading

    wrote = enqueue_for_grading(
        routing_decision_id=42,
        prompt="What is 2+2?",
        response="4",
        task_type="query",
        answering_model="ollama/qwen3.5:latest",
    )
    assert wrote is True

    entries = _queue_lines(judge_home)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["routing_decision_id"] == 42
    assert entry["prompt"] == "What is 2+2?"
    assert entry["response"] == "4"
    assert entry["task_type"] == "query"
    assert entry["answering_model"] == "ollama/qwen3.5:latest"


def test_enqueue_for_grading_respects_sample_rate(judge_home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.0")
    from llm_router.judge import enqueue_for_grading

    wrote = enqueue_for_grading(
        routing_decision_id=1, prompt="p", response="r",
        task_type="query", answering_model="m",
    )
    assert wrote is False
    assert _queue_lines(judge_home) == []


def test_enqueue_for_grading_never_calls_the_judge_model(judge_home, monkeypatch):
    """The hot path must make no network call — that is causes (b) and (c)."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router import judge

    with patch("llm_router.judge.call_llm") as mock_call:
        judge.enqueue_for_grading(
            routing_decision_id=1, prompt="p", response="r",
            task_type="query", answering_model="m",
        )
    mock_call.assert_not_called()


def test_enqueue_for_grading_skips_without_routing_decision_id(judge_home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router.judge import enqueue_for_grading

    wrote = enqueue_for_grading(
        routing_decision_id=None, prompt="p", response="r",
        task_type="query", answering_model="m",
    )
    assert wrote is False
    assert _queue_lines(judge_home) == []


# ── _select_judge_model: independence guard ─────────────────────────────────


def test_select_judge_model_prefers_a_different_local_model():
    from llm_router.judge import _select_judge_model

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/answering-model", "ollama/judge-model"],
         ):
        picked = _select_judge_model("ollama/answering-model")

    assert picked == "ollama/judge-model"


def test_select_judge_model_returns_none_when_only_the_answering_model_exists():
    """No independent judge available -> None, never the same model."""
    from llm_router.judge import _select_judge_model

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/only-model"],
         ):
        picked = _select_judge_model("ollama/only-model")

    assert picked is None


def test_select_judge_model_returns_none_when_ollama_unavailable():
    from llm_router.judge import _select_judge_model

    with patch("llm_router.discover.is_ollama_available", return_value=False):
        picked = _select_judge_model("ollama/answering-model")

    assert picked is None


def test_select_judge_model_prefers_a_stronger_judge_when_installed():
    """feat/judge-discrimination: among several independent candidates, prefer
    the ones in `_JUDGE_MODEL_PREFERENCE` order rather than whatever happens
    to come first from discovery — measured to separate correct/wrong answers
    better on tests/fixtures/judge_eval_set.py."""
    from llm_router.judge import _select_judge_model

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             # Discovery order deliberately puts the LEAST-preferred model
             # first, so this only passes if preference order is honoured.
             return_value=["ollama/qwen3.5:latest", "ollama/qwen3-coder:30b"],
         ):
        picked = _select_judge_model("ollama/some-other-answering-model")

    assert picked == "ollama/qwen3-coder:30b"


def test_select_judge_model_falls_back_when_no_preferred_name_installed():
    """No installed candidate matches the preference list -> falls back to
    the original "first independent candidate" behaviour, never returns
    None just because none of the preferred names are present."""
    from llm_router.judge import _select_judge_model

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/some-random-model", "ollama/another-random-model"],
         ):
        picked = _select_judge_model("ollama/answering-model")

    assert picked == "ollama/some-random-model"


# ── drain_queue: the grader ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_queue_grades_with_an_independent_judge(temp_db, judge_home, monkeypatch):
    """A queued item with an independent judge available must be graded,
    and the score must be written via the existing store code
    (routing_decisions.judge_score)."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router import judge

    decision_id = await _insert_ungraded_decision("ollama/answering-model")
    judge.enqueue_for_grading(
        routing_decision_id=decision_id,
        prompt="What is 2+2?",
        response="4",
        task_type="query",
        answering_model="ollama/answering-model",
    )

    judge_json = '{"relevance": 0.9, "completeness": 0.9, "correctness": 0.9}'
    fake_judge_response = LLMResponse(
        content=judge_json, model="ollama/judge-model",
        input_tokens=20, output_tokens=8, cost_usd=0.0,
        latency_ms=40.0, provider="ollama",
    )

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/judge-model", "ollama/answering-model"],
         ), \
         patch("llm_router.judge.call_llm", AsyncMock(return_value=fake_judge_response)) as mock_call:
        result = await judge.drain_queue()

    assert result == {"graded": 1, "ungraded": 0, "failed": 0, "requeued": 0}
    # The judge must have been called with the INDEPENDENT model, not the one
    # that answered.
    assert mock_call.call_args.kwargs["model"] == "ollama/judge-model"

    score = await _judge_score(decision_id)
    assert score is not None
    assert abs(score - 0.9) < 0.01


@pytest.mark.asyncio
async def test_drain_queue_refuses_when_judge_model_equals_answering_model(
    temp_db, judge_home, monkeypatch
):
    """Only the answering model is 'available' -> no independent judge exists
    -> the row must stay ungraded, never scored 0."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router import judge

    decision_id = await _insert_ungraded_decision("ollama/only-model")
    judge.enqueue_for_grading(
        routing_decision_id=decision_id,
        prompt="What is 2+2?",
        response="4",
        task_type="query",
        answering_model="ollama/only-model",
    )

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/only-model"],
         ), \
         patch("llm_router.judge.call_llm") as mock_call:
        result = await judge.drain_queue()

    mock_call.assert_not_called()
    assert result == {"graded": 0, "ungraded": 1, "failed": 0, "requeued": 0}

    score = await _judge_score(decision_id)
    assert score is None, (
        f"row must stay ungraded (NULL) when no independent judge exists, "
        f"got {score!r} — a written 0 is indistinguishable from a real bad score"
    )


@pytest.mark.asyncio
async def test_drain_queue_records_judge_failure_in_failopen(temp_db, judge_home, monkeypatch):
    """A judge call that raises must be recorded via failopen, not swallowed."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router import judge

    decision_id = await _insert_ungraded_decision("ollama/answering-model")
    judge.enqueue_for_grading(
        routing_decision_id=decision_id,
        prompt="What is 2+2?",
        response="4",
        task_type="query",
        answering_model="ollama/answering-model",
    )

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/judge-model", "ollama/answering-model"],
         ), \
         patch("llm_router.judge.call_llm", AsyncMock(side_effect=RuntimeError("no key configured"))), \
         patch("llm_router.failopen.record") as mock_record:
        result = await judge.drain_queue()

    assert result == {"graded": 0, "ungraded": 0, "failed": 1, "requeued": 0}
    codes = [c.args[0] for c in mock_record.call_args_list]
    assert "CHZ-FO-JUDGE-EVAL" in codes, (
        f"a failing judge call must be recorded via failopen.record, got calls: "
        f"{mock_record.call_args_list!r}"
    )

    score = await _judge_score(decision_id)
    assert score is None


@pytest.mark.asyncio
async def test_drain_queue_bounds_batch_size_and_requeues_overflow(
    temp_db, judge_home, monkeypatch
):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    from llm_router import judge

    ids = [await _insert_ungraded_decision("ollama/answering-model") for _ in range(3)]
    for decision_id in ids:
        judge.enqueue_for_grading(
            routing_decision_id=decision_id,
            prompt="p", response="r", task_type="query",
            answering_model="ollama/answering-model",
        )

    judge_json = '{"relevance": 0.8, "completeness": 0.8, "correctness": 0.8}'
    fake = LLMResponse(
        content=judge_json, model="ollama/judge-model",
        input_tokens=5, output_tokens=5, cost_usd=0.0,
        latency_ms=10.0, provider="ollama",
    )

    with patch("llm_router.discover.is_ollama_available", return_value=True), \
         patch(
             "llm_router.discover.get_cached_ollama_models",
             return_value=["ollama/judge-model", "ollama/answering-model"],
         ), \
         patch("llm_router.judge.call_llm", AsyncMock(return_value=fake)):
        result = await judge.drain_queue(batch_size=2)

    assert result["graded"] == 2
    assert result["requeued"] == 1
    # The overflow item must have been appended back to the queue file for
    # the next drain to pick up.
    remaining = _queue_lines(judge_home)
    assert len(remaining) == 1


# ── Concurrency: two claims must never overwrite each other's rows ─────────


def test_claim_queue_work_files_are_unique_per_call(judge_home, monkeypatch):
    """CHZ-JUDGE-QUEUE-CONCURRENCY unit test. `_claim_queue` renames the
    queue file to a work-file name before reading it; that name must be
    unique per call, or a second concurrent claim's `os.replace` silently
    overwrites the first claim's still-unread file (os.replace gives no
    warning when it clobbers an existing destination)."""
    from llm_router.judge import _work_path

    queue_path = judge_home / "judge_queue.jsonl"
    first = _work_path(queue_path)
    second = _work_path(queue_path)
    assert first != second, (
        f"two calls to _work_path produced the SAME name ({first!r}) — a "
        "second concurrent claim would overwrite the first's in-flight file"
    )


@pytest.mark.asyncio
async def test_two_overlapping_claims_lose_no_rows(judge_home, monkeypatch):
    """Reproduces the actual interleaving: claim 1 renames the queue aside
    (leaving it "in flight" — not yet read+deleted), and BEFORE it gets to
    read its own work file, claim 2 runs start-to-finish against freshly
    appended items. With a fixed work-file name, claim 2's `os.replace`
    would land on the exact same path as claim 1's un-read file and destroy
    it. With a unique name per call, claim 1's rows must survive untouched.
    """
    from llm_router import judge

    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    queue_path = judge_home / "judge_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text('{"id": "A"}\n')

    real_replace = os.replace
    interleaved: dict = {}

    def replace_then_interleave(src, dst):
        real_replace(src, dst)
        if "ran" not in interleaved:
            interleaved["ran"] = True
            # A second, independent claim starts and finishes entirely
            # while claim 1 is still holding its own (unique) work file,
            # unread.
            queue_path.write_text('{"id": "B"}\n')
            interleaved["items"] = judge._claim_queue(queue_path)

    monkeypatch.setattr(judge.os, "replace", replace_then_interleave)

    items_first = judge._claim_queue(queue_path)

    assert interleaved.get("items") == [{"id": "B"}]
    assert items_first == [{"id": "A"}], (
        f"claim 1's rows were lost to the interleaved claim 2 — got "
        f"{items_first!r} (expected [{{'id': 'A'}}])"
    )


def test_recover_orphaned_claims_requeues_old_draining_files(judge_home):
    """A `.draining.*` file left behind by a crashed drain (old enough that
    it can't plausibly still be in flight) must be requeued, not dropped."""
    from llm_router.judge import _recover_orphaned_claims, _work_path

    queue_path = judge_home / "judge_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    orphan_path = _work_path(queue_path)
    with open(orphan_path, "w") as f:
        f.write('{"id": "stranded"}\n')
    # Back-date it well past the recovery age threshold so it reads as a
    # genuinely crashed drain, not one still in flight.
    old = os.path.getmtime(orphan_path) - 999
    os.utime(orphan_path, (old, old))

    recovered = _recover_orphaned_claims(queue_path)

    assert recovered == 1
    assert not os.path.exists(orphan_path), "the orphan file must be cleaned up after recovery"
    entries = _queue_lines(judge_home)
    assert entries == [{"id": "stranded"}]


def test_recover_orphaned_claims_leaves_fresh_files_alone(judge_home):
    """A `.draining.*` file that is still young must NOT be swept up — it
    plausibly belongs to a drain that is still actively reading it right
    now, and recovering it would grade the same rows twice."""
    from llm_router.judge import _recover_orphaned_claims, _work_path

    queue_path = judge_home / "judge_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    fresh_path = _work_path(queue_path)
    with open(fresh_path, "w") as f:
        f.write('{"id": "still-in-flight"}\n')

    recovered = _recover_orphaned_claims(queue_path)

    assert recovered == 0
    assert os.path.exists(fresh_path), "a fresh (not stale) claim file must be left alone"
    assert _queue_lines(judge_home) == []


# ── Bounded queue size ───────────────────────────────────────────────────


def test_enqueue_for_grading_caps_queue_size_and_drops_oldest(judge_home, monkeypatch):
    """A queue nobody drains must still stay bounded — the cap is enforced
    at enqueue time, not only at drain time."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES", "3")
    from llm_router.judge import enqueue_for_grading

    for i in range(5):
        enqueue_for_grading(
            routing_decision_id=i, prompt=f"p{i}", response=f"r{i}",
            task_type="query", answering_model="m",
        )

    entries = _queue_lines(judge_home)
    assert len(entries) == 3, f"queue must be capped at 3 entries, got {len(entries)}"
    # Oldest dropped first; newest kept.
    assert [e["routing_decision_id"] for e in entries] == [2, 3, 4]


def test_enqueue_for_grading_records_dropped_count_via_failopen(judge_home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES", "2")
    from llm_router.judge import enqueue_for_grading

    with patch("llm_router.failopen.record") as mock_record:
        for i in range(4):
            enqueue_for_grading(
                routing_decision_id=i, prompt="p", response="r",
                task_type="query", answering_model="m",
            )

    drop_calls = [
        c for c in mock_record.call_args_list
        if c.args[0] == "CHZ-FO-JUDGE-QUEUE-CAP-DROPPED"
    ]
    assert drop_calls, (
        "expected at least one CHZ-FO-JUDGE-QUEUE-CAP-DROPPED failopen "
        f"record so the drop is visible, got calls: {mock_record.call_args_list!r}"
    )


def test_enqueue_for_grading_no_cap_drop_when_under_limit(judge_home, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("LLM_ROUTER_JUDGE_QUEUE_MAX_ENTRIES", "10")
    from llm_router.judge import enqueue_for_grading

    for i in range(3):
        enqueue_for_grading(
            routing_decision_id=i, prompt="p", response="r",
            task_type="query", answering_model="m",
        )

    assert len(_queue_lines(judge_home)) == 3


# ── T-14: neither cleanup failure may go silent ─────────────────────────────


def test_claim_queue_cleanup_failure_is_recorded_via_failopen(judge_home, monkeypatch):
    """`_claim_queue`'s `os.remove(work_path)` used to be a bare
    `except OSError: pass` — T-14 census flagged it as a new silent
    persistence site. It must now record via failopen when cleanup fails."""
    from llm_router import judge

    queue_path = judge_home / "judge_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text('{"id": "A"}\n')

    with patch("llm_router.judge.os.remove", side_effect=OSError("simulated cleanup failure")), \
         patch("llm_router.failopen.record") as mock_record:
        items = judge._claim_queue(queue_path)

    assert items == [{"id": "A"}], "cleanup failing must not lose the already-read rows"
    codes = [c.args[0] for c in mock_record.call_args_list]
    assert "CHZ-FO-JUDGE-QUEUE-CLAIM-CLEANUP" in codes, (
        f"expected CHZ-FO-JUDGE-QUEUE-CLAIM-CLEANUP to be recorded, got: {codes!r}"
    )


def test_recover_orphaned_claims_unlink_failure_is_recorded_via_failopen(judge_home):
    """`_recover_orphaned_claims`'s `entry.unlink()` used to be a bare
    `except OSError: pass` — T-14 census flagged it as a new silent
    persistence site. It must now record via failopen when cleanup fails."""
    from llm_router.judge import _recover_orphaned_claims, _work_path

    queue_path = judge_home / "judge_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    orphan_path = _work_path(queue_path)
    with open(orphan_path, "w") as f:
        f.write('{"id": "stranded"}\n')
    old = os.path.getmtime(orphan_path) - 999
    os.utime(orphan_path, (old, old))

    with patch("pathlib.Path.unlink", side_effect=OSError("simulated cleanup failure")), \
         patch("llm_router.failopen.record") as mock_record:
        recovered = _recover_orphaned_claims(queue_path)

    assert recovered == 1, "cleanup failing must not lose the already-requeued row"
    codes = [c.args[0] for c in mock_record.call_args_list]
    assert "CHZ-FO-JUDGE-QUEUE-ORPHAN-CLEANUP" in codes, (
        f"expected CHZ-FO-JUDGE-QUEUE-ORPHAN-CLEANUP to be recorded, got: {codes!r}"
    )
    os.remove(orphan_path)
