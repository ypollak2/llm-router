"""Tests for v8.3.0 context-window cost optimizer."""

from __future__ import annotations

from llm_router.context_optimizer import (
    ContextOptimizationResult,
    _stage_recency,
    _stage_structural,
    _truncate_old_message,
    optimize_context,
)


# ── Stage 1: Structural ─────────────────────────────────────────────────────


class TestStageStructural:
    def test_collapses_whitespace(self):
        text = "line1\n\n\n\n\nline2\n\n\n\nline3"
        result = _stage_structural(text)
        assert result.count("\n\n\n") == 0
        assert "line1" in result
        assert "line2" in result

    def test_removes_code_comments(self):
        text = "before\n```python\n# this is a comment\nx = 1\n# another comment\ny = 2\n```\nafter"
        result = _stage_structural(text)
        assert "x = 1" in result
        assert "y = 2" in result
        # Comments should be removed
        assert "this is a comment" not in result

    def test_deduplicates_sections(self):
        block = "line a\nline b\nline c"
        text = f"{block}\nseparator\n{block}"
        result = _stage_structural(text)
        assert "repeated section removed" in result

    def test_preserves_short_text(self):
        text = "short text"
        result = _stage_structural(text)
        assert result == text


# ── Stage 2: Recency weighting ──────────────────────────────────────────────


class TestStageRecency:
    def test_keeps_few_messages_intact(self):
        text = "User: hello\nAssistant: hi\nUser: bye\nAssistant: goodbye"
        result = _stage_recency(text)
        assert result == text

    def test_truncates_old_messages(self):
        messages = []
        for i in range(6):
            messages.append(f"User: message {i} " + "x" * 300)
            messages.append(f"Assistant: response {i} " + "y" * 300)
        text = "\n".join(messages)
        result = _stage_recency(text)
        # Last 4 messages (2 exchanges) should be verbatim
        assert "response 5 " + "y" * 300 in result
        # Older messages should be truncated
        assert "..." in result

    def test_preserves_header(self):
        text = "[Recent conversation context]\nUser: hello\nAssistant: hi"
        result = _stage_recency(text)
        assert result.startswith("[Recent conversation context]")

    def test_drops_code_blocks_from_old(self):
        old_msg = "User: here is code\n```python\ndef foo():\n    pass\n```\nend"
        recent = "User: recent\nAssistant: answer"
        text = f"{old_msg}\nUser: middle\nAssistant: mid-answer\n{recent}\nUser: last\nAssistant: final"
        result = _stage_recency(text)
        # Code blocks from old messages should be removed
        assert "def foo" not in result or "last" in result


# ── Truncate helper ──────────────────────────────────────────────────────────


class TestTruncateOldMessage:
    def test_short_message_unchanged(self):
        result = _truncate_old_message("hello world")
        assert result == "hello world"

    def test_long_message_truncated(self):
        result = _truncate_old_message("x" * 500, max_chars=100)
        assert len(result) <= 104  # 100 + "..."
        assert result.endswith("...")

    def test_code_blocks_removed(self):
        text = "before\n```python\ndef foo():\n    pass\n```\nafter"
        result = _truncate_old_message(text)
        assert "def foo" not in result
        assert "[code removed]" in result


# ── Full pipeline ────────────────────────────────────────────────────────────


class TestOptimizeContext:
    def test_off_mode_passthrough(self):
        text = "hello " * 100
        result, metrics = optimize_context(text, mode="off")
        assert result == text
        assert metrics.skipped

    def test_free_model_skips(self):
        text = "hello " * 100
        result, metrics = optimize_context(text, mode="auto", is_free_model=True)
        assert result == text
        assert metrics.skipped

    def test_empty_text_no_crash(self):
        result, metrics = optimize_context("", mode="auto")
        assert result == ""
        assert metrics.skipped

    def test_auto_mode_compresses(self):
        # Build text with lots of whitespace and code comments
        text = (
            "line1\n\n\n\n\nline2\n\n\n\n"
            "```python\n# comment 1\nx = 1\n# comment 2\ny = 2\n```\n"
            "more text\n\n\n\nend"
        )
        result, metrics = optimize_context(text, mode="auto")
        assert metrics.compressed_tokens <= metrics.original_tokens
        assert len(metrics.stages_applied) > 0

    def test_metrics_accuracy(self):
        text = "a " * 200  # ~100 tokens
        result, metrics = optimize_context(text, mode="auto")
        assert metrics.original_tokens == len(text) // 4
        assert metrics.compressed_tokens == len(result) // 4
        assert metrics.tokens_saved == metrics.original_tokens - metrics.compressed_tokens

    def test_reduction_pct_calculation(self):
        metrics = ContextOptimizationResult(
            original_tokens=100,
            compressed_tokens=60,
            stages_applied=("structural",),
        )
        assert metrics.reduction_pct == 40.0
        assert metrics.tokens_saved == 40

    def test_zero_original_no_divide_by_zero(self):
        metrics = ContextOptimizationResult(
            original_tokens=0,
            compressed_tokens=0,
            stages_applied=(),
        )
        assert metrics.reduction_pct == 0.0
