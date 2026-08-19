"""LoopHole verifier verdicts → the quality store (ground-truth routing signal).

_MIN_CALLS_FOR_SIGNAL is 3 and QUALITY_THRESHOLD is 0.4, so a model with three
recorded failures (score 0.0) drops below threshold and should_skip_model fires.
"""

import json
import os

import pytest

from llm_router.quality_feedback import (
    get_model_quality,
    ingest_loophole_jsonl,
    record_loophole_verdict,
    reset_quality_store,
    should_skip_model,
    _normalize_loophole_model,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_quality_store()
    yield
    reset_quality_store()


def test_normalize_model_and_alias_rejection():
    assert _normalize_loophole_model("ollama:qwen3-coder:30b") == "ollama/qwen3-coder:30b"
    assert _normalize_loophole_model("anthropic:claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    assert _normalize_loophole_model("llm_router:complex") is None    # router alias, not concrete
    assert _normalize_loophole_model("unknown") is None
    assert _normalize_loophole_model("") is None


def test_verified_done_records_top_quality():
    rec = {"executor_model": "ollama:qwen3-coder:30b", "status": "done",
           "verified_done": True, "planner_model": "llm_router:complex"}
    for _ in range(3):
        assert record_loophole_verdict(rec) is True
    # complexity inferred from the llm_router:complex planner label
    assert get_model_quality("ollama/qwen3-coder:30b", "code", "complex") == 1.0
    assert should_skip_model("ollama/qwen3-coder:30b", "code", "complex") is False


def test_failed_runs_make_a_model_skippable():
    rec = {"executor_model": "ollama:tinyllama:1b", "status": "failed",
           "verified_done": False}
    for _ in range(3):
        record_loophole_verdict(rec)
    # three 0.0 scores -> below QUALITY_THRESHOLD (0.4) -> skip
    assert get_model_quality("ollama/tinyllama:1b", "code", "moderate") == 0.0
    assert should_skip_model("ollama/tinyllama:1b", "code", "moderate") is True


def test_router_alias_records_nothing():
    rec = {"executor_model": "llm_router:complex", "status": "done", "verified_done": True}
    assert record_loophole_verdict(rec) is False
    assert record_loophole_verdict({}) is False
    assert record_loophole_verdict("garbage") is False


def test_ingest_jsonl_incremental(tmp_path):
    path = os.path.join(str(tmp_path), "quality_feedback.jsonl")
    recs = [
        {"executor_model": "ollama:qwen3-coder:30b", "status": "done", "verified_done": True},
        {"executor_model": "llm_router:moderate", "status": "done", "verified_done": True},  # alias
        {"executor_model": "ollama:qwen3-coder:30b", "status": "paused", "verified_done": False},
    ]
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    applied, offset = ingest_loophole_jsonl(path)
    assert applied == 2                          # the alias record contributes nothing
    assert offset > 0
    # a second drain from the saved offset applies nothing new
    with open(path, "a") as f:
        f.write(json.dumps({"executor_model": "ollama:x:1b", "status": "failed"}) + "\n")
    applied2, _ = ingest_loophole_jsonl(path, since_offset=offset)
    assert applied2 == 1
