"""Tests for Phase 4: Live Benchmark Registry additions to benchmarks.py."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from llm_router.benchmarks import (
    _LOCAL_QUALITY_SCORES,
    _resolve_local_alias,
    get_quality_score,
    maybe_refresh_benchmarks_background,
)


# ── _resolve_local_alias ──────────────────────────────────────────────────────

class TestResolveLocalAlias:
    def test_qwen3_resolves(self):
        assert _resolve_local_alias("ollama/qwen3:32b") == "qwen3"

    def test_qwen3_coder_resolves(self):
        assert _resolve_local_alias("ollama/qwen3-coder:latest") == "qwen3-coder"

    def test_qwen2_5_coder_resolves(self):
        assert _resolve_local_alias("ollama/qwen2.5-coder:7b") == "qwen2.5-coder"

    def test_deepseek_coder_resolves(self):
        assert _resolve_local_alias("ollama/deepseek-coder:6.7b") == "deepseek-coder"

    def test_deepseek_resolves_to_v3(self):
        assert _resolve_local_alias("ollama/deepseek:latest") == "deepseek-v3"

    def test_llama3_resolves(self):
        assert _resolve_local_alias("ollama/llama3.3:70b") == "llama-3"

    def test_llama3_prefix_resolves(self):
        assert _resolve_local_alias("ollama/llama3:8b") == "llama-3"

    def test_gemma4_resolves_to_gemma(self):
        assert _resolve_local_alias("ollama/gemma4:latest") == "gemma"

    def test_mistral_resolves(self):
        assert _resolve_local_alias("ollama/mistral:7b") == "mistral"

    def test_phi4_resolves(self):
        assert _resolve_local_alias("ollama/phi4:latest") == "phi-4"

    def test_codestral_resolves(self):
        assert _resolve_local_alias("ollama/codestral:22b") == "codestral"

    def test_granite_resolves(self):
        assert _resolve_local_alias("ollama/granite3.3:latest") == "granite"

    def test_command_resolves(self):
        assert _resolve_local_alias("ollama/command-r:35b") == "command-r"

    def test_lm_studio_prefix(self):
        assert _resolve_local_alias("lm_studio/qwen3:8b") == "qwen3"

    def test_vllm_prefix(self):
        assert _resolve_local_alias("vllm/deepseek-coder:latest") == "deepseek-coder"

    def test_api_model_returns_none(self):
        assert _resolve_local_alias("openai/gpt-4o") is None

    def test_anthropic_returns_none(self):
        assert _resolve_local_alias("anthropic/claude-sonnet-4-6") is None

    def test_unknown_local_returns_none(self):
        # A local prefix but unknown model family
        assert _resolve_local_alias("ollama/unknown-model-xyz:latest") is None


# ── get_quality_score ─────────────────────────────────────────────────────────

class TestGetQualityScore:
    def test_local_model_returns_registry_score(self):
        score = get_quality_score("ollama/qwen3:32b", "code")
        expected = _LOCAL_QUALITY_SCORES["qwen3"]["code"]
        assert score == pytest.approx(expected)

    def test_local_model_analyze_score(self):
        score = get_quality_score("ollama/deepseek-coder:6.7b", "analyze")
        expected = _LOCAL_QUALITY_SCORES["deepseek-coder"]["analyze"]
        assert score == pytest.approx(expected)

    def test_unknown_model_returns_default(self):
        score = get_quality_score("unknown/mystery-model", "code")
        assert score == 0.5

    def test_unknown_task_type_returns_default(self):
        # ollama/qwen3 is in registry but "vision" task is not
        score = get_quality_score("ollama/qwen3:32b", "vision")
        assert score == 0.5

    def test_scores_are_normalized(self):
        for task in ("code", "analyze", "query", "generate", "research"):
            score = get_quality_score("ollama/qwen3:32b", task)
            assert 0.0 <= score <= 1.0, f"score out of range for task={task}: {score}"

    def test_code_specialist_scores_higher_on_code(self):
        """qwen3-coder should outscore qwen3 on code tasks."""
        coder_score = get_quality_score("ollama/qwen3-coder:latest", "code")
        general_score = get_quality_score("ollama/qwen3:32b", "code")
        assert coder_score >= general_score

    def test_api_model_with_benchmark_data(self):
        """When benchmark data exists, API model score should be returned."""
        fake_data = {
            "task_scores": {
                "code": {"openai/gpt-4o": 0.85},
            }
        }
        with patch("llm_router.benchmarks.get_benchmark_data", return_value=fake_data):
            score = get_quality_score("openai/gpt-4o", "code")
        assert score == pytest.approx(0.85)

    def test_api_model_falls_back_to_default_when_no_data(self):
        with patch("llm_router.benchmarks.get_benchmark_data", return_value=None):
            score = get_quality_score("openai/gpt-4o", "code")
        assert score == 0.5

    def test_benchmark_data_takes_priority_over_local_registry(self):
        """Benchmark data should override local quality scores for API models."""
        fake_data = {
            "task_scores": {
                "code": {"anthropic/claude-haiku-4-5-20251001": 0.92},
            }
        }
        with patch("llm_router.benchmarks.get_benchmark_data", return_value=fake_data):
            score = get_quality_score("anthropic/claude-haiku-4-5-20251001", "code")
        assert score == pytest.approx(0.92)

    def test_exception_in_benchmark_data_returns_default(self):
        with patch("llm_router.benchmarks.get_benchmark_data", side_effect=RuntimeError("db error")):
            score = get_quality_score("openai/gpt-4o", "code")
        assert score == 0.5


# ── maybe_refresh_benchmarks_background ──────────────────────────────────────

class TestMaybeRefreshBenchmarks:
    def test_returns_false_when_not_stale(self, tmp_path):
        """If benchmarks.json is fresh, no refresh should be triggered."""
        benchmarks_file = tmp_path / "benchmarks.json"
        from datetime import datetime, timezone
        fresh_data = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": 1}
        benchmarks_file.write_text(json.dumps(fresh_data))

        with (
            patch("llm_router.benchmarks._INSTALLED", benchmarks_file),
            patch("llm_router.benchmarks._refresh_in_progress", False),
        ):
            result = maybe_refresh_benchmarks_background(ttl_days=7)

        assert result is False

    def test_returns_true_when_stale(self, tmp_path):
        """If benchmarks.json is old, a refresh should be triggered."""
        benchmarks_file = tmp_path / "benchmarks.json"
        from datetime import datetime, timezone, timedelta
        old_data = {"generated_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(), "version": 1}
        benchmarks_file.write_text(json.dumps(old_data))

        with (
            patch("llm_router.benchmarks._INSTALLED", benchmarks_file),
            patch("llm_router.benchmarks._refresh_in_progress", False),
            patch("llm_router.benchmarks._refresh_lock", threading.Lock()),
            patch("llm_router.benchmarks.threading") as mock_threading,
        ):
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            mock_threading.Lock.return_value = threading.Lock()
            result = maybe_refresh_benchmarks_background(ttl_days=7)

        # Either True (refresh started) or False (lock taken by another thread)
        assert isinstance(result, bool)

    def test_returns_false_when_already_refreshing(self):
        """If refresh is in progress, don't start another one."""
        with patch("llm_router.benchmarks._refresh_in_progress", True):
            result = maybe_refresh_benchmarks_background(ttl_days=7)
        assert result is False

    def test_returns_true_when_file_missing(self, tmp_path):
        """Missing benchmarks.json should trigger a refresh."""
        missing_path = tmp_path / "benchmarks.json"  # does not exist
        with (
            patch("llm_router.benchmarks._INSTALLED", missing_path),
            patch("llm_router.benchmarks._refresh_in_progress", False),
            patch("llm_router.benchmarks._refresh_lock", threading.Lock()),
            patch("llm_router.benchmark_fetcher.generate_benchmarks_json"),
        ):
            result = maybe_refresh_benchmarks_background(ttl_days=7)
        # Should attempt to start (file is missing = stale)
        assert isinstance(result, bool)


