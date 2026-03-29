"""Tests for structural prompt compaction."""

from __future__ import annotations

import pytest

from llm_router.compaction import (
    collapse_stack_traces,
    collapse_whitespace,
    compact_structural,
    dedup_sections,
    estimate_tokens,
    strip_code_comments,
    truncate_long_code,
)


class TestEstimateTokens:
    def test_basic(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 100) == 25

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestCollapseWhitespace:
    def test_excessive_newlines(self):
        text = "a\n\n\n\nb"
        assert collapse_whitespace(text) == "a\n\nb"

    def test_trailing_whitespace(self):
        text = "hello   \nworld  "
        assert collapse_whitespace(text) == "hello\nworld"

    def test_interior_spaces(self):
        text = "a     b     c"
        assert collapse_whitespace(text) == "a b c"

    def test_preserves_indent(self):
        text = "    indented line"
        result = collapse_whitespace(text)
        assert result.startswith("    ")

    def test_two_newlines_preserved(self):
        text = "a\n\nb"
        assert collapse_whitespace(text) == "a\n\nb"


class TestStripCodeComments:
    def test_removes_hash_comments(self):
        text = "```python\n# this is a comment\nx = 1\n```"
        result = strip_code_comments(text)
        assert "# this is a comment" not in result
        assert "x = 1" in result

    def test_removes_slash_comments(self):
        text = "```js\n// this is a comment\nconst x = 1;\n```"
        result = strip_code_comments(text)
        assert "// this is a comment" not in result
        assert "const x = 1;" in result

    def test_preserves_urls(self):
        text = "```python\n# http://example.com/path\nx = 1\n```"
        result = strip_code_comments(text)
        assert "http://example.com/path" in result

    def test_preserves_shebangs(self):
        text = "```bash\n#!/bin/bash\necho hello\n```"
        result = strip_code_comments(text)
        assert "#!/bin/bash" in result

    def test_preserves_strings(self):
        text = '```python\nx = "hello # world"\n```'
        result = strip_code_comments(text)
        assert '"hello # world"' in result

    def test_outside_code_block_untouched(self):
        text = "# This is a markdown heading\nSome text"
        result = strip_code_comments(text)
        assert result == text


class TestDedupSections:
    def test_removes_duplicate_block(self):
        block = "line1\nline2\nline3"
        text = f"{block}\nother\n{block}"
        result = dedup_sections(text)
        assert result.count("line1") == 1
        assert "[... repeated section removed ...]" in result

    def test_no_change_if_no_duplicates(self):
        text = "a\nb\nc\nd\ne\nf"
        assert dedup_sections(text) == text

    def test_short_text_unchanged(self):
        text = "a\nb\nc"
        assert dedup_sections(text) == text


class TestTruncateLongCode:
    def test_truncates_long_block(self):
        lines = [f"line {i}" for i in range(60)]
        text = "```python\n" + "\n".join(lines) + "\n```"
        result = truncate_long_code(text)
        assert "[... 30 lines truncated ...]" in result
        assert "line 0" in result
        assert "line 19" in result
        assert "line 59" in result
        # A middle line should be gone
        assert "line 30" not in result

    def test_short_block_unchanged(self):
        lines = [f"line {i}" for i in range(10)]
        text = "```\n" + "\n".join(lines) + "\n```"
        result = truncate_long_code(text)
        assert result == text


class TestCollapseStackTraces:
    def test_python_traceback(self):
        frames = []
        for i in range(15):
            frames.append(f'  File "mod{i}.py", line {i}, in func{i}')
            frames.append(f"    code_line_{i}()")
        text = "Traceback (most recent call last):\n" + "\n".join(frames) + "\nValueError: bad"
        result = collapse_stack_traces(text)
        assert "[... 9 frames truncated ...]" in result
        assert "mod0.py" in result
        assert "mod14.py" in result
        assert "ValueError: bad" in result

    def test_short_traceback_unchanged(self):
        frames = []
        for i in range(5):
            frames.append(f'  File "mod{i}.py", line {i}, in func{i}')
        text = "Traceback:\n" + "\n".join(frames)
        result = collapse_stack_traces(text)
        assert "[... " not in result


class TestCompactStructural:
    @pytest.mark.asyncio
    async def test_below_threshold_unchanged(self):
        text = "short text"
        result_text, result = await compact_structural(text, threshold=4000)
        assert result_text == text
        assert result.tokens_saved_estimate == 0
        assert result.strategies_applied == ()

    @pytest.mark.asyncio
    async def test_above_threshold_compacts(self):
        # Create text above 4000 tokens (~16000 chars)
        text = ("x" * 80 + "\n") * 250  # ~20000 chars with lots of whitespace
        text += "\n\n\n\n\n"  # extra newlines to trigger collapse
        result_text, result = await compact_structural(text, threshold=100)
        assert result.original_length > result.compacted_length
        assert result.tokens_saved_estimate > 0
        assert len(result.strategies_applied) > 0

    @pytest.mark.asyncio
    async def test_result_is_frozen(self):
        _, result = await compact_structural("x" * 1000, threshold=10)
        with pytest.raises(AttributeError):
            result.original_length = 0  # type: ignore[misc]
