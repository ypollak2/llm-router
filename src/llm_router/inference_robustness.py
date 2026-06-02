"""Plan 07 Phase 2 — defensive helpers for provider response handling.

This module exists for cross-provider quirks the router has to absorb in
the hot path. Each helper is intentionally tiny and pure so it can be
unit-tested in isolation and composed into the request flow without
introducing new failure modes.

Currently in scope:
- D.1 `extract_content` — fall back to `message.reasoning` when a thinking
  model leaves `message.content` empty (DeepSeek R1, qwen3 reasoning,
  o1-family). See Plan 07 §D.1 and tests/test_inference_robustness.py.

Deferred until a concrete need surfaces:
- D.2 max_tokens cap table
- D.3 empty-response → success-flag coherence (lives in response_validation.py)
- D.4 provider-quirk registry (Protocol)
"""

from __future__ import annotations

from typing import Any


def extract_content(message: Any) -> str:
    """Return the textual answer from a LiteLLM-style chat message.

    Falls back to ``message.reasoning`` when ``message.content`` is missing,
    None, or whitespace-only. This is the single point of repair for the
    thinking-model bug described in Plan 07 §D.1.

    Both attributes are accessed defensively because real provider responses
    vary: OpenAI's message has no ``reasoning`` at all; DeepSeek R1 sets
    ``content=None`` and packs the answer into ``reasoning``; older
    completions sometimes carry an empty string instead of None.

    Args:
        message: A chat-completion message object (LiteLLM or compatible).

    Returns:
        The best-effort answer string, possibly empty if neither attribute
        carries usable text.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    reasoning = getattr(message, "reasoning", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return ""
