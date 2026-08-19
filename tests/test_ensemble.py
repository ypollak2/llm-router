"""LLM-first ensemble classifier + task-type complexity floor.

The ensemble's local-model calls are mocked so these run offline/fast; the blend,
tiebreak, floor, and routing-wrapper logic are what's under test.
"""
from __future__ import annotations

import pytest

from llm_router import ensemble
from llm_router.classify import apply_complexity_floor, classify_signals
from llm_router.types import ClassificationResult, Complexity, Subject, TaskType


def _result(task, complexity, confidence, model="ollama/fake") -> ClassificationResult:
    return ClassificationResult(
        complexity=complexity,
        confidence=confidence,
        reasoning="",
        inferred_task_type=task,
        classifier_model=model,
        classifier_cost_usd=0.0,
        classifier_latency_ms=1.0,
        subject=Subject.GENERAL,
    )


# ── Floor policy (single source of truth in classify.py) ─────────────────────
@pytest.mark.parametrize(
    "task,base,expected",
    [
        (TaskType.ANALYZE, Complexity.SIMPLE, Complexity.COMPLEX),   # clamp up
        (TaskType.RESEARCH, Complexity.MODERATE, Complexity.COMPLEX),
        (TaskType.GENERATE, Complexity.SIMPLE, Complexity.MODERATE),
        (TaskType.CODE, Complexity.SIMPLE, Complexity.MODERATE),
        (TaskType.QUERY, Complexity.SIMPLE, Complexity.SIMPLE),       # no floor
        (TaskType.ANALYZE, Complexity.DEEP_REASONING, Complexity.DEEP_REASONING),  # never down
    ],
)
def test_floor_clamps_up_never_down(task, base, expected):
    assert apply_complexity_floor(base, task) is expected


def test_floor_applied_by_default_in_signals():
    # "Analyze why our p95 latency spikes" → analyze; floor forces >= complex.
    sig = classify_signals("Analyze why our p95 latency spikes during cache warmup")
    assert sig.task_type is TaskType.ANALYZE
    assert sig.complexity in (Complexity.COMPLEX, Complexity.DEEP_REASONING)


# ── Blend: LLM task-type + heuristic vote ────────────────────────────────────
@pytest.mark.asyncio
async def test_ensemble_blends_llm_and_heuristic(monkeypatch):
    # LLM says analyze/simple (under-rates complexity); floor must lift to complex.
    async def fake_local(prompt, model, **kw):
        return _result(TaskType.ANALYZE, Complexity.SIMPLE, 0.9, model)

    monkeypatch.setattr(ensemble, "local_llm_classify", fake_local)
    res = await ensemble.classify_ensemble("compare kafka and sqs", secondary=None)
    assert res.inferred_task_type is TaskType.ANALYZE
    assert res.complexity is Complexity.COMPLEX  # floor lifted simple→complex


@pytest.mark.asyncio
async def test_ensemble_tiebreak_only_on_thin_margin(monkeypatch):
    calls = []

    async def fake_local(prompt, model, **kw):
        calls.append(model)
        # Primary is highly confident → margin wide → secondary must NOT run.
        return _result(TaskType.CODE, Complexity.MODERATE, 0.95, model)

    monkeypatch.setattr(ensemble, "local_llm_classify", fake_local)
    await ensemble.classify_ensemble(
        "implement pagination for the users endpoint",
        primary="ollama/p", secondary="ollama/s",
    )
    assert calls == ["ollama/p"], "secondary should not fire on a confident primary"


@pytest.mark.asyncio
async def test_ensemble_falls_back_safely_on_model_failure(monkeypatch):
    async def boom(prompt, model, **kw):
        return ensemble._fallback_result("simulated failure")

    monkeypatch.setattr(ensemble, "local_llm_classify", boom)
    res = await ensemble.classify_ensemble("what is 2+2", secondary=None)
    # Never raises; returns a usable result.
    assert isinstance(res, ClassificationResult)


# ── Routing wrapper gate ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_classify_for_routing_uses_ensemble_when_enabled(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "on")
    called = {}

    async def fake_ensemble(prompt, **kw):
        called["ensemble"] = True
        return _result(TaskType.QUERY, Complexity.SIMPLE, 0.9)

    monkeypatch.setattr(ensemble, "classify_ensemble", fake_ensemble)
    await ensemble.classify_for_routing("hello", timeout_seconds=10.0)
    assert called.get("ensemble") is True


@pytest.mark.asyncio
async def test_classify_for_routing_defers_to_cloud_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "off")
    import llm_router.classifier as clf
    called = {}

    async def fake_cloud(prompt, **kw):
        called["cloud"] = True
        return _result(TaskType.QUERY, Complexity.SIMPLE, 0.9)

    monkeypatch.setattr(clf, "classify_complexity", fake_cloud)
    await ensemble.classify_for_routing("hello", timeout_seconds=10.0)
    assert called.get("cloud") is True


# ── Cold-start warmup (#3) ───────────────────────────────────────────────────
def test_warm_primary_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "off")
    started = {"threads": 0}
    import threading
    real = threading.Thread

    def counting(*a, **k):
        started["threads"] += 1
        return real(*a, **k)

    monkeypatch.setattr(threading, "Thread", counting)
    ensemble.warm_primary()  # must not spawn any work
    assert started["threads"] == 0


def test_warm_primary_noop_for_non_local_model(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "on")
    started = {"threads": 0}
    import threading
    real = threading.Thread
    monkeypatch.setattr(
        threading, "Thread",
        lambda *a, **k: (started.__setitem__("threads", started["threads"] + 1), real(*a, **k))[1],
    )
    ensemble.warm_primary(model="anthropic/claude-haiku")  # not ollama → skip
    assert started["threads"] == 0


def test_warm_primary_is_non_blocking(monkeypatch):
    # Even if the underlying call would hang, warm_primary returns immediately
    # (work is on a daemon thread). We assert it returns without raising.
    monkeypatch.setenv("LLM_ROUTER_ENSEMBLE", "on")
    assert ensemble.warm_primary(model="ollama/nonexistent-test-model") is None
