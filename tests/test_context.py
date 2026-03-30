"""Tests for session context management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.context import (
    SessionBuffer,
    auto_summarize_session,
    build_context_messages,
    format_session_summaries,
    get_recent_session_summaries,
    get_session_buffer,
    save_session_summary,
)
from llm_router.types import LLMResponse


class TestSessionBuffer:
    def test_record_and_get_recent(self):
        buf = SessionBuffer(max_messages=5)
        buf.record("user", "Hello", task_type="query")
        buf.record("assistant", "Hi there", task_type="query")

        msgs = buf.get_recent(5)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"
        assert msgs[1].role == "assistant"

    def test_ring_buffer_eviction(self):
        buf = SessionBuffer(max_messages=3)
        for i in range(5):
            buf.record("user", f"msg-{i}")

        msgs = buf.get_recent(5)
        assert len(msgs) == 3
        assert msgs[0].content == "msg-2"
        assert msgs[2].content == "msg-4"

    def test_get_recent_limits(self):
        buf = SessionBuffer(max_messages=10)
        for i in range(10):
            buf.record("user", f"msg-{i}")

        msgs = buf.get_recent(3)
        assert len(msgs) == 3
        assert msgs[0].content == "msg-7"

    def test_truncates_long_content_on_record(self):
        buf = SessionBuffer()
        long_content = "x" * 5000
        buf.record("user", long_content)

        msgs = buf.get_recent(1)
        assert len(msgs[0].content) == 2000

    def test_clear(self):
        buf = SessionBuffer()
        buf.record("user", "hello")
        buf.clear()
        assert buf.message_count == 0

    def test_format_for_injection_empty(self):
        buf = SessionBuffer()
        assert buf.format_for_injection() == ""

    def test_format_for_injection(self):
        buf = SessionBuffer()
        buf.record("user", "What is Python?", task_type="query")
        buf.record("assistant", "Python is a programming language.", task_type="query")

        result = buf.format_for_injection()
        assert "[Recent conversation context]" in result
        assert "User (query): What is Python?" in result
        assert "Assistant (query): Python is a programming language." in result

    def test_format_truncates_long_messages(self):
        buf = SessionBuffer()
        buf.record("user", "x" * 2000)

        result = buf.format_for_injection()
        # Content in injection is capped at 500 chars + "..."
        assert "..." in result


class TestSessionBufferSingleton:
    def test_returns_same_instance(self):
        buf1 = get_session_buffer()
        buf2 = get_session_buffer()
        assert buf1 is buf2


class TestFormatSessionSummaries:
    def test_empty(self):
        assert format_session_summaries([]) == ""

    def test_formats_summaries(self):
        summaries = [
            {
                "summary": "Worked on auth module",
                "session_start": "2026-03-29T10:00:00",
                "session_end": "2026-03-29T11:00:00",
                "message_count": 5,
                "task_types": ["code", "analyze"],
            },
            {
                "summary": "Research on caching strategies",
                "session_start": "2026-03-30T09:00:00",
                "session_end": "2026-03-30T10:00:00",
                "message_count": 3,
                "task_types": ["research"],
            },
        ]

        result = format_session_summaries(summaries)
        assert "[Previous session context]" in result
        # Input is newest-first (as returned by DB), reversed() makes oldest first
        # So "Research" (index 1, older after reverse) appears before "auth" (index 0, newer after reverse)
        # Actually: reversed([auth, research]) = [research, auth]
        assert result.index("Research on caching") < result.index("auth module")


class TestPersistentSummaries:
    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "test.db"
        with patch("llm_router.context._get_db_path", return_value=path):
            yield path

    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, db_path):
        with patch("llm_router.context._get_db_path", return_value=db_path):
            await save_session_summary(
                summary="Built context injection feature",
                message_count=8,
                task_types=["code", "query"],
            )

            summaries = await get_recent_session_summaries(limit=5)
            assert len(summaries) == 1
            assert summaries[0]["summary"] == "Built context injection feature"
            assert summaries[0]["message_count"] == 8
            assert summaries[0]["task_types"] == ["code", "query"]

    @pytest.mark.asyncio
    async def test_respects_limit(self, db_path):
        with patch("llm_router.context._get_db_path", return_value=db_path):
            for i in range(5):
                await save_session_summary(f"Session {i}", i, ["query"])

            summaries = await get_recent_session_summaries(limit=2)
            assert len(summaries) == 2
            # Newest first
            assert summaries[0]["summary"] == "Session 4"

    @pytest.mark.asyncio
    async def test_no_db_returns_empty(self, tmp_path):
        missing = tmp_path / "nonexistent" / "db.sqlite"
        with patch("llm_router.context._get_db_path", return_value=missing):
            summaries = await get_recent_session_summaries()
            assert summaries == []


class TestBuildContextMessages:
    @pytest.mark.asyncio
    async def test_no_context_returns_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages()
            assert msgs == []

    @pytest.mark.asyncio
    async def test_with_session_buffer_only(self, tmp_path):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer()
            buf.record("user", "What is FastAPI?", task_type="query")
            buf.record("assistant", "FastAPI is a web framework.", task_type="query")

            msgs = await build_context_messages()
            assert len(msgs) == 1
            assert msgs[0]["role"] == "system"
            assert "FastAPI" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_with_caller_context(self, tmp_path):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages(
                caller_context="Working on the llm-router project, adding context injection",
            )
            assert len(msgs) == 1
            assert "llm-router" in msgs[0]["content"]
            assert "[Additional context]" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_combined_context_order(self, tmp_path):
        db_path = tmp_path / "test.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            # Save a previous session summary
            await save_session_summary("Worked on auth", 3, ["code"])

            # Add current session messages
            buf = get_session_buffer()
            buf.record("user", "Now working on context", task_type="code")

            # Build context
            msgs = await build_context_messages(caller_context="Extra info")
            assert len(msgs) == 1
            content = msgs[0]["content"]

            # Previous sessions should come before current session
            prev_idx = content.index("Previous session")
            curr_idx = content.index("Recent conversation")
            extra_idx = content.index("Additional context")

            assert prev_idx < curr_idx < extra_idx

    @pytest.mark.asyncio
    async def test_respects_token_budget(self, tmp_path):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            # Fill buffer with lots of content
            buf = get_session_buffer()
            for i in range(10):
                buf.record("user", f"Message {i}: {'x' * 500}", task_type="query")

            msgs = await build_context_messages(max_context_tokens=100)
            assert len(msgs) == 1
            # Should be truncated to roughly 100*4=400 chars
            assert len(msgs[0]["content"]) <= 500


class TestAutoSummarize:
    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "test.db"
        with patch("llm_router.context._get_db_path", return_value=path):
            yield path

    @pytest.mark.asyncio
    async def test_skips_short_sessions(self, db_path):
        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer()
            buf.record("user", "hello")
            result = await auto_summarize_session(min_messages=3)
            assert result is None

    @pytest.mark.asyncio
    async def test_summarizes_via_llm(self, db_path):
        mock_response = LLMResponse(
            content="User asked about FastAPI and received an explanation of the framework.",
            model="gemini/gemini-2.5-flash",
            input_tokens=50,
            output_tokens=20,
            cost_usd=0.0001,
            latency_ms=200.0,
            provider="gemini",
        )

        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer()
            buf.record("user", "What is FastAPI?", task_type="query")
            buf.record("assistant", "FastAPI is a modern web framework.", task_type="query")
            buf.record("user", "How do I install it?", task_type="query")
            buf.record("assistant", "Run pip install fastapi.", task_type="query")

            with patch("llm_router.router.route_and_call", new_callable=AsyncMock, return_value=mock_response):
                summary = await auto_summarize_session(min_messages=3)

            assert summary is not None
            assert "FastAPI" in summary

            # Verify it was persisted
            summaries = await get_recent_session_summaries()
            assert len(summaries) == 1
            assert summaries[0]["summary"] == summary
            assert summaries[0]["task_types"] == ["query"]

    @pytest.mark.asyncio
    async def test_falls_back_on_llm_failure(self, db_path):
        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer()
            buf.record("user", "Build a REST API", task_type="code")
            buf.record("assistant", "Here's the code...", task_type="code")
            buf.record("user", "Add auth", task_type="code")

            with patch("llm_router.router.route_and_call", new_callable=AsyncMock, side_effect=RuntimeError("No models")):
                summary = await auto_summarize_session(min_messages=3)

            assert summary is not None
            assert "Topics:" in summary
            assert "Build a REST API" in summary

    @pytest.mark.asyncio
    async def test_collects_task_types(self, db_path):
        mock_response = LLMResponse(
            content="Mixed session with research and code tasks.",
            model="gemini/gemini-2.5-flash",
            input_tokens=50, output_tokens=20,
            cost_usd=0.0001, latency_ms=200.0, provider="gemini",
        )

        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer()
            buf.record("user", "Research caching", task_type="research")
            buf.record("assistant", "Redis is popular", task_type="research")
            buf.record("user", "Write cache code", task_type="code")
            buf.record("assistant", "Here's the code", task_type="code")

            with patch("llm_router.router.route_and_call", new_callable=AsyncMock, return_value=mock_response):
                await auto_summarize_session(min_messages=3)

            summaries = await get_recent_session_summaries()
            assert set(summaries[0]["task_types"]) == {"code", "research"}
