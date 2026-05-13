"""Tests for v8.2.0 routing explainability feature."""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import patch

from llm_router.tools.text import (
    _get_explain_mode,
    _routing_explanation,
    _savings_info,
    _format_response,
)
from llm_router.types import LLMResponse


def _make_response(**overrides) -> LLMResponse:
    """Create a test LLMResponse with sensible defaults."""
    defaults = dict(
        content="Test response content",
        model="gemini/gemini-2.5-flash",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0002,
        latency_ms=150.0,
        provider="gemini",
        confidence=0.94,
        classification_method="heuristic",
        complexity="simple",
        task_type_str="query",
        chain_attempts=["ollama/qwen3.5:latest", "gemini/gemini-2.5-flash"],
    )
    defaults.update(overrides)
    return LLMResponse(**defaults)


# ── Mode resolution ──────────────────────────────────────────────────────────


class TestExplainMode:
    def test_default_is_footer(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_ROUTER_EXPLAIN", None)
            mode = _get_explain_mode()
            assert mode == "footer"

    def test_legacy_1_maps_to_header(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "1"}):
            assert _get_explain_mode() == "header"

    def test_explicit_off(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "off"}):
            assert _get_explain_mode() == "off"

    def test_explicit_verbose(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "verbose"}):
            assert _get_explain_mode() == "verbose"

    def test_explicit_footer(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            assert _get_explain_mode() == "footer"


# ── Savings calculation ──────────────────────────────────────────────────────


class TestSavingsInfo:
    def test_cheaper_model_shows_ratio(self):
        resp = _make_response(model="gemini/gemini-2.5-flash", cost_usd=0.0002)
        label, saved = _savings_info(resp)
        assert "cheaper" in label
        assert saved > 0

    def test_sonnet_baseline_no_savings(self):
        resp = _make_response(model="anthropic/claude-sonnet-4-6", cost_usd=0.015)
        label, saved = _savings_info(resp)
        assert label == ""
        assert saved == 0.0

    def test_unknown_model_no_savings(self):
        resp = _make_response(model="unknown/mystery-model", cost_usd=0.001)
        label, saved = _savings_info(resp)
        assert label == ""

    def test_zero_cost_no_crash(self):
        resp = _make_response(model="gemini/gemini-2.5-flash", cost_usd=0.0)
        label, saved = _savings_info(resp)
        assert "cheaper" in label
        assert saved == 0.0

    def test_opus_more_expensive(self):
        resp = _make_response(model="anthropic/claude-opus-4-6", cost_usd=0.075)
        label, saved = _savings_info(resp)
        # Opus is more expensive than Sonnet, no "cheaper" label
        assert label == ""


# ── Routing explanation ──────────────────────────────────────────────────────


class TestRoutingExplanation:
    def test_off_returns_empty(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "off"}):
            resp = _make_response()
            assert _routing_explanation(resp, "query") == ""

    def test_footer_contains_model_name(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            resp = _make_response(model="gemini/gemini-2.5-flash")
            result = _routing_explanation(resp, "query")
            assert "gemini-2.5-flash" in result

    def test_footer_contains_cost(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            resp = _make_response(cost_usd=0.00035)
            result = _routing_explanation(resp, "query")
            assert "$0.00035" in result

    def test_footer_contains_complexity(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            resp = _make_response(complexity="simple")
            result = _routing_explanation(resp, "query")
            assert "simple" in result

    def test_footer_starts_with_separator(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            resp = _make_response()
            result = _routing_explanation(resp, "query")
            assert result.startswith("\n─────")

    def test_header_uses_brackets(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "header"}):
            resp = _make_response()
            result = _routing_explanation(resp, "query")
            assert result.startswith("[→")
            assert result.rstrip().endswith("]")

    def test_verbose_shows_chain(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "verbose"}):
            resp = _make_response(
                chain_attempts=["ollama/qwen3.5:latest", "gemini/gemini-2.5-flash"],
            )
            result = _routing_explanation(resp, "query")
            assert "Chain:" in result
            assert "qwen3.5" in result
            assert "[✗]" in result
            assert "[✓]" in result

    def test_verbose_shows_confidence(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "verbose"}):
            resp = _make_response(confidence=0.94, classification_method="heuristic")
            result = _routing_explanation(resp, "query")
            assert "94%" in result
            assert "heuristic" in result

    def test_verbose_shows_savings(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "verbose"}):
            resp = _make_response(model="gemini/gemini-2.5-flash", cost_usd=0.0002)
            result = _routing_explanation(resp, "query")
            assert "cheaper" in result
            assert "saved" in result


# ── Format response integration ──────────────────────────────────────────────


class TestFormatResponse:
    def test_footer_appears_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_ROUTER_EXPLAIN", None)
            resp = _make_response()
            result = _format_response(resp, "query")
            assert "─────" in result
            assert "gemini-2.5-flash" in result

    def test_off_suppresses_footer(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "off"}):
            resp = _make_response()
            result = _format_response(resp, "query")
            assert "─────" not in result

    def test_header_places_above_content(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "header"}):
            resp = _make_response()
            result = _format_response(resp, "query")
            lines = result.split("\n")
            # Header should be first line
            assert lines[0].startswith("[→")

    def test_footer_places_after_content(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            resp = _make_response(content="Hello world")
            result = _format_response(resp, "query")
            content_idx = result.index("Hello world")
            separator_idx = result.index("─────")
            assert separator_idx > content_idx

    def test_all_tasks_have_explanation(self):
        with patch.dict(os.environ, {"LLM_ROUTER_EXPLAIN": "footer"}):
            resp = _make_response()
            for task in ["query", "code", "analyze", "generate", "research"]:
                result = _format_response(resp, task)
                assert "─────" in result, f"Missing footer for task={task}"


# ── LLMResponse explainability fields ────────────────────────────────────────


class TestLLMResponseFields:
    def test_default_values(self):
        resp = LLMResponse(
            content="test", model="m", input_tokens=0,
            output_tokens=0, cost_usd=0.0, latency_ms=0.0, provider="p",
        )
        assert resp.confidence == 0.0
        assert resp.classification_method == ""
        assert resp.complexity == ""
        assert resp.task_type_str == ""
        assert resp.chain_attempts == []

    def test_fields_set_correctly(self):
        resp = _make_response(
            confidence=0.95,
            classification_method="ollama",
            complexity="moderate",
            chain_attempts=["a", "b"],
        )
        assert resp.confidence == 0.95
        assert resp.classification_method == "ollama"
        assert resp.complexity == "moderate"
        assert resp.chain_attempts == ["a", "b"]

    def test_frozen_replace(self):
        resp = _make_response(confidence=0.5)
        resp2 = replace(resp, confidence=0.9)
        assert resp.confidence == 0.5
        assert resp2.confidence == 0.9
