"""The ensemble's third vote: complexity_knn on the frontier boundary (Phase 2.1)."""
from __future__ import annotations

import pytest

from llm_router import complexity_knn as ck
from llm_router import ensemble
from llm_router.types import ClassificationResult, Complexity, Subject, TaskType


def _result(task, complexity, confidence) -> ClassificationResult:
    return ClassificationResult(complexity=complexity, confidence=confidence, reasoning="",
                                inferred_task_type=task, classifier_model="ollama/fake",
                                classifier_cost_usd=0.0, classifier_latency_ms=1.0,
                                subject=Subject.GENERAL)


def _primary(task, cx, conf):
    async def fake_local(prompt, model, **kw):
        return _result(task, cx, conf)
    return fake_local


def _knn(score, evidence=0.9, thr=0.5, calls=None):
    async def fake(prompt):
        if calls is not None:
            calls.append(prompt)
        if score is None:
            return None
        return ck.ComplexityScore(score, score >= thr, evidence, thr)
    return fake


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_COMPLEXITY_KNN", raising=False)


PROMPT = "hello there friend"  # low-signal for the heuristic: weight ~0


@pytest.mark.asyncio
async def test_off_by_default_the_score_is_never_computed(monkeypatch):
    calls = []
    monkeypatch.setattr(ensemble, "local_llm_classify", _primary(TaskType.QUERY, Complexity.SIMPLE, 0.3))
    monkeypatch.setattr(ck, "complexity_score", _knn(0.99, calls=calls))
    res = await ensemble.classify_ensemble(PROMPT, secondary=None)
    assert calls == []
    assert res.complexity is Complexity.SIMPLE


@pytest.mark.asyncio
async def test_on_a_confident_knn_vote_lifts_a_weak_simple_to_complex(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    monkeypatch.setattr(ensemble, "local_llm_classify", _primary(TaskType.QUERY, Complexity.SIMPLE, 0.3))
    monkeypatch.setattr(ck, "complexity_score", _knn(0.8, evidence=0.9))
    res = await ensemble.classify_ensemble(PROMPT, secondary=None)
    assert res.complexity is Complexity.COMPLEX
    assert "knn=0.80" in res.reasoning


@pytest.mark.asyncio
async def test_on_a_confident_llm_outvotes_the_knn(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    monkeypatch.setattr(ensemble, "local_llm_classify", _primary(TaskType.QUERY, Complexity.SIMPLE, 0.95))
    monkeypatch.setattr(ck, "complexity_score", _knn(0.8, evidence=0.5))
    res = await ensemble.classify_ensemble(PROMPT, secondary=None)
    assert res.complexity is Complexity.SIMPLE


@pytest.mark.asyncio
async def test_on_an_abstaining_knn_changes_nothing(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    monkeypatch.setattr(ensemble, "local_llm_classify", _primary(TaskType.QUERY, Complexity.SIMPLE, 0.3))
    monkeypatch.setattr(ck, "complexity_score", _knn(None))
    res = await ensemble.classify_ensemble(PROMPT, secondary=None)
    assert res.complexity is Complexity.SIMPLE
    assert "knn" not in res.reasoning


@pytest.mark.asyncio
async def test_a_not_frontier_vote_lowers_but_never_below_the_task_floor(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COMPLEXITY_KNN", "on")
    monkeypatch.setattr(ck, "complexity_score", _knn(0.05, evidence=0.95))
    monkeypatch.setattr(ensemble, "local_llm_classify", _primary(TaskType.QUERY, Complexity.COMPLEX, 0.3))
    res = await ensemble.classify_ensemble(PROMPT, secondary=None)
    assert res.complexity is Complexity.MODERATE
    # analyze carries a COMPLEX floor: the vote cannot take it below that
    monkeypatch.setattr(ensemble, "local_llm_classify", _primary(TaskType.ANALYZE, Complexity.COMPLEX, 0.3))
    res = await ensemble.classify_ensemble(PROMPT, secondary=None)
    assert res.inferred_task_type is TaskType.ANALYZE
    assert res.complexity is Complexity.COMPLEX
