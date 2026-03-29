"""Structural prompt compaction — reduces token usage before sending to LLMs.

Applies a pipeline of pure-Python text transformations that shrink prompts
without losing meaning. Each strategy is conservative: it preserves error
messages, file paths, URLs, and explicit constraints.

Strategies (applied in order):
  1. collapse_whitespace — normalize excessive blank lines and spaces
  2. strip_code_comments — remove single-line comments from code blocks
  3. dedup_sections — deduplicate repeated multi-line blocks
  4. truncate_long_code — shorten oversized fenced code blocks
  5. collapse_stack_traces — trim verbose stack traces to top/bottom frames
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of a compaction pass, tracking what changed and how much."""

    original_length: int
    compacted_length: int
    tokens_saved_estimate: int  # (original - compacted) // 4
    strategies_applied: tuple[str, ...]


def estimate_tokens(text: str) -> int:
    """Rough token estimate: len(text) // 4."""
    return len(text) // 4


# ---------------------------------------------------------------------------
# Strategy 1: collapse_whitespace
# ---------------------------------------------------------------------------

def collapse_whitespace(text: str) -> str:
    """Normalize excessive whitespace without changing meaning.

    - Replace runs of 3+ newlines with exactly 2 newlines
    - Strip trailing whitespace from each line
    - Collapse runs of 3+ spaces to a single space (outside leading indent)
    """
    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)

    # Replace 3+ consecutive newlines with 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Collapse runs of 3+ spaces to single space, but preserve leading indent.
    # We process each line: keep leading whitespace intact, collapse interior runs.
    result_lines = []
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        indent = line[: len(line) - len(stripped)]
        # Collapse 3+ spaces to 1 in the non-indent portion
        stripped = re.sub(r" {3,}", " ", stripped)
        result_lines.append(indent + stripped)

    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# Strategy 2: strip_code_comments
# ---------------------------------------------------------------------------

# Matches fenced code blocks: ``` optionally followed by a language tag
_FENCED_BLOCK_RE = re.compile(r"(```[^\n]*\n)(.*?)(```)", re.DOTALL)


def _strip_comments_from_code(code: str) -> str:
    """Remove single-line # and // comments from code, preserving URLs and shebangs."""
    result_lines = []
    for line in code.split("\n"):
        stripped = line.lstrip()

        # Preserve shebangs
        if stripped.startswith("#!"):
            result_lines.append(line)
            continue

        # Preserve full-line comments that are likely important (error messages, paths)
        # Only strip full-line # and // comments
        if stripped.startswith("#") or stripped.startswith("//"):
            # Keep lines that look like they contain file paths or URLs
            if re.search(r"https?://|/[\w.]+/", stripped):
                result_lines.append(line)
                continue
            # Remove the comment line entirely
            continue

        # For inline comments: remove trailing # or // comments
        # but be careful with strings and URLs
        # Only handle simple cases — if the line has quotes, leave it alone
        if '"' in line or "'" in line or "`" in line:
            result_lines.append(line)
            continue

        # Remove trailing // comment (not part of a URL)
        cleaned = re.sub(r"(?<!:)//[^/].*$", "", line).rstrip()
        # Remove trailing # comment (not part of a shebang or URL)
        cleaned = re.sub(r"(?<!&)#(?!!).*$", "", cleaned).rstrip()

        result_lines.append(cleaned if cleaned else line)

    return "\n".join(result_lines)


def strip_code_comments(text: str) -> str:
    """Remove single-line comments from fenced code blocks.

    Preserves: URLs (http://), shebangs (#!), string contents, block/doc comments.
    Only operates inside fenced code blocks (``` ... ```).
    """
    def _replace(m: re.Match) -> str:
        opener = m.group(1)
        body = m.group(2)
        closer = m.group(3)
        return opener + _strip_comments_from_code(body) + closer

    return _FENCED_BLOCK_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# Strategy 3: dedup_sections
# ---------------------------------------------------------------------------

def dedup_sections(text: str) -> str:
    """Remove duplicate blocks of 3+ identical consecutive lines.

    On second and subsequent occurrences, replace with a marker.
    """
    lines = text.split("\n")
    if len(lines) < 6:
        return text

    min_block = 3
    seen_blocks: set[str] = set()
    result: list[str] = []
    i = 0

    while i < len(lines):
        # Try to find a block starting at i that we've seen before.
        # Check block sizes from largest feasible down to min_block.
        matched = False
        max_block = min(50, len(lines) - i)  # cap search to avoid O(n^2) blowup

        for size in range(max_block, min_block - 1, -1):
            block_key = "\n".join(lines[i : i + size])
            if block_key in seen_blocks:
                result.append("[... repeated section removed ...]")
                i += size
                matched = True
                break

        if not matched:
            # Record blocks starting at this position
            for size in range(min_block, min(max_block + 1, len(lines) - i + 1)):
                block_key = "\n".join(lines[i : i + size])
                seen_blocks.add(block_key)
            result.append(lines[i])
            i += 1

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Strategy 4: truncate_long_code
# ---------------------------------------------------------------------------

