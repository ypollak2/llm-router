"""Tests for result_cache module — SQLite + FTS5 BM25 retrieval."""

import time

import pytest

from llm_router.result_cache import (
    CachedResult,
    _sanitize_fts_query,
    check_dedup,
    clear_cache,
    format_context,
    search_results,
    store_result,
)


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Redirect cache DBs to temp directory for test isolation."""
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)


class TestStoreResult:
    """Test storing results in the cache."""

    def test_store_basic(self):
        store_result(
            user_prompt="What is Python?",
            response="Python is a programming language.",
            task_type="query",
            complexity="simple",
            model_used="ollama/gemma4:latest",
        )
        # Should not raise

    def test_skip_empty_response(self):
        store_result(
            user_prompt="Hello",
            response="",
            task_type="query",
            complexity="simple",
            model_used="ollama/gemma4",
        )
        # Empty response should be skipped — no error
        results = search_results("Hello", "query")
        assert len(results) == 0

    def test_skip_short_response(self):
        store_result(
            user_prompt="Hello",
            response="Hi",  # < 20 chars
            task_type="query",
            complexity="simple",
            model_used="ollama/gemma4",
        )
        results = search_results("Hello", "query")
        assert len(results) == 0

    def test_dedup_prevents_duplicate(self):
        for _ in range(3):
            store_result(
                user_prompt="What is Python?",
                response="Python is a high-level programming language created by Guido.",
                task_type="query",
                complexity="simple",
                model_used="ollama/gemma4",
            )
        results = search_results("Python", "query")
        assert len(results) == 1


class TestSearchResults:
    """Test BM25 search retrieval."""

    def test_search_finds_relevant(self):
        store_result(
            user_prompt="How does async/await work in Python?",
            response="Async/await in Python uses coroutines and the event loop to handle concurrency.",
            task_type="query",
            complexity="moderate",
            model_used="openai/gpt-4o-mini",
        )
        store_result(
            user_prompt="What is Rust's ownership model?",
            response="Rust uses ownership with borrowing rules to manage memory safely without a garbage collector.",
            task_type="query",
            complexity="moderate",
            model_used="openai/gpt-4o-mini",
        )

        results = search_results("async await Python coroutines", "query")
        assert len(results) >= 1
        assert "async" in results[0].user_prompt.lower() or "async" in results[0].response.lower()

    def test_search_respects_budget(self):
        # Store a large response
        store_result(
            user_prompt="Explain everything about Python",
            response="Python is " + "a wonderful language. " * 500,  # ~3000 tokens
            task_type="query",
            complexity="complex",
            model_used="openai/gpt-4o",
        )
        # Search with tiny budget
        results = search_results("Python", "query", budget_tokens=50)
        # Should return 0 results (single result exceeds budget)
        assert len(results) == 0

    def test_search_returns_empty_for_no_match(self):
        store_result(
            user_prompt="What is Python?",
            response="Python is a programming language for general purpose use.",
            task_type="query",
            complexity="simple",
            model_used="ollama/gemma4",
        )
        results = search_results("quantum physics entanglement", "query")
        assert len(results) == 0

    def test_search_empty_db_returns_empty(self):
        results = search_results("anything", "query")
        assert results == []


class TestCheckDedup:
    """Test exact-match deduplication."""

    def test_dedup_finds_exact_match(self):
        store_result(
            user_prompt="What is the capital of France?",
            response="The capital of France is Paris, located on the Seine river.",
            task_type="query",
            complexity="simple",
            model_used="ollama/gemma4",
        )
        result = check_dedup("What is the capital of France?", "query")
        assert result is not None
        assert "Paris" in result.response

    def test_dedup_case_insensitive(self):
        store_result(
            user_prompt="What is Python?",
            response="Python is a high-level, interpreted programming language.",
            task_type="query",
            complexity="simple",
            model_used="ollama/gemma4",
        )
        # Same prompt, different case
        result = check_dedup("what is python?", "query")
        assert result is not None

    def test_dedup_returns_none_for_no_match(self):
        result = check_dedup("Never stored this question before", "query")
        assert result is None


class TestFormatContext:
    """Test context formatting for prompt injection."""

    def test_format_empty_list(self):
        assert format_context([]) == ""

    def test_format_single_result(self):
        results = [
            CachedResult(
                user_prompt="What is X?",
                response="X is a thing that does stuff reliably.",
                task_type="query",
                model_used="ollama/gemma4",
                timestamp=time.time(),
            )
        ]
        formatted = format_context(results)
        assert "[Relevant prior answers]" in formatted
        assert "Q: What is X?" in formatted
        assert "A: X is a thing" in formatted

    def test_format_respects_budget(self):
        results = [
            CachedResult(
                user_prompt=f"Question {i}?",
                response="A " * 200,  # ~100 tokens each
                task_type="query",
                model_used="m",
                timestamp=time.time(),
            )
            for i in range(20)
        ]
        formatted = format_context(results, max_tokens=200)
        # Should not include all 20 results
        assert formatted.count("Question") < 20


class TestSanitizeFtsQuery:
    """Test FTS5 query sanitization."""

    def test_removes_special_chars(self):
        result = _sanitize_fts_query("What's the (best) way?")
        assert "(" not in result
        assert ")" not in result
        assert "'" not in result

    def test_filters_short_words(self):
        result = _sanitize_fts_query("I am a Python developer")
        # "I", "am", "a" should be filtered (< 3 chars)
        assert "Python" in result
        assert "developer" in result

    def test_returns_empty_for_all_short(self):
        result = _sanitize_fts_query("I am a")
        assert result == ""

    def test_limits_to_10_terms(self):
        long_query = " ".join(f"word{i}" for i in range(20))
        result = _sanitize_fts_query(long_query)
        terms = result.split(" OR ")
        assert len(terms) <= 10


class TestClearCache:
    """Test cache clearing."""

    def test_clear_returns_count(self):
        store_result(
            user_prompt="Test question one for clearing",
            response="Test answer one that is long enough to store in cache.",
            task_type="query",
            complexity="simple",
            model_used="m",
        )
        store_result(
            user_prompt="Test question two for clearing",
            response="Test answer two that is long enough to store in cache.",
            task_type="query",
            complexity="simple",
            model_used="m",
        )
        deleted = clear_cache()
        assert deleted == 2

    def test_clear_empty_cache(self):
        deleted = clear_cache()
        assert deleted == 0
