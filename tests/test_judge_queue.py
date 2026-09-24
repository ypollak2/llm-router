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
