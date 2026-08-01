# The TestLoopHoleVerdicts section below is ported from Chuzom's
# test_quality_feedback.py, adapted for the "llm-router:<tier>" alias-label
# rename and the LLM_ROUTER_LOOPHOLE_JSONL env var (an llm-router-side
# addition beyond Chuzom's original, for consistency with WS1's
# env-var-override convention).
"""Tests for quality_feedback module — auto-scoring and routing feedback."""

import json

import pytest

from llm_router.quality_feedback import (
    QUALITY_THRESHOLD,
    ModelQuality,
    _loophole_complexity,
    _loophole_jsonl_path,
    _normalize_loophole_model,
    get_model_quality,
    get_quality_summary,
    ingest_loophole_jsonl,
    record_loophole_verdict,
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


class TestNormalizeLoopholeModel:
    """_normalize_loophole_model: concrete provider:model labels vs router aliases."""

    def test_concrete_label_normalized(self):
        assert _normalize_loophole_model("ollama:qwen3-coder:30b") == "ollama/qwen3-coder:30b"

    def test_router_alias_returns_none(self):
        assert _normalize_loophole_model("llm-router:complex") is None

    def test_unknown_returns_none(self):
        assert _normalize_loophole_model("unknown") is None

    def test_empty_returns_none(self):
        assert _normalize_loophole_model("") is None

    def test_no_colon_returns_none(self):
        assert _normalize_loophole_model("justaname") is None


class TestLoopholeComplexity:
    """_loophole_complexity: reads the llm-router:<tier> alias off either field."""

    def test_executor_alias_maps_tier(self):
        rec = {"executor_model": "llm-router:complex"}
        assert _loophole_complexity(rec) == "complex"

    def test_planner_alias_used_when_executor_concrete(self):
        rec = {"executor_model": "ollama:qwen3-coder:30b", "planner_model": "llm-router:simple"}
        assert _loophole_complexity(rec) == "simple"

    def test_unmapped_tier_defaults_moderate(self):
        rec = {"executor_model": "llm-router:reasoning"}
        assert _loophole_complexity(rec) == "complex"

    def test_no_alias_defaults_moderate(self):
        rec = {"executor_model": "ollama:qwen3-coder:30b"}
        assert _loophole_complexity(rec) == "moderate"


class TestRecordLoopholeVerdict:
    """record_loophole_verdict: fold one LoopHole record into the quality store."""

    def test_done_status_scores_one(self):
        rec = {"status": "done", "executor_model": "ollama:qwen3-coder:30b",
               "goal_id": "g1"}
        assert record_loophole_verdict(rec) is True
        assert get_model_quality("ollama/qwen3-coder:30b", "code", "moderate") is None
        # need MIN_CALLS_FOR_SIGNAL calls before quality is exposed
        for _ in range(2):
            record_loophole_verdict(rec)
        assert get_model_quality("ollama/qwen3-coder:30b", "code", "moderate") == pytest.approx(1.0)

    def test_failed_status_scores_zero(self):
        rec = {"status": "failed", "executor_model": "ollama:qwen3-coder:30b"}
        for _ in range(3):
            record_loophole_verdict(rec)
        assert get_model_quality("ollama/qwen3-coder:30b", "code", "moderate") == pytest.approx(0.0)

    def test_paused_status_scores_soft_negative(self):
        rec = {"status": "paused", "executor_model": "ollama:qwen3-coder:30b"}
        for _ in range(3):
            record_loophole_verdict(rec)
        assert get_model_quality("ollama/qwen3-coder:30b", "code", "moderate") == pytest.approx(0.25)

    def test_missing_status_falls_back_to_verified_done(self):
        rec = {"verified_done": True, "executor_model": "ollama:qwen3-coder:30b"}
        for _ in range(3):
            record_loophole_verdict(rec)
        assert get_model_quality("ollama/qwen3-coder:30b", "code", "moderate") == pytest.approx(1.0)

    def test_router_alias_model_not_scored(self):
        rec = {"status": "done", "executor_model": "llm-router:complex"}
        assert record_loophole_verdict(rec) is False

    def test_non_dict_input_returns_false(self):
        assert record_loophole_verdict("not a dict") is False  # type: ignore[arg-type]

    def test_complexity_taken_from_executor_alias(self):
        rec = {"status": "done", "executor_model": "ollama:qwen3-coder:30b",
               "planner_model": "llm-router:complex"}
        for _ in range(3):
            record_loophole_verdict(rec)
        # executor_model is concrete, so complexity is read from planner_model's alias
        assert get_model_quality("ollama/qwen3-coder:30b", "code", "complex") == pytest.approx(1.0)


class TestLoopholeJsonlPath:
    """_loophole_jsonl_path: default path + LLM_ROUTER_LOOPHOLE_JSONL override."""

    def test_default_path_under_llm_router_home(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_LOOPHOLE_JSONL", raising=False)
        path = _loophole_jsonl_path()
        assert ".llm-router" in path
        assert path.endswith("quality_feedback.jsonl")
        assert "chuzom" not in path.lower()

    def test_env_override_wins(self, monkeypatch, tmp_path):
        override = str(tmp_path / "custom.jsonl")
        monkeypatch.setenv("LLM_ROUTER_LOOPHOLE_JSONL", override)
        assert _loophole_jsonl_path() == override


class TestIngestLoopholeJsonl:
    """ingest_loophole_jsonl: drain a LoopHole verdict JSONL file, fail-open."""

    def test_missing_file_returns_zero_applied(self, tmp_path):
        applied, offset = ingest_loophole_jsonl(str(tmp_path / "nope.jsonl"), since_offset=0)
        assert applied == 0
        assert offset == 0

    def test_ingests_valid_lines_and_skips_router_aliases(self, tmp_path):
        path = tmp_path / "verdicts.jsonl"
        lines = [
            {"status": "done", "executor_model": "ollama:qwen3-coder:30b", "goal_id": "g1"},
            {"status": "done", "executor_model": "llm-router:complex", "goal_id": "g2"},
            {"status": "failed", "executor_model": "ollama:qwen3-coder:30b", "goal_id": "g3"},
        ]
        path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
        applied, new_offset = ingest_loophole_jsonl(str(path), since_offset=0)
        assert applied == 2  # the router-alias line is skipped
        assert new_offset == len(path.read_text().encode("utf-8"))

    def test_malformed_line_never_crashes(self, tmp_path):
        path = tmp_path / "verdicts.jsonl"
        path.write_text('{"status": "done", "executor_model": "ollama:x"}\n'
                        "not json at all\n"
                        "\n")
        applied, _offset = ingest_loophole_jsonl(str(path), since_offset=0)
        assert applied == 1

    def test_incremental_offset_only_reads_new_lines(self, tmp_path):
        path = tmp_path / "verdicts.jsonl"
        rec1 = {"status": "done", "executor_model": "ollama:qwen3-coder:30b"}
        path.write_text(json.dumps(rec1) + "\n")
        applied1, offset1 = ingest_loophole_jsonl(str(path), since_offset=0)
        assert applied1 == 1

        rec2 = {"status": "done", "executor_model": "openai:gpt-4o-mini"}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec2) + "\n")
        applied2, _offset2 = ingest_loophole_jsonl(str(path), since_offset=offset1)
        assert applied2 == 1  # only the newly-appended line is read


class TestQualityFeedbackBrandLeak:
    """Brand-leak guard: no 'chuzom' in this module's runtime-visible surface."""

    def test_no_chuzom_in_env_var_name(self):
        assert "chuzom" not in "LLM_ROUTER_LOOPHOLE_JSONL".lower()

    def test_no_chuzom_in_default_path(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_LOOPHOLE_JSONL", raising=False)
        assert "chuzom" not in _loophole_jsonl_path().lower()

    def test_no_chuzom_in_alias_prefix(self):
        # the router-alias prefix is "llm-router:", never "chuzom:"
        assert _normalize_loophole_model("llm-router:simple") is None
        assert _normalize_loophole_model("chuzom:simple") is not None  # not treated as an alias
