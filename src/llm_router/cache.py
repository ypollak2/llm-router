"""Prompt classification cache — SHA-256 exact match with in-memory LRU.

Caches ClassificationResult (not RoutingRecommendation) so budget pressure
is always applied fresh. This means cached results stay valid even as quota changes.

Design:
- SHA-256 hash of (prompt + quality_mode + min_model) for O(1) lookup
- In-memory LRU: max 1000 entries, 1-hour TTL
- asyncio.Lock for thread safety
- Zero overhead for misses (hash lookup only)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from llm_router.types import ClassificationResult

log = logging.getLogger("llm_router.cache")

MAX_ENTRIES = 1000
TTL_SECONDS = 3600  # 1 hour


@dataclass(frozen=True)
class CacheEntry:
    result: ClassificationResult
    created_at: float
    prompt_preview: str  # first 80 chars for debugging


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total > 0 else 0.0


class ClassificationCache:
    """Thread-safe in-memory LRU cache for classification results."""

    def __init__(self, max_entries: int = MAX_ENTRIES, ttl_seconds: int = TTL_SECONDS):
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._stats = CacheStats()

    @staticmethod
    def _hash_key(prompt: str, quality_mode: str = "balanced", min_model: str = "haiku") -> str:
        """SHA-256 hash of prompt + routing params for exact-match lookup."""
        raw = f"{prompt}|{quality_mode}|{min_model}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def get(
        self, prompt: str, quality_mode: str = "balanced", min_model: str = "haiku"
    ) -> ClassificationResult | None:
        """Look up cached classification. Returns None on miss or expired entry."""
        key = self._hash_key(prompt, quality_mode, min_model)
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._stats.misses += 1
                return None

            # Check TTL
            if time.monotonic() - entry.created_at > self._ttl:
                del self._cache[key]
                self._stats.misses += 1
                log.debug("Cache expired: %s", entry.prompt_preview)
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._stats.hits += 1
            log.debug("Cache hit: %s", entry.prompt_preview)
            return entry.result

    async def put(
        self,
        prompt: str,
        result: ClassificationResult,
        quality_mode: str = "balanced",
        min_model: str = "haiku",
    ) -> None:
        """Store a classification result in cache."""
        key = self._hash_key(prompt, quality_mode, min_model)
        entry = CacheEntry(
            result=result,
            created_at=time.monotonic(),
            prompt_preview=prompt[:80],
        )
        async with self._lock:
            # If key exists, update it
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = entry
                return

            # Evict oldest if at capacity
            while len(self._cache) >= self._max_entries:
                evicted_key, evicted = self._cache.popitem(last=False)
                self._stats.evictions += 1
                log.debug("Cache evicted: %s", evicted.prompt_preview)

            self._cache[key] = entry

    async def clear(self) -> int:
        """Clear all entries. Returns count of cleared entries."""
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    async def get_stats(self) -> dict:
        """Return cache statistics."""
        async with self._lock:
            now = time.monotonic()
            entries = len(self._cache)
            oldest_age = 0.0
            if self._cache:
                oldest = next(iter(self._cache.values()))
                oldest_age = (now - oldest.created_at) / 3600  # hours

            return {
                "entries": entries,
                "max_entries": self._max_entries,
                "ttl_seconds": self._ttl,
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "hit_rate": f"{self._stats.hit_rate:.1%}",
                "evictions": self._stats.evictions,
                "oldest_entry_age_hours": round(oldest_age, 2),
                "memory_estimate_kb": entries * 2,  # ~2KB per entry estimate
            }


# ── Module-level singleton ───────────────────────────────────────────────────

_cache: ClassificationCache | None = None


def get_cache() -> ClassificationCache:
    """Get or create the global classification cache."""
    global _cache
    if _cache is None:
        _cache = ClassificationCache()
    return _cache
