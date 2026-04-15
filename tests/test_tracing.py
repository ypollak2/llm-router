"""Tests for tracing helpers and instrumentation hooks."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import llm_router.tracing as tracing
from llm_router.cache import get_cache
from llm_router.chain_builder import build_chain
from llm_router.classifier import classify_complexity
from llm_router.router import route_and_call
from llm_router.scorer import score_all_models
from llm_router.types import (
    Complexity,
    LLMResponse,
    ModelCapability,
    ProviderTier,
    RoutingProfile,
    ScoredModel,
    TaskType,
)


class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes: dict[str, object] = {}
        self.exceptions: list[BaseException] = []
        self.statuses: list[object] = []

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)

    def set_status(self, status: object) -> None:
        self.statuses.append(status)


class FakeTracer:
    def __init__(self):
        self.spans: list[FakeSpan] = []

    def start_as_current_span(self, name: str, **kwargs: object):
        span = FakeSpan(name)
        self.spans.append(span)

        class _ContextManager:
            def __enter__(self_inner):
                return span

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _ContextManager()


@pytest.fixture(autouse=True)
async def clear_classifier_cache():
    cache = get_cache()
    await cache.clear()
    yield
    await cache.clear()


def test_get_tracer_returns_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(tracing, "_CONFIGURED", False)
    monkeypatch.setattr(tracing, "_TRACE_API", None)
    monkeypatch.setattr(tracing, "_STATUS", None)
    monkeypatch.setattr(tracing, "_STATUS_CODE", None)

    tracer = tracing.get_tracer("llm_router.test")
    with tracer.start_as_current_span("noop") as span:
        tracing.set_span_attributes(span, task_type=TaskType.QUERY, answer=42)

    assert span.is_recording() is False


@pytest.mark.asyncio
async def test_classify_complexity_emits_span(mock_env):
    tracer = FakeTracer()
    response = LLMResponse(
        content='{"complexity":"simple","task_type":"query","confidence":0.9,"reasoning":"fact"}',
        model="gemini/gemini-2.5-flash",
        input_tokens=12,
        output_tokens=9,
        cost_usd=0.00001,
        latency_ms=123.0,
        provider="gemini",
    )

    with patch("llm_router.tracing.get_tracer", return_value=tracer):
        with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=response):
            result = await classify_complexity("What is 2+2?")

    span = next(span for span in tracer.spans if span.name == "classify_complexity")
    assert result.complexity == Complexity.SIMPLE
    assert span.attributes["prompt_version"] == "v1"
    assert span.attributes["complexity"] == "simple"
    assert span.attributes["classifier_model"] == result.classifier_model


@pytest.mark.asyncio
async def test_route_and_call_emits_route_and_provider_spans(mock_env, mock_acompletion):
    tracer = FakeTracer()

    with patch("llm_router.tracing.get_tracer", return_value=tracer):
        response = await route_and_call(
            TaskType.QUERY,
            "Hello",
            model_override="openai/gpt-4o",
            complexity_hint="simple",
        )

    route_span = next(span for span in tracer.spans if span.name == "route_and_call")
    provider_span = next(span for span in tracer.spans if span.name == "provider_call")
    assert route_span.attributes["task_type"] == "query"
    assert route_span.attributes["final_model"] == response.model
    assert provider_span.attributes["response_model"] == response.model
    assert provider_span.attributes["provider"] == "openai"


@pytest.mark.asyncio
async def test_build_chain_emits_span():
    tracer = FakeTracer()
    capability = ModelCapability(
        model_id="ollama/qwen3:32b",
        provider="ollama",
        provider_tier=ProviderTier.LOCAL,
        task_types=frozenset({TaskType.QUERY}),
    )
    scored_model = ScoredModel(
        model_id=capability.model_id,
        capability=capability,
        score=0.91,
        quality_score=0.8,
        budget_score=1.0,
        latency_score=0.9,
        acceptance_score=0.8,
    )

    with patch("llm_router.tracing.get_tracer", return_value=tracer):
        with patch("llm_router.discover.discover_available_models", new_callable=AsyncMock, return_value={capability.model_id: capability}):
            with patch("llm_router.scorer.score_all_models", new_callable=AsyncMock, return_value=[scored_model]):
                chain = await build_chain(TaskType.QUERY, "simple", RoutingProfile.BUDGET)

    span = next(span for span in tracer.spans if span.name == "build_chain")
    assert chain[0] == capability.model_id
    assert span.attributes["dynamic_enabled"] is True
    assert span.attributes["top_model"] == capability.model_id


@pytest.mark.asyncio
async def test_score_all_models_emits_span():
    tracer = FakeTracer()
    capability = ModelCapability(
        model_id="openai/gpt-4o",
        provider="openai",
        provider_tier=ProviderTier.CHEAP_PAID,
        task_types=frozenset({TaskType.QUERY}),
    )

    with patch("llm_router.tracing.get_tracer", return_value=tracer):
        with patch("llm_router.cost.get_model_failure_rates", new_callable=AsyncMock, return_value={}):
            with patch("llm_router.cost.get_model_latency_stats", new_callable=AsyncMock, return_value={}):
                with patch("llm_router.cost.get_model_acceptance_scores", new_callable=AsyncMock, return_value={}):
                    with patch("llm_router.scorer._fetch_pressures", new_callable=AsyncMock, return_value={capability.model_id: 0.1}):
                        scored = await score_all_models([capability], "query", "simple")

    span = next(span for span in tracer.spans if span.name == "score_all_models")
    assert scored[0].model_id == capability.model_id
    assert span.attributes["top_model"] == capability.model_id
    assert span.attributes["scored_models"] == 1
