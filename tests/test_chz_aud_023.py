"""Regression test for CHZ-AUD-023.

Privacy mode 'local' must block ALL session context — including Layer 1
persistent session summaries — from reaching external paid providers
(openai/gemini). Previously context.py Layer 1 called
format_session_summaries() unconditionally, bypassing the target_provider
gate that session_store.build_session_context already enforces.
"""

from __future__ import annotations

import pytest

from llm_router import context as ctx


@pytest.fixture
def _reset_buffer():
    # Ensure the in-process session buffer registry does not leak state
    # between tests (CHZ-AUD-B-04: buffers are now keyed by (project_id,
    # session_id) in a bounded registry, not a single module-level global).
    ctx._reset_session_buffers_for_test()
    yield
    ctx._reset_session_buffers_for_test()


async def _run(monkeypatch, *, mode: str, target_provider: str | None):
    # Layer 1: pretend a persisted summary exists.
    async def _fake_summaries(limit=3):
        return [{
            "summary": "did secret work",
            "session_start": "s",
            "session_end": "e",
            "message_count": 4,
            "task_types": ["query"],
        }]

    monkeypatch.setattr(ctx, "get_recent_session_summaries", _fake_summaries)
    # Privacy mode is resolved via session_store.get_mode().
    from llm_router import session_store
    monkeypatch.setattr(session_store, "get_mode", lambda: mode)
    # Keep Layer 2b from contributing / touching disk.
    monkeypatch.setattr(session_store, "resolve_session_id", lambda sid=None: None)

    return await ctx.build_context_messages(target_provider=target_provider)


@pytest.mark.asyncio
async def test_local_mode_blocks_summaries_to_openai(_reset_buffer, monkeypatch):
    msgs = await _run(monkeypatch, mode="local", target_provider="openai")
    blob = "".join(m["content"] for m in msgs)
    assert "[Previous session context]" not in blob
    assert "did secret work" not in blob


@pytest.mark.asyncio
async def test_local_mode_blocks_summaries_to_gemini(_reset_buffer, monkeypatch):
    msgs = await _run(monkeypatch, mode="local", target_provider="gemini")
    blob = "".join(m["content"] for m in msgs)
    assert "[Previous session context]" not in blob


@pytest.mark.asyncio
async def test_local_mode_allows_summaries_to_local_provider(_reset_buffer, monkeypatch):
    msgs = await _run(monkeypatch, mode="local", target_provider="ollama")
    blob = "".join(m["content"] for m in msgs)
    assert "did secret work" in blob


@pytest.mark.asyncio
async def test_all_mode_allows_summaries_to_openai(_reset_buffer, monkeypatch):
    msgs = await _run(monkeypatch, mode="all", target_provider="openai")
    blob = "".join(m["content"] for m in msgs)
    assert "did secret work" in blob


@pytest.mark.asyncio
async def test_off_mode_blocks_summaries_everywhere(_reset_buffer, monkeypatch):
    msgs = await _run(monkeypatch, mode="off", target_provider="ollama")
    blob = "".join(m["content"] for m in msgs)
    assert "[Previous session context]" not in blob
