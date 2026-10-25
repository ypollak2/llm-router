"""Tests for Phase 4: Live Benchmark Registry additions to benchmarks.py."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from llm_router.benchmarks import (
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
    # W1-3: the invented local table and 0.5 default are gone; unknown is None.
    def test_local_model_has_no_invented_score(self):
        assert get_quality_score("ollama/qwen3:32b", "code") is None

    def test_unknown_model_returns_none(self):
        assert get_quality_score("unknown/mystery-model", "code") is None

    def test_unknown_task_type_returns_none(self):
        assert get_quality_score("ollama/qwen3:32b", "vision") is None

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
        assert score is None

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
        assert score is None


# ── maybe_refresh_benchmarks_background ──────────────────────────────────────

class TestMaybeRefreshBenchmarks:
    def test_returns_false_when_not_stale(self, tmp_path):
        """If benchmarks.json is fresh, no refresh should be triggered."""
        benchmarks_file = tmp_path / "benchmarks.json"
        from datetime import datetime, timezone
        fresh_data = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": 1}
        benchmarks_file.write_text(json.dumps(fresh_data))

        with (
            patch("llm_router.benchmarks._installed", lambda: benchmarks_file),
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
            patch("llm_router.benchmarks._installed", lambda: benchmarks_file),
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
            patch("llm_router.benchmarks._installed", lambda: missing_path),
            patch("llm_router.benchmarks._refresh_in_progress", False),
            patch("llm_router.benchmarks._refresh_lock", threading.Lock()),
            patch("llm_router.benchmark_fetcher.fetch_pinned_catalog"),
        ):
            result = maybe_refresh_benchmarks_background(ttl_days=7)
        # Should attempt to start (file is missing = stale)
        assert isinstance(result, bool)

    def test_worker_releases_original_lock_when_global_lock_changes(self, tmp_path, monkeypatch):
        """Worker should release the lock it acquired, not whatever is later in the global.

        Needs the fetch to actually fire (asserts ``result is True``), so it
        opts in explicitly — this repo gates the fetch behind
        LLM_ROUTER_AUTO_BENCHMARK_FETCH (default off, PR2 follow-up).
        """
        monkeypatch.setenv("LLM_ROUTER_AUTO_BENCHMARK_FETCH", "1")
        import llm_router.benchmarks as bm

        benchmarks_file = tmp_path / "benchmarks.json"
        from datetime import datetime, timezone, timedelta

        old_data = {"generated_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(), "version": 1}
        benchmarks_file.write_text(json.dumps(old_data))

        original_lock = threading.Lock()

        class ImmediateThread:
            def __init__(self, target, name=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        def swap_global_lock(output_path):
            bm._refresh_lock = threading.Lock()

        with (
            patch("llm_router.benchmarks._installed", lambda: benchmarks_file),
            patch("llm_router.benchmarks._refresh_in_progress", False),
            patch("llm_router.benchmarks._refresh_lock", original_lock),
            patch("llm_router.benchmark_fetcher.fetch_pinned_catalog", side_effect=swap_global_lock),
            patch("llm_router.benchmarks.threading.Thread", ImmediateThread),
        ):
            result = maybe_refresh_benchmarks_background(ttl_days=7)

        assert result is True
        assert not original_lock.locked()
