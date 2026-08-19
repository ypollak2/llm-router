"""Tests for quality_feedback module — auto-scoring and routing feedback."""

import pytest

from llm_router.quality_feedback import (
    QUALITY_THRESHOLD,
    ModelQuality,
    get_model_quality,
    get_quality_summary,
    record_quality,
    reset_quality_store,
    score_response,
    should_skip_model,
)


@pytest.fixture(autouse=True)
def clean_store():
    """Reset quality store between tests."""
    reset_quality_store()
    yield
    reset_quality_store()


class TestScoreResponse:
    """Test the automatic quality scoring heuristics."""

    def test_empty_response_scores_zero(self):
        qs = score_response("", "query")
        assert qs.score == 0.0
        assert "empty response" in qs.reasons

    def test_whitespace_only_scores_zero(self):
        qs = score_response("   \n  ", "code")
        assert qs.score == 0.0

    def test_short_refusal_scores_low(self):
        qs = score_response("I cannot help with that.", "query")
        assert qs.score < 0.4
        assert "contains refusal" in qs.reasons

    def test_code_response_with_blocks_scores_high(self):
        response = """Here's the implementation:

```python
def hello():
    return "world"
```

This function returns a greeting."""
        qs = score_response(response, "code")
        assert qs.score >= 0.7
        assert "contains code" in qs.reasons

    def test_research_with_urls_scores_high(self):
        response = """## Findings

Based on research from https://example.com/paper and https://arxiv.org/abs/123:

- Finding 1: significant improvement
- Finding 2: robust across datasets."""
        qs = score_response(response, "research")
        assert qs.score >= 0.7
        assert any("citation" in r for r in qs.reasons)

    def test_analysis_with_structure_scores_well(self):
        response = """## Analysis

### Problem
The issue stems from incorrect state management.

### Solution
- Use immutable data structures
- Add validation at boundaries
- Test edge cases thoroughly."""
        qs = score_response(response, "analyze")
        assert qs.score >= 0.6
        assert "structured" in qs.reasons

    def test_generation_with_substance_scores_well(self):
        response = """# Welcome to the Platform

Our platform helps developers build faster and more reliable applications
with less effort and lower cost.

With intelligent routing, your AI tools become significantly more cost-effective
while maintaining high quality across all providers and use cases. This is a
comprehensive solution for teams that care about both cost and quality."""
        qs = score_response(response, "generate")
        assert qs.score >= 0.5
        assert "substantial output" in qs.reasons

    def test_complete_response_gets_bonus(self):
        complete = "The answer is 42."
        incomplete = "The answer is 4"
        qs_complete = score_response(complete, "query")
        qs_incomplete = score_response(incomplete, "query")
        assert qs_complete.score >= qs_incomplete.score

    def test_score_capped_at_one(self):
        # Long structured code response with everything
        response = """## Solution

```python
def comprehensive_solution():
    \"\"\"This is a well-documented function.\"\"\"
    result = compute_complex_algorithm()
    return result
```

This implementation handles all edge cases. See https://docs.python.org for details."""
        qs = score_response(response, "code")
        assert qs.score <= 1.0

    def test_metadata_preserved(self):
        qs = score_response("Hello world.", "query", model="ollama/gemma4", complexity="simple")
        assert qs.task_type == "query"
        assert qs.model == "ollama/gemma4"
        assert qs.tokens > 0


class TestModelQuality:
    """Test the ModelQuality dataclass."""

    def test_initial_avg_is_neutral(self):
        mq = ModelQuality(model="test", task_type="code", complexity="moderate")
        assert mq.avg_quality == 0.5

    def test_record_updates_avg(self):
        mq = ModelQuality(model="test", task_type="code", complexity="moderate")
        mq.record(0.8)
        mq.record(0.6)
        assert mq.avg_quality == pytest.approx(0.7)
        assert mq.call_count == 2

    def test_record_updates_timestamp(self):
        import time
        mq = ModelQuality(model="test", task_type="code", complexity="moderate")
        before = time.time()
        mq.record(0.5)
        assert mq.last_updated >= before


class TestRecordAndRetrieve:
    """Test recording and retrieving quality data."""

    def test_record_and_get_quality(self):
        record_quality("ollama/gemma4", "code", "simple", 0.8)
        record_quality("ollama/gemma4", "code", "simple", 0.7)
        record_quality("ollama/gemma4", "code", "simple", 0.9)

        quality = get_model_quality("ollama/gemma4", "code", "simple")
        assert quality == pytest.approx(0.8, abs=0.01)

    def test_insufficient_data_returns_none(self):
        record_quality("ollama/gemma4", "code", "simple", 0.8)
        # Only 1 call, need 3 for signal
        quality = get_model_quality("ollama/gemma4", "code", "simple")
        assert quality is None

    def test_different_patterns_tracked_separately(self):
        # Record good quality for code/simple
        for _ in range(3):
            record_quality("ollama/gemma4", "code", "simple", 0.9)

        # Record bad quality for analyze/complex
        for _ in range(3):
            record_quality("ollama/gemma4", "analyze", "complex", 0.2)

        assert get_model_quality("ollama/gemma4", "code", "simple") == pytest.approx(0.9)
        assert get_model_quality("ollama/gemma4", "analyze", "complex") == pytest.approx(0.2)


class TestShouldSkipModel:
    """Test the routing feedback mechanism."""

    def test_no_data_does_not_skip(self):
        assert should_skip_model("ollama/gemma4", "code", "moderate") is False

    def test_good_model_not_skipped(self):
        for _ in range(5):
            record_quality("ollama/gemma4", "code", "simple", 0.8)
        assert should_skip_model("ollama/gemma4", "code", "simple") is False

    def test_bad_model_skipped(self):
        for _ in range(5):
            record_quality("ollama/gemma4", "analyze", "complex", 0.2)
        assert should_skip_model("ollama/gemma4", "analyze", "complex") is True

    def test_threshold_boundary(self):
        # Exactly at threshold
        for _ in range(3):
            record_quality("test/model", "query", "simple", QUALITY_THRESHOLD)
        # At threshold = not skipped (must be below)
        assert should_skip_model("test/model", "query", "simple") is False

        # Just below threshold
        reset_quality_store()
        for _ in range(3):
            record_quality("test/model", "query", "simple", QUALITY_THRESHOLD - 0.01)
        assert should_skip_model("test/model", "query", "simple") is True

    def test_insufficient_calls_not_skipped(self):
        # Only 2 calls (below _MIN_CALLS_FOR_SIGNAL of 3)
        record_quality("ollama/gemma4", "code", "moderate", 0.1)
        record_quality("ollama/gemma4", "code", "moderate", 0.1)
        assert should_skip_model("ollama/gemma4", "code", "moderate") is False


class TestQualitySummary:
    """Test the summary/report functionality."""

    def test_empty_summary(self):
        assert get_quality_summary() == {}

    def test_summary_structure(self):
        for _ in range(3):
            record_quality("ollama/gemma4", "code", "simple", 0.8)
        record_quality("openai/gpt-4o", "analyze", "complex", 0.9)

        summary = get_quality_summary()
        assert "ollama/gemma4" in summary
        assert "code/simple" in summary["ollama/gemma4"]
        assert summary["ollama/gemma4"]["code/simple"]["avg_quality"] == pytest.approx(0.8)
        assert summary["ollama/gemma4"]["code/simple"]["call_count"] == 3
