"""Keyword signal — bm25 / ngram / fuzzy keyword matching.

Stub for v0.0.1. Full bm25/ngram/fuzzy implementations land in v0.0.2
once the signal config schema is locked.
"""
from __future__ import annotations

from dataclasses import dataclass

from llm_router.signals.base import SignalScore


@dataclass(frozen=True)
class KeywordSignal:
    """Score is 1.0 if any keyword matches, 0.0 otherwise (v0.0.1 stub).

    Future: bm25 with configurable threshold, ngram with arity, fuzzy with
    Levenshtein-distance threshold — matching the vllm-semantic-router
    signal/keywords config schema.
    """

    name: str
    keywords: tuple[str, ...]
    threshold: float = 0.5
    case_sensitive: bool = False
    method: str = "literal"  # v0.0.2: bm25 | ngram | fuzzy | literal

    def evaluate(self, prompt: str, context: dict | None = None) -> SignalScore:
        haystack = prompt if self.case_sensitive else prompt.lower()
        for kw in self.keywords:
            needle = kw if self.case_sensitive else kw.lower()
            if needle in haystack:
                return SignalScore(
                    name=self.name,
                    score=1.0,
                    threshold=self.threshold,
                    evidence=f"literal match: {needle!r}",
                )
        return SignalScore(
            name=self.name, score=0.0, threshold=self.threshold, evidence="no keyword match"
        )
