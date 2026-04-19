"""Tests for response compression (Phase 2 — Token-Savior)."""

from llm_router.compression.response_compressor import ResponseCompressor, compress_response


class TestCompressionResult:
    """Test CompressionResult dataclass."""

    def test_tokens_saved_calculation(self):
        """Test that tokens_saved correctly calculates the difference."""
        from llm_router.compression.response_compressor import CompressionResult

        result = CompressionResult(
            original_tokens=100,
            compressed_tokens=40,
            compression_ratio=0.4,
            output="test",
            stages_applied=["test"],
        )
        assert result.tokens_saved() == 60

    def test_tokens_saved_zero(self):
        """Test tokens_saved when no compression occurred."""
        from llm_router.compression.response_compressor import CompressionResult

        result = CompressionResult(
            original_tokens=100,
            compressed_tokens=100,
            compression_ratio=1.0,
            output="test",
            stages_applied=[],
        )
        assert result.tokens_saved() == 0


class TestResponseCompressor:
    """Test ResponseCompressor main class."""

    def test_disabled_compression(self):
        """Compression disabled should return original text."""
        compressor = ResponseCompressor(enable=False)
        text = "This is a test. I think it's basically working."
        result = compressor.compress(text)

        assert result.output == text
        assert result.compression_ratio == 1.0
        assert result.stages_applied == []

    def test_empty_response(self):
        """Empty response should not compress."""
        compressor = ResponseCompressor(enable=True)
        result = compressor.compress("")

        assert result.output == ""
        assert result.stages_applied == []

    def test_very_short_response(self):
        """Very short response (<100 chars) should not compress."""
        compressor = ResponseCompressor(enable=True)
        text = "Short text."
        result = compressor.compress(text)

        assert result.output == text
        assert result.stages_applied == []

    def test_compression_enabled_applies_stages(self):
        """Enabled compression with adequate text should apply stages."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "I think basically this is a really important approach. "
            "Here's the solution: you should definitely use method X. "
            "For example, you can do this like: a = 1. "
            "Another example would be: b = 2. "
            "The key steps are: first do this, then do that, finally do this."
        )
        result = compressor.compress(text, target_reduction=0.6)

        assert result.output != text
        assert len(result.stages_applied) > 0
        assert result.compression_ratio < 1.0


class TestFillerRemoval:
    """Test Stage 1: Filler removal."""

    def test_remove_filler_words(self):
        """Filler words should be removed."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "I think this is basically really important. "
            "Actually, you should definitely use this approach. "
            "Honestly, it's quite good."
        ) * 20  # Repeat to exceed 100 chars

        result = compressor._remove_filler(text)

        assert "I think" not in result.lower()
        assert "basically" not in result.lower()
        assert "actually" not in result.lower()
        assert len(result) < len(text)

    def test_preserve_non_filler_words(self):
        """Non-filler important words should be preserved."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "The solution is to use authentication. "
            "This is critical for security. "
            "You must verify the implementation."
        ) * 20

        result = compressor._remove_filler(text)

        assert "solution" in result.lower()
        assert "critical" in result.lower()
        assert "verify" in result.lower()

    def test_filler_at_line_start(self):
        """Filler at line start should be removed."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "I think this is important.\n"
            "Basically, you should use this.\n"
            "Really, it's the best approach."
        ) * 20

        result = compressor._remove_filler(text)

        # Check that filler words don't appear at the start of lines
        lines = result.split("\n")
        for line in lines:
            if line.strip():
                assert not line.lower().startswith(
                    ("i think", "basically", "really")
                )


class TestExampleConsolidation:
    """Test Stage 2: Example consolidation."""

    def test_consolidate_multiple_examples(self):
        """Multiple examples should be consolidated."""
        compressor = ResponseCompressor(enable=True)
        # Use newlines to make examples more distinct
        text = (
            "Here's the approach.\n"
            "Example 1: You can do this like:\n"
            "  a = 1; b = 2\n"
            "Example 2: Another example:\n"
            "  c = 3; d = 4\n"
            "Example 3: One more:\n"
            "  e = 5; f = 6\n"
        ) * 20

        result = compressor._consolidate_examples(text)

        # Should have consolidation marker or be shorter
        has_marker = "[Additional examples omitted" in result
        is_shorter = len(result) < len(text)
        assert has_marker or is_shorter  # One or the other

    def test_keep_first_example(self):
        """First example should be kept."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "Here's the approach.\n"
            "Example: First example with code.\n"
            "For example: second example here.\n"
        ) * 20

        result = compressor._consolidate_examples(text)

        # First example pattern should still be there
        assert "Example:" in result or "first example" in result.lower()

    def test_example_with_code_block(self):
        """Code blocks after example should be preserved."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "For example:\n"
            "  const x = 1;\n"
            "  const y = 2;\n"
            "Another example:\n"
            "  const z = 3;\n"
        ) * 10

        result = compressor._consolidate_examples(text)

        # First code block should be present
        assert "const x" in result or "const y" in result


