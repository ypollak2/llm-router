"""Tests for session context management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.context import (
    SessionBuffer,
    _reset_session_buffers_for_test,
    _resolve_context_identity,
    _strip_injected_context,
    auto_summarize_session,
    build_context_messages,
    format_session_summaries,
    get_recent_session_summaries,
    get_session_buffer,
    save_session_summary,
)
from llm_router.types import LLMResponse


def _ambient_identity():
    """(project_id, session_id) that build_context_messages()/auto_summarize_session()
    resolve to internally when called with no explicit identity args (CHZ-AUD-B-04).
    Test bodies that pre-populate a SessionBuffer directly via get_session_buffer()
    must use this same key so the buffer they write to is the one those functions
    read from — get_session_buffer() no longer has a zero-arg process-wide singleton.
    """
    return _resolve_context_identity(None, None)


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
        _reset_session_buffers_for_test()
        buf1 = get_session_buffer("proj-a", "sess-1")
        buf2 = get_session_buffer("proj-a", "sess-1")
        assert buf1 is buf2

    def test_different_keys_get_different_instances(self):
        """CHZ-AUD-B-04: the registry must not collapse distinct
        (project_id, session_id) identities into one shared buffer."""
        _reset_session_buffers_for_test()
        buf_a = get_session_buffer("proj-a", "sess-1")
        buf_b = get_session_buffer("proj-b", "sess-1")
        buf_c = get_session_buffer("proj-a", "sess-2")
        assert buf_a is not buf_b
        assert buf_a is not buf_c
        assert buf_b is not buf_c


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
    @pytest.fixture
    def reset_session_buffer(self, tmp_path_factory, monkeypatch):
        """Reset the global session buffer AND isolate ~/.llm-router before each test.

        build_context_messages() also reads the durable session-context
        accumulator via llm_router.session_store, which lives under ~/.llm-router. Without
        isolating HOME the test read the developer's LIVE session accumulator
        (populated by the active llm_router hooks) and non-deterministically saw
        context where the test expects none. Pointing HOME at a fresh temp dir
        makes the accumulator empty and the test hermetic.
        """
        import llm_router.context as context_module
        context_module._reset_session_buffers_for_test()
        monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("chz-home")))
        yield
        context_module._reset_session_buffers_for_test()

    @pytest.mark.asyncio
    async def test_no_context_returns_empty(self, tmp_path, reset_session_buffer):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages()
            assert msgs == []

    @pytest.mark.asyncio
    async def test_with_session_buffer_only(self, tmp_path, reset_session_buffer):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            pid, sid = _ambient_identity()
            buf = get_session_buffer(pid, sid)
            buf.record("user", "What is FastAPI?", task_type="query")
            buf.record("assistant", "FastAPI is a web framework.", task_type="query")

            msgs = await build_context_messages()
            assert len(msgs) == 1
            assert msgs[0]["role"] == "system"
            assert "FastAPI" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_with_caller_context(self, tmp_path, reset_session_buffer):
        # reset_session_buffer isolates HOME so build_context_messages() does not
        # read the developer's LIVE ~/.llm-router session accumulator (its siblings
        # above already require this fixture; this test omitted it and so read
        # real captured session context, making it flaky under a long session).
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages(
                caller_context="Working on the llm_router project, adding context injection",
            )
            assert len(msgs) == 1
            assert "llm_router" in msgs[0]["content"]
            assert "[Additional context]" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_combined_context_order(self, tmp_path, reset_session_buffer):
        db_path = tmp_path / "test.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            # Save a previous session summary
            await save_session_summary("Worked on auth", 3, ["code"])

            # Add current session messages
            buf = get_session_buffer(*_ambient_identity())
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
    async def test_respects_token_budget(self, tmp_path, reset_session_buffer):
        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            # Fill buffer with lots of content
            buf = get_session_buffer(*_ambient_identity())
            for i in range(10):
                buf.record("user", f"Message {i}: {'x' * 500}", task_type="query")

            msgs = await build_context_messages(max_context_tokens=100)
            assert len(msgs) == 1
            # Should be truncated to roughly 100*4=400 chars
            assert len(msgs[0]["content"]) <= 500


class TestBuildContextMessagesLayer2b:
    """Layer 2b: the Session Context Accumulator's durable, cross-process
    JSONL event store folded into build_context_messages()'s MCP path.

    All tests here monkeypatch llm_router.session_store's functions directly
    (never touching a real ~/.llm-router) — the store's own behavior (privacy
    mode gating, dedup, truncation) is covered independently in
    tests/test_session_store.py. This class only verifies build_context_messages
    correctly wires session_id/target_provider through to session_store, folds
    the result into the assembled context in the documented position (after
    layer 2's in-process buffer, before layer 3's caller-supplied context),
    and fails open at every step (import, resolve_session_id, get_config,
    build_session_context all independently raising).
    """

    @pytest.fixture
    def reset_session_buffer(self):
        import llm_router.context as context_module
        context_module._reset_session_buffers_for_test()
        yield
        context_module._reset_session_buffers_for_test()

    @pytest.mark.asyncio
    async def test_no_resolved_session_id_contributes_nothing(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        resolve_calls = []
        monkeypatch.setattr(
            real_session_store,
            "resolve_session_id",
            lambda explicit=None: resolve_calls.append(explicit) or None,
        )
        build_calls = []
        monkeypatch.setattr(
            real_session_store,
            "build_session_context",
            lambda *a, **kw: build_calls.append((a, kw)) or "SHOULD NOT APPEAR",
        )

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages(session_id="explicit-sid")

        # Behavior unchanged from before layer 2b existed: no other context ->
        # empty list, and build_session_context must never be called since
        # resolve_session_id returned falsy.
        assert msgs == []
        assert resolve_calls == ["explicit-sid"]
        assert build_calls == []

    @pytest.mark.asyncio
    async def test_resolved_session_id_pulls_in_durable_context(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        monkeypatch.setattr(
            real_session_store, "resolve_session_id", lambda explicit=None: "sess-durable"
        )
        monkeypatch.setattr(
            real_session_store,
            "build_session_context",
            lambda *a, **kw: "durable event from a prior tool call",
        )

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages()

        assert len(msgs) == 1
        assert "durable event from a prior tool call" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_session_id_and_target_provider_forwarded_to_session_store(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        resolve_calls = []
        monkeypatch.setattr(
            real_session_store,
            "resolve_session_id",
            lambda explicit=None: resolve_calls.append(explicit) or "sess-1",
        )
        build_calls = []

        def _fake_build_session_context(sid, **kw):
            build_calls.append((sid, kw))
            return "durable content"

        monkeypatch.setattr(
            real_session_store, "build_session_context", _fake_build_session_context
        )

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            await build_context_messages(
                session_id="explicit-sid",
                caller_context="looking for docs on FastAPI",
                target_provider="openai",
            )

        assert resolve_calls == ["explicit-sid"]
        assert len(build_calls) == 1
        sid_arg, kwargs = build_calls[0]
        assert sid_arg == "sess-1"  # the *resolved* id, not the raw explicit param
        assert kwargs["query"] == "looking for docs on FastAPI"
        assert kwargs["target_provider"] == "openai"
        assert kwargs["max_tokens"] == 1500  # RouterConfig default

    @pytest.mark.asyncio
    async def test_mcp_budget_from_config_is_forwarded_as_max_tokens(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.config as real_config
        import llm_router.session_store as real_session_store
        from types import SimpleNamespace

        monkeypatch.setattr(
            real_session_store, "resolve_session_id", lambda explicit=None: "sess-1"
        )
        build_calls = []

        def _fake_build_session_context(sid, **kw):
            build_calls.append(kw)
            return "durable content"

        monkeypatch.setattr(
            real_session_store, "build_session_context", _fake_build_session_context
        )
        monkeypatch.setattr(
            real_config, "get_config", lambda: SimpleNamespace(session_context_max_tokens_mcp=42)
        )

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            await build_context_messages()

        assert build_calls[0]["max_tokens"] == 42

    @pytest.mark.asyncio
    async def test_get_config_raising_falls_back_to_default_budget(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.config as real_config
        import llm_router.session_store as real_session_store

        monkeypatch.setattr(
            real_session_store, "resolve_session_id", lambda explicit=None: "sess-1"
        )
        build_calls = []

        def _fake_build_session_context(sid, **kw):
            build_calls.append(kw)
            return "durable content"

        monkeypatch.setattr(
            real_session_store, "build_session_context", _fake_build_session_context
        )

        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(real_config, "get_config", _raise)

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages()

        # get_config() raising is caught by its own inner try/except, falling
        # back to the documented default of 1500 — layer 2b must still work.
        assert build_calls[0]["max_tokens"] == 1500
        assert len(msgs) == 1
        assert "durable content" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_empty_durable_context_contributes_nothing(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        monkeypatch.setattr(
            real_session_store, "resolve_session_id", lambda explicit=None: "sess-1"
        )
        monkeypatch.setattr(
            real_session_store, "build_session_context", lambda *a, **kw: ""
        )

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages()

        assert msgs == []

    @pytest.mark.asyncio
    async def test_ordering_layer2b_before_layer3_caller_context(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        monkeypatch.setattr(
            real_session_store, "resolve_session_id", lambda explicit=None: "sess-1"
        )
        monkeypatch.setattr(
            real_session_store,
            "build_session_context",
            lambda *a, **kw: "DURABLE_MARKER content",
        )

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages(caller_context="CALLER_MARKER info")

        assert len(msgs) == 1
        content = msgs[0]["content"]
        assert content.index("DURABLE_MARKER") < content.index("CALLER_MARKER")

    # ── fail-open ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_resolve_session_id_raising_is_fail_open(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        def _raise(explicit=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(real_session_store, "resolve_session_id", _raise)

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            # Other layers (none active here) still resolve fine; the whole
            # call must not raise despite layer 2b's resolve_session_id
            # blowing up.
            msgs = await build_context_messages(caller_context="still works")

        assert len(msgs) == 1
        assert "still works" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_build_session_context_raising_is_fail_open(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        import llm_router.session_store as real_session_store

        monkeypatch.setattr(
            real_session_store, "resolve_session_id", lambda explicit=None: "sess-1"
        )

        def _raise(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(real_session_store, "build_session_context", _raise)

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages(caller_context="still works")

        assert len(msgs) == 1
        assert "still works" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_session_store_import_failure_is_fail_open(
        self, tmp_path, reset_session_buffer, monkeypatch
    ):
        """Simulate the `from llm_router import session_store` import line itself
        failing (e.g. a packaging/circular-import problem) by making the
        already-imported module object unavailable under its expected name.
        The surrounding bare `except Exception` must still make layer 2b a
        harmless no-op rather than breaking the whole function."""
        import sys

        monkeypatch.setitem(sys.modules, "llm_router.session_store", None)

        db_path = tmp_path / "empty.db"
        with patch("llm_router.context._get_db_path", return_value=db_path):
            msgs = await build_context_messages(caller_context="still works")

        assert len(msgs) == 1
        assert "still works" in msgs[0]["content"]


class TestAutoSummarize:
    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "test.db"
        with patch("llm_router.context._get_db_path", return_value=path):
            yield path

    @pytest.fixture
    def reset_session_buffer(self):
        """Reset the global session buffer registry before each test."""
        import llm_router.context as context_module
        context_module._reset_session_buffers_for_test()
        yield
        context_module._reset_session_buffers_for_test()

    @pytest.mark.asyncio
    async def test_skips_short_sessions(self, db_path, reset_session_buffer):
        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer(*_ambient_identity())
            buf.record("user", "hello")
            result = await auto_summarize_session(min_messages=3)
            assert result is None

    @pytest.mark.asyncio
    async def test_summarizes_via_llm(self, db_path, reset_session_buffer):
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
            buf = get_session_buffer(*_ambient_identity())
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
            buf = get_session_buffer(*_ambient_identity())
            buf.record("user", "Build a REST API", task_type="code")
            buf.record("assistant", "Here's the code...", task_type="code")
            buf.record("user", "Add auth", task_type="code")

            with patch("llm_router.router.route_and_call", new_callable=AsyncMock, side_effect=RuntimeError("No models")):
                summary = await auto_summarize_session(min_messages=3)

            assert summary is not None
            assert "Topics:" in summary
            assert "Build a REST API" in summary

    @pytest.mark.asyncio
    async def test_collects_task_types(self, db_path, reset_session_buffer):
        mock_response = LLMResponse(
            content="Mixed session with research and code tasks.",
            model="gemini/gemini-2.5-flash",
            input_tokens=50, output_tokens=20,
            cost_usd=0.0001, latency_ms=200.0, provider="gemini",
        )

        with patch("llm_router.context._get_db_path", return_value=db_path):
            buf = get_session_buffer(*_ambient_identity())
            buf.record("user", "Research caching", task_type="research")
            buf.record("assistant", "Redis is popular", task_type="research")
            buf.record("user", "Write cache code", task_type="code")
            buf.record("assistant", "Here's the code", task_type="code")

            with patch("llm_router.router.route_and_call", new_callable=AsyncMock, return_value=mock_response):
                await auto_summarize_session(min_messages=3)

            summaries = await get_recent_session_summaries()
            assert set(summaries[0]["task_types"]) == {"code", "research"}


class TestInjectedContextStripping:
    """Regression tests for the self-poisoning bug: SessionBuffer (always-on, NOT
    gated by LLM_ROUTER_OKF) recorded every routed prompt/response verbatim, including
    OKF-injected <knowledge_context> blocks — which then kept replaying into
    unrelated future prompts indefinitely, even after OKF was disabled, because
    the buffer no longer knows where the content originally came from.
    """

    def test_strips_okf_knowledge_context_block(self):
        poisoned = (
            "<knowledge_context>\n"
            "## [SourceFile] setup.py\n"
            "fake fabricated content about a nonexistent API\n"
            "</knowledge_context>\n\n"
            "What is the real task API?"
        )
        clean = _strip_injected_context(poisoned)
        assert "knowledge_context" not in clean
        assert "fabricated" not in clean
        assert clean == "What is the real task API?"

    def test_strips_own_previously_injected_blocks(self):
        # a message that itself already carries THIS module's own injected
        # markers (e.g. captured before this fix existed) must not compound
        poisoned = (
            "[Recent conversation context]\n"
            "User (analyze): some old exchange\n"
            "Assistant (analyze): some old reply\n\n"
            "New unrelated question"
        )
        clean = _strip_injected_context(poisoned)
        assert "Recent conversation context" not in clean
        assert clean == "New unrelated question"

    def test_leaves_clean_text_untouched(self):
        assert _strip_injected_context("just a normal prompt") == "just a normal prompt"

    def test_session_buffer_record_strips_before_storing(self):
        """The exact bug, reproduced: recording an OKF-poisoned prompt must not
        let the poisoned block survive in the buffer to be replayed later."""
        buf = SessionBuffer(max_messages=5)
        poisoned_prompt = (
            "<knowledge_context>\n"
            "## [ModelCapability] codex-cli.md\n"
            "The Codex CLI provides access to GPT-3 and GPT-4 models\n"
            "</knowledge_context>\n\n"
            "Say the word \"test\" and nothing else."
        )
        buf.record("user", poisoned_prompt, task_type="query")
        stored = buf.get_recent(1)[0].content
        assert "knowledge_context" not in stored
        assert "codex-cli.md" not in stored
        assert stored == 'Say the word "test" and nothing else.'

        # and it must not appear when replayed into a future prompt either
        injected = buf.format_for_injection(n=1)
        assert "knowledge_context" not in injected
        assert "codex-cli.md" not in injected

    @pytest.mark.asyncio
    async def test_auto_summarize_never_sees_injected_content(self):
        """Defense in depth: even if a poisoned message somehow reached the
        buffer (e.g. from a process still running an older build), the
        summarizer LLM must never be handed the raw injected block."""
        import time as _time

        import llm_router.context as context_module
        from llm_router.context import SessionMessage
        context_module._reset_session_buffers_for_test()

        buf = get_session_buffer(*_ambient_identity())
        # append directly via the dataclass, bypassing record()'s own guard,
        # to simulate content buffered by an older build before this fix
        buf._buffer.append(SessionMessage(
            role="user",
            content="<knowledge_context>\n## [SourceFile] fake.py\nbogus\n"
                   "</knowledge_context>\n\nreal question",
            timestamp=_time.time(),
            task_type="query",
        ))
        buf.record("assistant", "real answer", task_type="query")
        buf.record("user", "another real question", task_type="query")

        captured_prompt = {}

        async def _capture(*args, **kwargs):
            captured_prompt["prompt"] = args[1] if len(args) > 1 else kwargs.get("prompt")
            return LLMResponse(
                content="summary", model="m", input_tokens=1, output_tokens=1,
                cost_usd=0.0, latency_ms=1.0, provider="p",
            )

        with patch("llm_router.context._get_db_path",
                   return_value=None) as _dbp:
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as td:
                _dbp.return_value = Path(td) / "t.db"
                with patch("llm_router.router.route_and_call", side_effect=_capture):
                    await auto_summarize_session(min_messages=3)

        assert "knowledge_context" not in captured_prompt.get("prompt", "")
        assert "fake.py" not in captured_prompt.get("prompt", "")

    @pytest.mark.asyncio
    async def test_retroactive_strip_on_read_of_already_poisoned_db_row(self):
        """A row written before this fix existed (by any older process) must
        still come out clean — this protects users whose SQLite already has
        poisoned summaries, without needing a data migration."""
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "t.db"
            with patch("llm_router.context._get_db_path", return_value=db_path):
                # write a poisoned row directly, bypassing save_session_summary,
                # to simulate data persisted before this fix landed
                from llm_router.context import _ensure_session_table
                _ensure_session_table(db_path)
                poisoned_summary = (
                    "<knowledge_context>\n## [SourceFile] x.py\nbogus\n"
                    "</knowledge_context>\n\nWorked on real feature X."
                )
                conn = sqlite3.connect(str(db_path))
                conn.execute(
                    """INSERT INTO session_summaries
                       (session_start, session_end, summary, message_count, task_types)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("2026-01-01", "2026-01-01", poisoned_summary, 4, '["code"]'),
                )
                conn.commit()
                conn.close()

                summaries = await get_recent_session_summaries()
                assert len(summaries) == 1
                assert "knowledge_context" not in summaries[0]["summary"]
                assert "bogus" not in summaries[0]["summary"]
                assert "Worked on real feature X." in summaries[0]["summary"]