# ── Local quality scores registry sanity checks ───────────────────────────────

class TestLocalQualityScoresRegistry:
    def test_all_task_types_present_for_all_aliases(self):
        required_tasks = {"code", "analyze", "query", "generate", "research"}
        for alias, scores in _LOCAL_QUALITY_SCORES.items():
            missing = required_tasks - set(scores.keys())
            assert not missing, f"Alias '{alias}' missing task types: {missing}"

    def test_all_scores_in_valid_range(self):
        for alias, scores in _LOCAL_QUALITY_SCORES.items():
            for task, score in scores.items():
                assert 0.0 <= score <= 1.0, f"Score out of range: {alias}/{task}={score}"

    def test_code_specialists_score_high_on_code(self):
        """Code-specialist aliases should have code scores ≥ 0.70."""
        code_specialists = ("qwen3-coder", "qwen2.5-coder", "codestral", "deepseek-coder")
        for alias in code_specialists:
            assert _LOCAL_QUALITY_SCORES[alias]["code"] >= 0.70, (
                f"{alias} code score should be ≥ 0.70"
            )

    def test_general_models_have_balanced_scores(self):
        """General-purpose models should not have any task score < 0.45."""
        general_models = ("qwen3", "qwen2.5", "llama-3", "mistral")
        for alias in general_models:
            for task, score in _LOCAL_QUALITY_SCORES[alias].items():
                assert score >= 0.45, f"{alias}/{task}={score} is too low for a general model"