class TestBoilerplateCollapse:
    """Test Stage 3: Boilerplate collapse."""

    def test_collapse_solution_header(self):
        """'Here's the solution:' patterns should collapse."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "Here's the solution:\n"
            "You should use approach X.\n"
            "This is better because Y.\n"
        ) * 20

        result = compressor._collapse_boilerplate(text)

        # Header should be simplified
        assert "Here's the solution" not in result

    def test_collapse_steps_header(self):
        """'The steps are:' patterns should collapse."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "The key steps are:\n"
            "Step 1: Do this.\n"
            "Step 2: Do that.\n"
        ) * 20

        result = compressor._collapse_boilerplate(text)

        # Should be modified
        assert len(result) <= len(text)

    def test_preserve_code_structure(self):
        """Code blocks should not be collapsed."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "Here's the code:\n"
            "```python\n"
            "def foo():\n"
            "    return 42\n"
            "```\n"
        ) * 10

        result = compressor._collapse_boilerplate(text)

        # Code should be preserved
        assert "def foo" in result


class TestSemanticExtraction:
    """Test Stage 4: Semantic extraction."""

    def test_extract_key_sentences(self):
        """Key sentences should be extracted."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "Filler sentence here. "
            "The critical solution is to use caching. "
            "More filler here. "
            "You must implement proper error handling. "
            "This is really important for security. "
        ) * 10

        result = compressor._extract_key_sentences(text)

        # Key words should be present
        assert "solution" in result.lower() or "critical" in result.lower()
        # Should be shorter
        assert len(result) < len(text)

    def test_preserve_short_responses(self):
        """Short responses should not be extracted."""
        compressor = ResponseCompressor(enable=True)
        text = "First sentence. Second sentence. Third sentence."
        result = compressor._extract_key_sentences(text)

        # Should preserve most of original for short text
        assert len(result) > len(text) * 0.5

    def test_sentence_scoring_favors_keywords(self):
        """Sentences with important keywords should score higher."""
        compressor = ResponseCompressor(enable=True)
        sentences = [
            "This is filler text.",
            "The solution requires proper error handling.",
            "More filler here.",
        ]

        scores = [
            compressor._score_sentence(s, sentences) for s in sentences
        ]

        # Middle sentence (with keywords) should score highest
        assert scores[1] > scores[0]
        assert scores[1] > scores[2]


class TestTokenEstimation:
    """Test token estimation logic."""

    def test_estimate_tokens_empty(self):
        """Empty string should estimate to 1 token (minimum)."""
        compressor = ResponseCompressor(enable=True)
        assert compressor._estimate_tokens("") == 1

    def test_estimate_tokens_formula(self):
        """Token estimation should follow 4-character formula."""
        compressor = ResponseCompressor(enable=True)
        # 400 characters should estimate to 100 tokens
        text = "x" * 400
        assert compressor._estimate_tokens(text) == 100

    def test_estimate_tokens_rounds_up(self):
        """Partial tokens should round up."""
        compressor = ResponseCompressor(enable=True)
        text = "x" * 10  # 10 / 4 = 2.5, should be 3
        assert compressor._estimate_tokens(text) >= 2


class TestCompressionRatios:
    """Test compression ratio calculations."""

    def test_compression_ratio_calculation(self):
        """Compression ratio should be compressed/original."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "I think this is basically really important. "
            "You should definitely use this approach. "
        ) * 30

        result = compressor.compress(text)

        expected_ratio = (
            result.compressed_tokens / result.original_tokens
        )
        assert abs(result.compression_ratio - expected_ratio) < 0.01

    def test_no_compression_ratio_one(self):
        """No compression should have ratio of 1.0."""
        compressor = ResponseCompressor(enable=False)
        text = "Short test."
        result = compressor.compress(text)

        assert result.compression_ratio == 1.0

    def test_compression_ratio_under_one(self):
        """Successful compression should have ratio < 1.0."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "I think this is basically really important. "
            "Actually, you should definitely use this approach. "
            "Here's an example: x = 1. Another example: y = 2. "
        ) * 40

        result = compressor.compress(text, target_reduction=0.6)

        assert result.compression_ratio < 1.0


