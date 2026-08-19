"""Semantic response cache — scaffold for v0.0.1.

v0.0.2 will implement:
    - sqlite-vec backend at ~/.llm-router/cache.db
    - sentence-transformers/all-MiniLM-L6-v2 embeddings (~22 MB, CPU-fast)
    - cosine similarity threshold (default 0.92)
    - TTL eviction
    - per-host scope so Cursor and Claude Code don't collide
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticCacheEntry:
    """A response cache entry keyed by prompt embedding similarity (v0.0.2+)."""

    prompt: str
    response: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)


class SemanticCache:
    """Placeholder cache — v0.0.1 has no persistence, every lookup misses.

    Existing in-process classification cache (llm_router.cache module from the
    llm-router port) handles SHA-256 LRU for now. The semantic upgrade
    lands in v0.0.2 once sqlite-vec + sentence-transformers are wired.
    """

    def __init__(self, *, similarity_threshold: float = 0.92, max_entries: int = 10_000) -> None:
        self.similarity_threshold = similarity_threshold
        self.max_entries = max_entries

    def get(self, prompt: str) -> SemanticCacheEntry | None:
        return None

    def put(self, prompt: str, response: str, model: str, **metadata: Any) -> None:
        return None

    def stats(self) -> dict[str, int]:
        return {"entries": 0, "hits": 0, "misses": 0}
