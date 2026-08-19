"""CHZ-AUD-006 regression: benchmark corpus size matches the README claim.

The README states "Reproducible measurements on a fixed corpus of 8,400 real-world
prompts" but the actual corpus has only 53 prompts (easy.jsonl=20, hard.jsonl=16,
moderate.jsonl=17).  This test enforces that the README's advertised corpus size
matches the real file count, so both stay in sync.
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
README = REPO_ROOT / "README.md"
CORPUS_DIR = REPO_ROOT / "bench" / "corpus"


def _count_corpus_prompts() -> int:
    """Count total lines across all *.jsonl files in bench/corpus/."""
    total = 0
    for jsonl in CORPUS_DIR.glob("*.jsonl"):
        lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        total += len(lines)
    return total


def _readme_corpus_claim() -> int | None:
    """Extract the numeric corpus-size claim from the README Benchmarks section.

    Returns the integer if found, else None.  Matches several phrasings:
      - "a smoke corpus of 53 prompts"
      - "fixed corpus of 8,400 real-world prompts"
      - "53-prompt corpus"
      - "corpus of 53 prompts"
    """
    text = README.read_text(encoding="utf-8")
    for pattern in [
        r"fixed corpus of ([\d,]+) real-world prompts",
        r"smoke corpus of ([\d,]+) prompts",
        r"corpus of ([\d,]+) prompts",
        r"([\d,]+)-prompt corpus",
    ]:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCorpusSizeConsistency:
    """README corpus size claim must match the actual bench/corpus/*.jsonl files."""

    def test_readme_corpus_claim_matches_actual_file_count(self) -> None:
        """The README's numeric corpus-size claim equals the real JSONL line count."""
        actual = _count_corpus_prompts()
        claimed = _readme_corpus_claim()

        assert claimed is not None, (
            "Could not find a numeric corpus-size claim in README.md Benchmarks section. "
            "Add a sentence like 'a smoke corpus of 53 prompts' so this test can verify it."
        )
        assert claimed == actual, (
            f"README claims a corpus of {claimed:,} prompts, "
            f"but bench/corpus/*.jsonl contains {actual} prompts. "
            "Either update README.md to say '{actual}' or expand the corpus to match the claim."
        )

    def test_corpus_dir_is_not_empty(self) -> None:
        """bench/corpus/ must contain at least one non-empty .jsonl file."""
        assert CORPUS_DIR.is_dir(), f"Missing corpus directory: {CORPUS_DIR}"
        jsonl_files = list(CORPUS_DIR.glob("*.jsonl"))
        assert jsonl_files, f"No *.jsonl files found in {CORPUS_DIR}"
        assert _count_corpus_prompts() > 0, "All corpus JSONL files are empty"

    def test_readme_does_not_claim_8400_prompts(self) -> None:
        """Ensure the removed false 8,400-prompt claim never comes back."""
        text = README.read_text(encoding="utf-8")
        assert "8,400 real-world prompts" not in text, (
            "README still contains the false claim '8,400 real-world prompts'. "
            "The actual corpus has only 53 prompts. Update README to reflect the real number."
        )