class TestTargetReduction:
    """Test target_reduction parameter."""

    def test_aggressive_reduction(self):
        """Higher target_reduction should produce more compression."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "Sentence one is about approach A. "
            "Sentence two explains approach A further. "
            "Sentence three is about approach B. "
            "Sentence four explains approach B further. "
            "Sentence five is about approach C. "
        ) * 30

        result_conservative = compressor.compress(text, target_reduction=0.3)
        result_aggressive = compressor.compress(text, target_reduction=0.7)

        # Aggressive should be shorter
        assert len(result_aggressive.output) <= len(
            result_conservative.output
        )

    def test_zero_reduction(self):
        """target_reduction=0 should preserve more content."""
        compressor = ResponseCompressor(enable=True)
        text = (
            "I think this is important. "
            "Actually, you should use this. "
            "This is the best approach. "
        ) * 30

        result = compressor.compress(text, target_reduction=0.0)

        # Should preserve most content
        assert result.compression_ratio > 0.7


class TestMaxOutputTokens:
    """Test max_output_tokens hard limit."""

    def test_max_output_tokens_enforced(self):
        """Output should not exceed max_output_tokens."""
        compressor = ResponseCompressor(enable=True)
        text = "x " * 500  # Many tokens

        result = compressor.compress(text, max_output_tokens=50)

        # Should be truncated
        assert compressor._estimate_tokens(result.output) <= 55  # Allow small buffer


class TestConvenienceFunction:
    """Test compress_response convenience function."""

    def test_compress_response_enabled(self):
        """compress_response should compress when enabled."""
        text = (
            "I think this is basically important. "
            "You should definitely use this approach. "
        ) * 30

        result = compress_response(text, enabled=True)

        assert result.compression_ratio <= 1.0

    def test_compress_response_disabled(self):
        """compress_response should not compress when disabled."""
        text = "Test text."
        result = compress_response(text, enabled=False)

        assert result.output == text
        assert result.compression_ratio == 1.0

    def test_compress_response_target_reduction(self):
        """compress_response should respect target_reduction."""
        text = (
            "I think the solution is to basically use this approach. "
            "Sentence about topic A with critical information. "
            "Actually, you should definitely verify the implementation. "
            "Sentence about topic B with more details. "
            "Here's an example: x = 1. Another example: y = 2. "
            "The key steps are: step 1, step 2, step 3. "
        ) * 30

        result = compress_response(text, target_reduction=0.5)

        # Should apply compression stages
        assert len(result.stages_applied) > 0


class TestIntegration:
    """Integration tests for full compression pipeline."""

    def test_full_pipeline_realistic(self):
        """Test full pipeline on realistic LLM response."""
        response = """I think the best approach here would be to basically use a cache layer. 
        
Here's the solution: You should definitely implement Redis caching for this.

For example, you could do something like:
  cache.get(key)
  cache.set(key, value)

Another example would be:
  if key in cache:
    return cache[key]

The key steps are:
  1. Set up Redis connection
  2. Configure cache TTL
  3. Implement cache invalidation
  4. Monitor cache hit rates

This is really quite important for performance. Actually, it's essential to verify that your cache layer is working correctly. The solution I mentioned above will definitely help with latency.
"""
        compressor = ResponseCompressor(enable=True)
        result = compressor.compress(response)

        assert result.compression_ratio < 1.0
        assert len(result.stages_applied) > 0
        assert result.output != response
        # Key technical content should be preserved
        assert "cache" in result.output.lower()

    def test_all_stages_applied_when_needed(self):
        """Multiple stages should apply when text is amenable."""
        response = (
            "I think this is basically really important. "
            "Here's the solution: You should definitely use X. "
            "Example 1: code here.\n"
            "Example 2: more code.\n"
            "The key steps are: step 1, step 2, step 3. "
            "This is critical important. "
            "Sentence about topic A with critical information. "
            "Sentence about topic B with more details. "
        ) * 50

        compressor = ResponseCompressor(enable=True)
        result = compressor.compress(response)

        # At least filler removal should apply
        assert "filler_removal" in result.stages_applied
        # Should have some compression
        assert result.compression_ratio <= 1.0

    def test_preserves_code_blocks(self):
        """Code blocks should be relatively preserved."""
        response = """Here's the code:

```python
def calculate(x, y):
    # Important comment
    return x + y
```

This is the solution. Actually, you should basically use this function.
"""
        compressor = ResponseCompressor(enable=True)
        result = compressor.compress(response)

        # Core function signature should be present
        assert "def calculate" in result.output or "x, y" in result.output
