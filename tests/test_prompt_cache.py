"""Tests for Anthropic prompt caching — inject_cache_control."""

from __future__ import annotations

import pytest

from llm_router.prompt_cache import inject_cache_control


# ── Helpers ─────────────────────────────────────────────────────────────────

def _long(n_tokens: int = 1100) -> str:
    """Return a string that exceeds the given token estimate (4 chars/token)."""
    return "x" * (n_tokens * 4)


def _short(n_tokens: int = 100) -> str:
    return "x" * (n_tokens * 4)


def _is_cached(msg: dict) -> bool:
    """Return True if a message has a cache_control block injected."""
    content = msg.get("content", "")
    return (
        isinstance(content, list)
        and len(content) == 1
        and content[0].get("cache_control") == {"type": "ephemeral"}
    )


def _plain_text(msg: dict) -> str | None:
    """Return the plain text from a message (cached or plain)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        return content[0].get("text")
    return None


# ── Non-Anthropic models ─────────────────────────────────────────────────────

def test_non_anthropic_model_unchanged():
    messages = [
        {"role": "system", "content": _long()},
        {"role": "user", "content": "hello"},
    ]
    result = inject_cache_control(messages, "openai/gpt-4o")
    assert result is messages  # original returned unchanged


def test_ollama_model_unchanged():
    messages = [{"role": "user", "content": _long()}]
    result = inject_cache_control(messages, "ollama/llama3.2")
    assert result is messages


def test_empty_messages_unchanged():
    result = inject_cache_control([], "anthropic/claude-sonnet-4-6")
    assert result == []


# ── System message caching ───────────────────────────────────────────────────

def test_long_system_message_gets_cached():
    messages = [
        {"role": "system", "content": _long(1100)},
        {"role": "user", "content": "hi"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert _is_cached(result[0])
    assert _plain_text(result[0]) == messages[0]["content"]


def test_short_system_message_not_cached():
    messages = [
        {"role": "system", "content": _short(50)},
        {"role": "user", "content": "hi"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert not _is_cached(result[0])


def test_system_message_text_preserved():
    text = _long(1200)
    messages = [
        {"role": "system", "content": text},
        {"role": "user", "content": "q"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert _plain_text(result[0]) == text


# ── Context message caching ──────────────────────────────────────────────────

def test_long_context_before_user_gets_cached():
    """The last message before the final user turn should be cached when context is long."""
    big = _long(1200)
    messages = [
        {"role": "system", "content": _short(50)},   # system too short
        {"role": "user", "content": big},             # earlier user msg (context)
        {"role": "assistant", "content": big},        # last context msg — should be cached
        {"role": "user", "content": "current prompt"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    # system not cached (too short)
    assert not _is_cached(result[0])
    # last context message (index 2) cached
    assert _is_cached(result[2])
    # current user prompt NOT cached
    assert not _is_cached(result[3])


def test_no_context_before_user_no_second_breakpoint():
    """Only current user turn — no second breakpoint."""
    messages = [
        {"role": "system", "content": _long(1200)},
        {"role": "user", "content": "just this"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert _is_cached(result[0])   # system cached
    assert not _is_cached(result[1])  # user not cached


def test_short_context_not_cached():
    """Accumulation of short context messages below threshold → no breakpoint."""
    messages = [
        {"role": "system", "content": _short(50)},
        {"role": "user", "content": _short(50)},
        {"role": "assistant", "content": _short(50)},
        {"role": "user", "content": "current"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert not any(_is_cached(m) for m in result)


# ── Current user prompt is never cached ─────────────────────────────────────

def test_current_user_prompt_never_cached():
    big = _long(2000)
    messages = [
        {"role": "system", "content": big},
        {"role": "user", "content": big},  # long current prompt
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    # System gets cached
    assert _is_cached(result[0])
    # Current user turn — never cached (it IS the last user turn)
    assert not _is_cached(result[1])


# ── Both breakpoints injected ─────────────────────────────────────────────────

def test_both_breakpoints_injected():
    big = _long(1200)
    messages = [
        {"role": "system", "content": big},
        {"role": "user", "content": big},
        {"role": "assistant", "content": big},
        {"role": "user", "content": "current"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert _is_cached(result[0])   # system
    assert _is_cached(result[2])   # last context msg
    assert not _is_cached(result[3])  # current user


# ── Caller list not mutated ───────────────────────────────────────────────────

def test_original_messages_not_mutated():
    """inject_cache_control must not modify the caller's list."""
    big = _long(1200)
    original = [
        {"role": "system", "content": big},
        {"role": "user", "content": "q"},
    ]
    original_copy = [dict(m) for m in original]
    inject_cache_control(original, "anthropic/claude-sonnet-4-6")
    assert original == original_copy


# ── Custom min_tokens threshold ───────────────────────────────────────────────

def test_custom_min_tokens_higher_threshold():
    """With min_tokens=2048, a 1100-token system message should NOT be cached."""
    messages = [
        {"role": "system", "content": _long(1100)},
        {"role": "user", "content": "q"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6", min_tokens=2048)
    assert not _is_cached(result[0])


def test_custom_min_tokens_lower_threshold():
    """With min_tokens=100, even a short message gets cached."""
    messages = [
        {"role": "system", "content": _long(200)},
        {"role": "user", "content": "q"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6", min_tokens=100)
    assert _is_cached(result[0])


# ── Already-structured content passthrough ───────────────────────────────────

def test_already_cached_content_not_double_wrapped():
    """If content is already a list, we still handle it (extract text and re-wrap)."""
    big_text = _long(1200)
    messages = [
        {
            "role": "system",
            "content": [{"type": "text", "text": big_text}],
        },
        {"role": "user", "content": "q"},
    ]
    result = inject_cache_control(messages, "anthropic/claude-sonnet-4-6")
    assert _is_cached(result[0])
    assert _plain_text(result[0]) == big_text
