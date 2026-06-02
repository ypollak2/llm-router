"""Plan 07 Phase 2 — defensive helpers for provider response handling.

This module exists for cross-provider quirks the router has to absorb in
the hot path. Each helper is intentionally tiny and pure so it can be
unit-tested in isolation and composed into the request flow without
introducing new failure modes.

Currently in scope:
- D.1 `extract_content` — fall back to `message.reasoning` when a thinking
  model leaves `message.content` empty (DeepSeek R1, qwen3 reasoning,
  o1-family). See Plan 07 §D.1 and tests/test_inference_robustness.py.
- D.2 `safe_max_tokens` — cap requested max_tokens at the model's known
  output limit. Prevents OpenAI silent truncation and Anthropic 400-errors
  when callers pass oversized values. See Plan 07 §D.2.

Deferred until a concrete need surfaces:
- D.3 empty-response → success-flag coherence (lives in response_validation.py)
- D.4 provider-quirk registry (Protocol)
"""

from __future__ import annotations

from typing import Any

# Matches existing ``config.default_max_tokens``. Kept here as well so
# ``safe_max_tokens`` can be exercised standalone (e.g. in tests) without
# pulling in the full config layer.
DEFAULT_MAX_TOKENS = 4096

# Per-model output token caps. Conservative — only models we're confident
# about. Unknown models bypass the cap so we never accidentally
# over-restrict a newer or better model that's not yet in this table.
#
# When adding a new model, prefer the published "max output tokens" figure
# from the vendor docs. If uncertain, omit (unknown models bypass).
_MODEL_OUTPUT_CAPS: dict[str, int] = {
    # ── Anthropic Claude 4.x (8192 output) ──
    "anthropic/claude-opus-4-6": 8192,
    "anthropic/claude-sonnet-4-6": 8192,
    "anthropic/claude-haiku-4-5-20251001": 8192,
    # ── OpenAI GPT-4o family (16384 output) ──
    "openai/gpt-4o": 16384,
    "openai/gpt-4o-mini": 16384,
    # ── Google Gemini 2.5 series (8192 output) ──
    "gemini/gemini-2.5-pro": 8192,
    "gemini/gemini-2.5-flash": 8192,
    "gemini/gemini-2.5-flash-lite": 8192,
    # ── Groq Llama 3.3 70B (8192 output) ──
    "groq/llama-3.3-70b-versatile": 8192,
    # ── DeepSeek V3+ chat & reasoner (8192 output) ──
    "deepseek/deepseek-chat": 8192,
    "deepseek/deepseek-reasoner": 8192,
    # ── Mistral Small Latest (8192 output) ──
    "mistral/mistral-small-latest": 8192,
    # ── Cohere Command R+ (4096 output) ──
    "cohere/command-r-plus": 4096,
    # ── xAI Grok 3 (8192 output) ──
    "xai/grok-3": 8192,
}


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


def safe_max_tokens(requested: int | None, model: str) -> int:
    """Return a max_tokens value safe to send to ``model``.

    Two-part safety:
      1. Falsy ``requested`` (None, 0, negative) falls back to
         ``DEFAULT_MAX_TOKENS`` so callers always get a sensible default.
      2. If ``model`` is in the per-model caps table, the result is capped
         at the model's published output limit. Unknown models bypass the
         cap and receive ``requested`` unchanged — preferable to silently
         over-restricting a model we haven't catalogued yet.

    This is the single repair point for two Plan 07 §D.2 failure modes:
      - OpenAI silently truncates responses at the model's context limit
        when ``max_tokens`` exceeds it. The cap surfaces the limit
        explicitly so we know what the model will actually emit.
      - Anthropic raises a 400 BadRequestError when ``max_tokens`` exceeds
        the model's limit. The cap prevents the hard failure altogether.

    Args:
        requested: Caller-supplied max_tokens, or None for default.
        model: Provider-prefixed model identifier (e.g. ``"openai/gpt-4o"``).

    Returns:
        An integer that is always > 0 and never exceeds the model's
        published output cap when one is known.
    """
    if not requested or requested <= 0:
        requested = DEFAULT_MAX_TOKENS
    cap = _MODEL_OUTPUT_CAPS.get(model)
    if cap is None:
        return requested
    return min(requested, cap)
