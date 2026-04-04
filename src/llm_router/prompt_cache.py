"""Anthropic prompt caching — auto-inject cache_control breakpoints.

When routing to an Anthropic model, long stable context (system prompts,
conversation history) can be cached at the API level for up to 5 minutes.
Cached tokens cost ~10% of full price, saving up to 90% on repeated context.

This module transforms a standard ``messages`` list into Anthropic's
structured content format with ``cache_control`` breakpoints injected at
the most cache-effective positions:

  1. The system message (if >= min token threshold) — most stable, highest hit rate.
  2. The last context message before the current user turn — caches conversation
     history that changes slowly across calls.

Anthropic allows up to 4 breakpoints per request. We use at most 2 to leave
room for callers that add their own (e.g. tool-use chains).

Minimum cacheable sizes (Anthropic requirement):
  - Claude 3.5 Sonnet / Opus: 1024 tokens
  - Claude 3 Haiku:           2048 tokens (higher due to smaller model overhead)
  We default to 1024 to cover all modern Claude models.

References:
  https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""

from __future__ import annotations

import logging

log = logging.getLogger("llm_router")

# Chars-per-token estimate — Anthropic's tokenizer averages ~4 chars/token.
_CHARS_PER_TOKEN = 4


def _is_anthropic_model(model: str) -> bool:
    """Return True if this is an Anthropic model string."""
    return model.startswith("anthropic/")


def _char_token_estimate(text: str) -> int:
    """Rough token count from character length (4 chars ≈ 1 token)."""
    return len(text) // _CHARS_PER_TOKEN


def _to_cached_block(text: str) -> list[dict]:
    """Convert plain text to an Anthropic content block with cache_control."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _content_text(msg: dict) -> str:
    """Extract plain text from a message regardless of content format."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def inject_cache_control(
    messages: list[dict],
    model: str,
    min_tokens: int = 1024,
) -> list[dict]:
    """Inject Anthropic cache_control breakpoints into the messages list.

    Modifies the message list in-place at up to 2 positions:
      - System message, if its token estimate >= min_tokens.
      - Last non-user message before the final user turn (context history),
        if the cumulative pre-user-turn content >= min_tokens.

    For non-Anthropic models, returns the original list unchanged.

    Args:
        messages: OpenAI-format message list (dicts with ``role`` and ``content``).
        model: LiteLLM model string (e.g. ``"anthropic/claude-sonnet-4-6"``).
        min_tokens: Minimum token estimate to apply caching. Anthropic requires
            at least 1024 tokens for Sonnet/Opus and 2048 for Haiku. Default 1024
            covers all modern Claude models.

    Returns:
        New list with cache_control injected at effective breakpoints.
        Returns the original list unchanged for non-Anthropic models.
    """
    if not _is_anthropic_model(model) or not messages:
        return messages

    min_chars = min_tokens * _CHARS_PER_TOKEN
    result = [dict(m) for m in messages]  # shallow copy — don't mutate caller's list
    injected = 0

    # ── Breakpoint 1: system message ────────────────────────────────────────
    if result[0]["role"] == "system":
        text = _content_text(result[0])
        if len(text) >= min_chars:
            result[0] = {**result[0], "content": _to_cached_block(text)}
            injected += 1
            log.debug(
                "prompt_cache: cached system message (%d chars, ~%d tokens)",
                len(text), _char_token_estimate(text),
            )

    # ── Breakpoint 2: last context message before the current user turn ─────
    # Find the index of the final user message (current prompt).
    last_user_idx = next(
        (i for i in range(len(result) - 1, -1, -1) if result[i]["role"] == "user"),
        -1,
    )
    if injected < 2 and last_user_idx > 0:
        # Accumulate char length of everything before the current user turn
        # (excluding system message already handled above).
        start = 1 if result[0]["role"] == "system" else 0
        context_slice = result[start:last_user_idx]
        total_context_chars = sum(len(_content_text(m)) for m in context_slice)

        if total_context_chars >= min_chars:
            # Mark the message just before the current user turn.
            target_idx = last_user_idx - 1
            text = _content_text(result[target_idx])
            result[target_idx] = {**result[target_idx], "content": _to_cached_block(text)}
            injected += 1
            log.debug(
                "prompt_cache: cached context tail at index %d (%d chars, ~%d tokens)",
                target_idx, len(text), _char_token_estimate(text),
            )

    if injected:
        log.info("prompt_cache: %d breakpoint(s) injected for %s", injected, model)

    return result
