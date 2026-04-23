"""Tests for ResponseRouter — explanation routing through cheaper models."""

import pytest

from llm_router.response_router import ResponseRouter, ResponseSection


class TestResponseParsing:
    """Test response parsing into critical vs explanation sections."""

    def setup_method(self):
        """Setup router for each test."""
        self.router = ResponseRouter()

    def test_parse_response_identifies_critical_sections(self):
        """Should preserve critical sections (code, file paths, commands)."""
        response = """
Here's the fix for the issue.

```python
def my_func():
    return 42
```

This code does something important.
"""
        parsed = self.router.parse_response(response)

        # Should identify code block as critical
        critical_sections = [s for t, s in parsed.sections if t == ResponseSection.CRITICAL]
        assert len(critical_sections) > 0
        assert any("```" in s for s in critical_sections)

    def test_parse_response_identifies_explanations(self):
        """Should identify explanation paragraphs for routing."""
        response = """
This is an important explanation about the architecture.

The system works by routing requests through multiple models.

Another explanation paragraph here.
"""
        parsed = self.router.parse_response(response)

        # Should identify multiple explanation sections
        assert parsed.explanation_count >= 2
        assert parsed.explanation_tokens > 0

    def test_parse_response_preserves_file_paths(self):
        """Should treat file paths as critical (don't route)."""
        response = """
I've updated the file /path/to/file.py with the following changes.

This is an explanation of what the changes do.
"""
        parsed = self.router.parse_response(response)

        # Should identify file path paragraph as critical
        critical_sections = [s for t, s in parsed.sections if t == ResponseSection.CRITICAL]
        assert any("/path/to/file.py" in s for s in critical_sections)

    def test_parse_response_preserves_tool_invocations(self):
        """Should preserve tool invocations (Read, Edit, Bash, etc)."""
        response = """
Let me read the configuration file.

- Read the file to understand the current setup
- Edit it to add the new feature
- Run tests to verify

This explains why we're doing this.
"""
        parsed = self.router.parse_response(response)

        # Tool invocation bullet should be critical
        critical_sections = [s for t, s in parsed.sections if t == ResponseSection.CRITICAL]
        assert any("Read" in s or "Edit" in s for s in critical_sections)

    def test_parse_response_skips_tiny_responses(self):
        """Should skip routing for responses with too few explanation tokens."""
        response = "OK, done."

        parsed = self.router.parse_response(response)
        assert parsed.explanation_tokens < self.router.MIN_TOKENS

    def test_parse_response_markdown_headers(self):
        """Should preserve markdown headers as critical."""
        response = """
## Implementation Plan

Here's my detailed plan for implementing this feature.

### Step 1

Do this first.

### Step 2

Do this second.
"""
        parsed = self.router.parse_response(response)

        # Headers should be critical
        critical_sections = [s for t, s in parsed.sections if t == ResponseSection.CRITICAL]
        assert any("##" in s for s in critical_sections)


class TestResponseRouting:
    """Test response routing functionality."""

    def setup_method(self):
        """Setup router for each test."""
        self.router = ResponseRouter()

    @pytest.mark.asyncio
    async def test_route_response_disabled(self):
        """Should return original response when routing is disabled."""
        router = ResponseRouter()
        router.ENABLED = False

        original = "This is an explanation that would normally be routed."
        result = await router.route_response(original)

        assert result == original

    @pytest.mark.asyncio
    async def test_route_response_preserves_critical_sections(self):
        """Should never route critical sections (code, commands, etc)."""
        response = """
Here's the implementation:

```python
def solution():
    return True
```

This code is important.
"""
        parsed = self.router.parse_response(response)

        # Verify code block is marked critical
        critical_sections = [s for t, s in parsed.sections if t == ResponseSection.CRITICAL]
        assert any("```" in s for s in critical_sections)

    def test_split_routed_explanations(self):
        """Should split routed text back into expected number of chunks."""
        routed = "First explanation\n\nSecond explanation\n\nThird explanation"
        result = self.router._split_routed_explanations(routed, 3)

        assert len(result) == 3
        assert "First" in result[0]

    def test_reassemble_response(self):
        """Should reassemble response with routed explanations in correct order."""
        sections = [
            (ResponseSection.CRITICAL, "Header section"),
            (ResponseSection.EXPLANATION, "Original explanation 1"),
            (ResponseSection.CRITICAL, "Code block"),
            (ResponseSection.EXPLANATION, "Original explanation 2"),
        ]
        routed_explanations = [
            "Routed explanation 1",
            "Routed explanation 2",
        ]

        result = self.router._reassemble_response(sections, routed_explanations)

        # Should contain routed versions
        assert "Routed explanation 1" in result
        assert "Routed explanation 2" in result

        # Should preserve critical sections
        assert "Header section" in result
        assert "Code block" in result

        # Should NOT contain original explanations (replaced by routed)
        assert "Original explanation 1" not in result
        assert "Original explanation 2" not in result


class TestTokenEstimation:
    """Test token estimation for routing decisions."""

    def setup_method(self):
        """Setup router for each test."""
        self.router = ResponseRouter()

    def test_explanation_token_estimation(self):
        """Should estimate explanation tokens approximately correctly."""
        # 400 chars ≈ 100 tokens
        explanation = "x" * 400
        parsed = self.router.parse_response(explanation)

        # Should estimate ~100 tokens (400 / 4)
        assert parsed.explanation_tokens == 100

    def test_threshold_filtering(self):
        """Should only route explanations above token threshold."""
        short = "Short explanation"
        router = ResponseRouter()
        router.MIN_TOKENS = 300

        parsed = router.parse_response(short)
        assert parsed.explanation_tokens < router.MIN_TOKENS
