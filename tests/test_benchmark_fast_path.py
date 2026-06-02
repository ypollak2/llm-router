"""Plan 07 Phase 3 (Category C) — benchmark prompt fast-paths.

Templated benchmark prompts (RouterArena, MMLU, HELM, etc.) have stable
prefixes. Pattern-matching them is O(constant) and free, skipping the
entire LLM classifier chain. The fast-path emits a classification dict
the same shape as other fast-paths plus a `subject` field for
forward-compatibility with Phase 3 B.

Loading auto-route.py is awkward because of the hyphen in the filename;
we use importlib.util.spec_from_file_location to import it as a module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_auto_route():
    """Load src/llm_router/hooks/auto-route.py as a module."""
    cached = sys.modules.get("auto_route_under_test")
    if cached is not None:
        return cached
    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location("auto_route_under_test", path)
    assert spec and spec.loader, f"Could not load spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auto_route():
    return _load_auto_route()


class TestBenchmarkFastPathMatchers:
    """Each known benchmark template must classify to the documented tuple."""

    def test_executable_python_function_routes_to_code(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "Generate an executable Python function that sorts a list of integers."
        )
        assert result is not None
        assert result["task_type"] == "code"
        assert result["subject"] == "code"
        assert result["complexity"] == "moderate"
        assert result["method"] == "benchmark-fp"

    def test_context_question_routes_to_narrative_query(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "Please read the following context and answer the question.\n\nContext: ..."
        )
        assert result is not None
        assert result["task_type"] == "query"
        assert result["subject"] == "narrative"
        assert result["complexity"] == "moderate"

    def test_mcq_routes_to_general_query(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "Please read the following multiple-choice questions and choose the best answer."
        )
        assert result is not None
        assert result["task_type"] == "query"
        assert result["subject"] == "general"

    def test_translate_routes_to_generate_simple(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "Translate the following sentence to French: 'Hello world.'"
        )
        assert result is not None
        assert result["task_type"] == "generate"
        assert result["complexity"] == "simple"

    def test_passage_cloze_routes_to_cloze(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "Read the following passage and answer the question by choosing the correct option."
        )
        assert result is not None
        assert result["task_type"] == "query"
        assert result["subject"] == "cloze"

    def test_consider_the_word_routes_to_cloze_simple(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            'Consider the word "bank" in the sentence below.'
        )
        assert result is not None
        assert result["subject"] == "cloze"
        assert result["complexity"] == "simple"

    def test_chess_routes_to_reasoning_analyze(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "You are given a question about chess moves and must analyze the position."
        )
        assert result is not None
        assert result["task_type"] == "analyze"
        assert result["subject"] == "reasoning"


class TestBenchmarkFastPathNonMatches:
    """Prompts that don't match any template must return None — the classifier
    chain handles them normally."""

    def test_generic_question_returns_none(self, auto_route) -> None:
        assert auto_route.benchmark_fast_path("What is 2+2?") is None

    def test_short_acknowledgement_returns_none(self, auto_route) -> None:
        assert auto_route.benchmark_fast_path("yes") is None

    def test_unrelated_code_request_returns_none(self, auto_route) -> None:
        """Prompts that ask for code work but don't match the templated prefix
        must NOT match — they should flow to the existing build/heuristic fast-paths."""
        assert auto_route.benchmark_fast_path(
            "Refactor this function to use async/await"
        ) is None

    def test_empty_string_returns_none(self, auto_route) -> None:
        assert auto_route.benchmark_fast_path("") is None


class TestBenchmarkFastPathRobustness:
    """Behavioral edge cases — whitespace, case, partial prefixes."""

    def test_leading_whitespace_is_tolerated(self, auto_route) -> None:
        result = auto_route.benchmark_fast_path(
            "   \n\n  Generate an executable Python function that does X."
        )
        assert result is not None
        assert result["task_type"] == "code"

    def test_case_sensitivity_matters(self, auto_route) -> None:
        """Templates use exact casing (e.g. 'Generate an...') — the lowercase
        variant should NOT match because it's likely user-written, not templated."""
        result = auto_route.benchmark_fast_path(
            "generate an executable python function for me please"
        )
        # Either None or a non-benchmark match; the key invariant is that
        # method is NOT "benchmark-fp".
        if result is not None:
            assert result.get("method") != "benchmark-fp"

    def test_prefix_must_be_at_start(self, auto_route) -> None:
        """The pattern is anchored — prefix appearing mid-prompt is not a match."""
        result = auto_route.benchmark_fast_path(
            "Hi there. Generate an executable Python function for me."
        )
        assert result is None or result.get("method") != "benchmark-fp"


class TestPipelineIntegration:
    """classify_prompt must consult benchmark_fast_path before falling back to
    heuristic / Ollama / API classifiers."""

    def test_classify_prompt_uses_benchmark_fast_path(self, auto_route) -> None:
        result = auto_route.classify_prompt(
            "Generate an executable Python function to compute fibonacci."
        )
        assert result is not None
        assert result["method"] == "benchmark-fp"
        assert result["task_type"] == "code"
