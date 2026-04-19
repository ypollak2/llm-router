"""Response compression for LLM outputs.

Compresses verbose LLM responses while preserving key information.
Target: 60-75% reduction through a 4-stage pipeline:
  1. Filler removal (5-10%): "I think", "basically", articles
  2. Example consolidation (15-20%): keep 1 example, tag others
  3. Boilerplate collapse (20-30%): explanations → bullet points
  4. Semantic extraction (10-20%): keep key sentences only
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class CompressionResult:
    """Result of compressing an LLM response."""

    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    output: str
    stages_applied: list[str]

    def tokens_saved(self) -> int:
        """Calculate tokens saved."""
        return self.original_tokens - self.compressed_tokens


class ResponseCompressor:
    """Compress LLM responses for token efficiency.

    Uses a 4-stage pipeline:
    1. Filler removal: Strip unnecessary words
    2. Example consolidation: Keep 1 example, consolidate others
    3. Boilerplate collapse: Convert explanations to bullets
    4. Semantic extraction: Keep only key sentences
    """

    # Filler words and patterns to remove (Stage 1)
    FILLER_WORDS = {
        "i think", "i believe", "i would say", "in my opinion",
        "basically", "actually", "really", "quite", "pretty",
        "definitely", "absolutely", "certainly", "surely",
        "essentially", "ultimately", "generally", "typically",
        "arguably", "frankly", "honestly", "obviously",
    }

    # Common verbose phrases (Stage 1)
    VERBOSE_PHRASES = [
        (r"\b(?:a |an |the )+", ""),  # Remove articles before lists
        (r"\b(?:would |could |should |might |may )+", ""),  # Remove modals in imperative
        (r"(?:in other words|that is|i\.e\.|e\.g\.)[,:]?\s*", ""),  # Remove rephrasing
    ]

    # Boilerplate patterns to collapse (Stage 3)
    BOILERPLATE_PATTERNS = [
        (r"Here's?\s+(?:the )?(?:solution|answer|approach)[^:]*:\n", "→ "),
        (r"(?:The )?(?:key |main )?(?:steps?|steps|process|approach)[^:]*:\n", "→ "),
        (r"(?:For |To accomplish this)[^:]*:\n", "→ "),
    ]

    def __init__(self, enable: bool = True):
        """Initialize compressor.

        Args:
            enable: Whether compression is enabled
        """
        self.enable = enable

    def compress(
        self,
        response: str,
        target_reduction: float = 0.6,
        max_output_tokens: Optional[int] = None,
    ) -> CompressionResult:
        """Compress response through 4-stage pipeline.

        Args:
            response: LLM response text
            target_reduction: Target compression ratio (0.6 = 60%)
            max_output_tokens: Hard limit on output tokens (for safety)

        Returns:
            CompressionResult with compression stats
        """
        if not self.enable or not response or len(response.strip()) < 100:
            # Skip compression for very short responses
            return CompressionResult(
                original_tokens=self._estimate_tokens(response),
                compressed_tokens=self._estimate_tokens(response),
                compression_ratio=1.0,
                output=response,
                stages_applied=[],
            )

        original_tokens = self._estimate_tokens(response)
        stages_applied = []
        compressed = response

        # Stage 1: Filler removal (target: 5-10%)
        before_1 = compressed
        compressed = self._remove_filler(compressed)
        if len(compressed) < len(before_1):
            stages_applied.append("filler_removal")

        # Stage 2: Example consolidation (target: 15-20%)
        before_2 = compressed
        compressed = self._consolidate_examples(compressed)
        if len(compressed) < len(before_2):
            stages_applied.append("example_consolidation")

        # Stage 3: Boilerplate collapse (target: 20-30%)
        before_3 = compressed
        compressed = self._collapse_boilerplate(compressed)
        if len(compressed) < len(before_3):
            stages_applied.append("boilerplate_collapse")

        # Stage 4: Semantic extraction (target: 10-20%)
        before_4 = compressed
        compressed = self._extract_key_sentences(
            compressed, target_reduction=target_reduction
        )
        if len(compressed) < len(before_4):
            stages_applied.append("semantic_extraction")

        # Apply hard limit if specified
        if max_output_tokens:
            max_chars = max_output_tokens * 4
            if len(compressed) > max_chars:
                compressed = compressed[:max_chars].rsplit(" ", 1)[0] + "..."

        compressed_tokens = self._estimate_tokens(compressed)
        compression_ratio = (
            compressed_tokens / original_tokens if original_tokens > 0 else 1.0
        )

        return CompressionResult(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            output=compressed,
            stages_applied=stages_applied,
        )

    def _remove_filler(self, text: str) -> str:
        """Stage 1: Remove filler words and unnecessary patterns.

        Targets: "I think", "basically", "actually", etc.
        Typical reduction: 5-10%
        """
        lines = text.split("\n")
        filtered_lines = []

        for line in lines:
            # Skip very short lines (probably structure)
            if len(line.strip()) < 10:
                filtered_lines.append(line)
                continue

            filtered = line

            # Remove filler words at line start
            for filler in self.FILLER_WORDS:
                pattern = rf"\b{re.escape(filler)}\b[,:]?\s*"
                filtered = re.sub(pattern, "", filtered, flags=re.IGNORECASE)

            # Apply verbose phrase patterns
            for pattern, replacement in self.VERBOSE_PHRASES:
                filtered = re.sub(pattern, replacement, filtered, flags=re.IGNORECASE)

            # Clean up multiple spaces
            filtered = re.sub(r"\s+", " ", filtered).strip()

            if filtered:
                filtered_lines.append(filtered)

        return "\n".join(filtered_lines)

    def _consolidate_examples(self, text: str) -> str:
        """Stage 2: Consolidate multiple examples into one.

        Keeps the first concrete example and tags additional ones as "similar".
        Typical reduction: 15-20%
        """
        # Detect example blocks (lines starting with Example/For example/E.g.)
        lines = text.split("\n")
        example_pattern = re.compile(
            r"^(?:Example|For example|E\.g\.|Here'?s? (?:an? )?example|Like this|Try)[:\s]",
            re.IGNORECASE,
        )

        kept_first_example = False
        filtered_lines = []
        in_example_block = False
        example_lines = 0

        for i, line in enumerate(lines):
            is_example = example_pattern.match(line)

            # Check if line is part of a code block or indented (continuation)
            is_continuation = line.startswith((" ", "\t")) and example_lines > 0

            if is_example:
                if not kept_first_example:
                    # Keep the first example
                    kept_first_example = True
                    in_example_block = True
                    example_lines = 1
                    filtered_lines.append(line)
                else:
                    # Skip additional examples but add marker
                    if not filtered_lines or not filtered_lines[-1].startswith(
                        "[Additional examples omitted"
                    ):
                        filtered_lines.append("[Additional examples omitted for brevity]")
                    in_example_block = True
                    example_lines = 1
            elif is_continuation:
                # Continue the current example block
                example_lines += 1
                if kept_first_example and in_example_block:
                    filtered_lines.append(line)
            else:
                # Non-example line
                in_example_block = False
                example_lines = 0
                filtered_lines.append(line)

        return "\n".join(filtered_lines)

    def _collapse_boilerplate(self, text: str) -> str:
        """Stage 3: Collapse verbose explanations into bullet points.

        Targets: "Here's the solution:", "The key steps are:"
        Typical reduction: 20-30%
        """
        result = text

        # Apply boilerplate patterns
        for pattern, replacement in self.BOILERPLATE_PATTERNS:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # Collapse overly long paragraphs (>500 chars) into bullet points
        paragraphs = result.split("\n\n")
        collapsed_paragraphs = []

        for para in paragraphs:
            if len(para) > 500 and "\n" not in para:
                # Long single-line paragraph - convert to bullets
                sentences = re.split(r"(?<=[.!?])\s+", para)
                if len(sentences) > 3:
                    collapsed = "• " + "\n• ".join(
                        s.strip() for s in sentences if s.strip()
                    )
                    collapsed_paragraphs.append(collapsed)
                else:
                    collapsed_paragraphs.append(para)
            else:
                collapsed_paragraphs.append(para)

        return "\n\n".join(collapsed_paragraphs)

    def _extract_key_sentences(
        self, text: str, target_reduction: float = 0.6
    ) -> str:
        """Stage 4: Extract and keep only key sentences.

        Uses simple TF-IDF heuristic to identify important sentences.
        Typical reduction: 10-20%
        """
        sentences = self._split_sentences(text)

        if len(sentences) <= 3:
            return text

        # Score sentences by importance
        scores = []
        for sentence in sentences:
            score = self._score_sentence(sentence, sentences)
            scores.append((sentence, score))

        # Keep top N sentences to achieve target reduction
        target_sentences = max(
            1, int(len(sentences) * (1 - target_reduction / 2))
        )  # More conservative
        top_sentences = sorted(scores, key=lambda x: x[1], reverse=True)[
            :target_sentences
        ]

        # Preserve original order
        sentence_set = {s[0] for s in top_sentences}
        kept = [s for s in sentences if s in sentence_set]

        return " ".join(kept)

    def _split_sentences(self, text: str) -> list[str]:
        """Split text into sentences, preserving structure."""
        # Handle code blocks and lists specially
        sentences = []
        current = ""

        for line in text.split("\n"):
            stripped = line.strip()

            # Skip empty lines or code/structure lines
            if not stripped or line.startswith(("```", "  ", "\t", "- ", "• ")):
                if current:
                    sentences.append(current.strip())
                    current = ""
                if line.startswith(("- ", "• ")):
                    sentences.append(line)
                continue

            # Sentence-split with regex
            line_sentences = re.split(r"(?<=[.!?])\s+", stripped)
            for sent in line_sentences:
                if sent:
                    current += " " + sent
                    if sent[-1] in ".!?":
                        sentences.append(current.strip())
                        current = ""

        if current:
            sentences.append(current.strip())

        return [s for s in sentences if len(s) > 10]

    def _score_sentence(self, sentence: str, all_sentences: list[str]) -> float:
        """Score a sentence by importance (simple TF-IDF heuristic)."""
        score = 0.0

        # Favor sentences with keywords
        important_words = {
            "important", "key", "critical", "must", "should", "error",
            "warning", "note", "remember", "ensure", "verify", "check",
            "use", "implement", "required", "solution", "approach", "method"
        }

        words = set(sentence.lower().split())
        keyword_overlap = len(words & important_words)
        score += keyword_overlap * 2

        # Favor longer sentences (likely more information)
        score += len(sentence) / 100

        # Penalize sentences that are all caps (usually headers)
        if sentence.isupper():
            score *= 0.5

        # Penalize very short sentences
        if len(sentence) < 20:
            score *= 0.5

        return score

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count (rough: 1 token ≈ 4 characters)."""
        return max(1, len(text) // 4)


def compress_response(
    response: str,
    enabled: bool = True,
    target_reduction: float = 0.6,
) -> CompressionResult:
    """Convenience function to compress LLM response.

    Args:
        response: LLM response to compress
        enabled: Whether compression is enabled
        target_reduction: Target compression ratio (0.6 = 60%)

    Returns:
        CompressionResult with compression metrics
    """
    compressor = ResponseCompressor(enable=enabled)
    return compressor.compress(response, target_reduction=target_reduction)
