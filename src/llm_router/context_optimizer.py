"""Context-window cost optimizer — compresses conversation context before LLM calls.

Reduces token usage by applying a 2-stage pipeline to session context:
  Stage 1: Structural compression (pure Python, instant)
    - Reuses compaction.py strategies: whitespace, comments, dedup, truncation
  Stage 2: Recency weighting (pure Python, instant)
    - Last 2 messages kept verbatim
    - Older messages: drop code blocks, truncate to 1-2 lines

Both stages are zero-latency (pure Python, no LLM calls). Free models
(Ollama, Codex) skip compression entirely since there's no cost benefit.

Controlled by LLM_ROUTER_CONTEXT_OPTIMIZER config:
  "off"  — pass context unchanged
  "auto" — Stage 1+2 (default)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from llm_router.compaction import (
    collapse_whitespace,
    dedup_sections,
    strip_code_comments,
    truncate_long_code,
)


@dataclass(frozen=True)
class ContextOptimizationResult:
    """Metrics from a context optimization pass."""

    original_tokens: int
    compressed_tokens: int
    stages_applied: tuple[str, ...]

    @property
    def tokens_saved(self) -> int:
        return self.original_tokens - self.compressed_tokens

    @property
    def reduction_pct(self) -> float:
        if self.original_tokens == 0:
            return 0.0
        return (self.tokens_saved / self.original_tokens) * 100

    @property
    def skipped(self) -> bool:
        return len(self.stages_applied) == 0


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


# ── Stage 1: Structural ─────────────────────────────────────────────────────


def _stage_structural(text: str) -> str:
    """Apply structural compression from compaction.py."""
    text = collapse_whitespace(text)
    text = strip_code_comments(text)
    text = dedup_sections(text)
    text = truncate_long_code(text)
    return text


# ── Stage 2: Recency weighting ──────────────────────────────────────────────

# Matches fenced code blocks
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


def _truncate_old_message(content: str, max_chars: int = 200) -> str:
    """Truncate an old message: drop code blocks, keep first max_chars."""
    # Remove code blocks entirely
    cleaned = _CODE_BLOCK_RE.sub("[code removed]", content)
    # Collapse to first max_chars
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "..."
    return cleaned


def _stage_recency(messages_text: str) -> str:
    """Apply recency weighting to conversation context.

    Parses the formatted context block and compresses older messages.
    Last 2 user/assistant exchanges are kept verbatim.
    Older messages have code blocks removed and are truncated.
    """
    lines = messages_text.split("\n")
    if not lines:
        return messages_text

    # Find message boundaries (lines starting with "User" or "Assistant")
    message_starts: list[int] = []
    for i, line in enumerate(lines):
        if line.startswith("User") or line.startswith("Assistant"):
            message_starts.append(i)

    if len(message_starts) <= 4:
        # 4 or fewer messages (2 exchanges) — keep everything
        return messages_text

    # Keep header lines (before first message)
    header_end = message_starts[0] if message_starts else 0
    result_lines = lines[:header_end]

    # Split into messages
    messages: list[str] = []
    for idx, start in enumerate(message_starts):
        end = message_starts[idx + 1] if idx + 1 < len(message_starts) else len(lines)
        messages.append("\n".join(lines[start:end]))

    # Keep last 4 lines (2 exchanges) verbatim, truncate older ones
    keep_verbatim = 4
    for i, msg in enumerate(messages):
        if i < len(messages) - keep_verbatim:
            result_lines.append(_truncate_old_message(msg))
        else:
            result_lines.append(msg)

    return "\n".join(result_lines)


# ── Pipeline ────────────────────────────────────────────────────────────────


def optimize_context(
    context_text: str,
    mode: str = "auto",
    is_free_model: bool = False,
) -> tuple[str, ContextOptimizationResult]:
    """Optimize context for token savings.

    Args:
        context_text: The assembled context string.
        mode: "off", "auto". Auto applies Stage 1+2.
        is_free_model: If True, skip compression (no cost benefit).

    Returns:
        Tuple of (optimized_text, result_metrics).
    """
    original_tokens = _estimate_tokens(context_text)

    if mode == "off" or is_free_model or not context_text.strip():
        return context_text, ContextOptimizationResult(
            original_tokens=original_tokens,
            compressed_tokens=original_tokens,
            stages_applied=(),
        )

    stages: list[str] = []
    current = context_text

    # Stage 1: Structural
    after_structural = _stage_structural(current)
    if after_structural != current:
        stages.append("structural")
        current = after_structural

    # Stage 2: Recency weighting
    after_recency = _stage_recency(current)
    if after_recency != current:
        stages.append("recency")
        current = after_recency

    compressed_tokens = _estimate_tokens(current)

    return current, ContextOptimizationResult(
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
        stages_applied=tuple(stages),
    )
