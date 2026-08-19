"""1c — the in-process SDK: ``from llm_router import route``."""
import pytest

import llm_router
from llm_router import RouteResult, RoutingError, route


def test_route_is_exported():
    assert callable(llm_router.route)
    assert "route" in llm_router.__all__


def test_route_result_total_tokens():
    r = RouteResult(text="hi", model="ollama/x", provider="ollama",
                    input_tokens=10, output_tokens=5, latency_ms=100)
    assert r.total_tokens == 15


def test_empty_prompt_raises():
    with pytest.raises(ValueError):
        route("   ")


def test_chain_exhausted_raises(monkeypatch):
    # Force the router to fail → RoutingError so callers can fall back.
    monkeypatch.setattr("llm_router.hooks.chain_builder.get_current_pressure",
                        lambda: ("green", 5))
    monkeypatch.setattr("llm_router.hooks.chain_builder.build_chain",
                        lambda *a, **k: [])
    monkeypatch.setattr("llm_router.hooks.direct_executor.execute_chain",
                        lambda *a, **k: None)
    with pytest.raises(RoutingError):
        route("hello world, a non-empty prompt")