_FENCED_BLOCK_FULL_RE = re.compile(r"(```[^\n]*\n)(.*?)(```)", re.DOTALL)


def truncate_long_code(text: str) -> str:
    """Truncate fenced code blocks exceeding 50 lines.

    Keeps first 20 and last 10 lines, inserting a truncation marker.
    """
    def _truncate(m: re.Match) -> str:
        opener = m.group(1)
        body = m.group(2)
        closer = m.group(3)

        body_lines = body.split("\n")
        # Account for trailing empty line before closing ```
        effective = body_lines
        if effective and effective[-1] == "":
            effective = effective[:-1]

        if len(effective) <= 50:
            return m.group(0)

        truncated_count = len(effective) - 30
        kept = (
            effective[:20]
            + [f"[... {truncated_count} lines truncated ...]"]
            + effective[-10:]
        )
        # Restore trailing newline if original had one
        if body_lines and body_lines[-1] == "":
            kept.append("")
        return opener + "\n".join(kept) + closer

    return _FENCED_BLOCK_FULL_RE.sub(_truncate, text)


# ---------------------------------------------------------------------------
# Strategy 5: collapse_stack_traces
# ---------------------------------------------------------------------------

# Heuristic: a stack frame line typically matches "  File ...", "    at ...",
# or "  at ..." patterns common in Python and JS/Node tracebacks.
_FRAME_RE = re.compile(r"^\s+(File |at |in )", re.MULTILINE)


def collapse_stack_traces(text: str) -> str:
    """Collapse stack traces with >10 frames, keeping first 3 and last 3.

    Detects Python-style ('  File "..."') and JS-style ('    at ...') frames.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        # Detect start of a stack trace: a header line followed by frame lines
        # Common headers: "Traceback (most recent call last):", "Error:", stack dump
        if i + 1 < len(lines) and _FRAME_RE.match(lines[i + 1]):
            # Collect consecutive frame lines (and their detail lines)
            frames: list[list[str]] = []
            header_line = lines[i]
            result.append(header_line)
            i += 1

            while i < len(lines) and _FRAME_RE.match(lines[i]):
                frame_lines = [lines[i]]
                i += 1
                # Grab indented continuation lines (e.g. source code line in Python tb)
                while i < len(lines) and lines[i].startswith("    ") and not _FRAME_RE.match(lines[i]):
                    frame_lines.append(lines[i])
                    i += 1
                frames.append(frame_lines)

            if len(frames) > 10:
                truncated = len(frames) - 6
                for frame in frames[:3]:
                    result.extend(frame)
                result.append(f"    [... {truncated} frames truncated ...]")
                for frame in frames[-3:]:
                    result.extend(frame)
            else:
                for frame in frames:
                    result.extend(frame)
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_STRATEGIES: tuple[tuple[str, callable], ...] = (
    ("collapse_whitespace", collapse_whitespace),
    ("strip_code_comments", strip_code_comments),
    ("dedup_sections", dedup_sections),
    ("truncate_long_code", truncate_long_code),
    ("collapse_stack_traces", collapse_stack_traces),
)


async def compact_structural(
    text: str, threshold: int = 4000
) -> tuple[str, CompactionResult]:
    """Apply structural compaction if text exceeds threshold tokens.

    Runs each strategy in order, tracking which ones actually changed the text.

    Args:
        text: The prompt text to compact.
        threshold: Token count threshold below which no compaction is applied.

    Returns:
        A tuple of (compacted_text, CompactionResult). If below threshold,
        returns the original text unchanged.
    """
    original_length = len(text)

    if estimate_tokens(text) <= threshold:
        return text, CompactionResult(
            original_length=original_length,
            compacted_length=original_length,
            tokens_saved_estimate=0,
            strategies_applied=(),
        )

    applied: list[str] = []
    current = text

    for name, strategy in _STRATEGIES:
        result = strategy(current)
        if result != current:
            applied.append(name)
            current = result

    compacted_length = len(current)
    return current, CompactionResult(
        original_length=original_length,
        compacted_length=compacted_length,
        tokens_saved_estimate=(original_length - compacted_length) // 4,
        strategies_applied=tuple(applied),
    )
