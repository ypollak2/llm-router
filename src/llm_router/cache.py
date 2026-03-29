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
"""Maximum number of cached classification results before LRU eviction kicks in."""

TTL_SECONDS = 3600  # 1 hour
"""Time-to-live for each cache entry. After this period, entries are treated as
stale and removed on next access (lazy expiration)."""


@dataclass(frozen=True)
class CacheEntry:
    """A single cached classification result with metadata for eviction decisions.

    Attributes:
        result: The cached ClassificationResult (complexity, task_type, etc.).
        created_at: Monotonic timestamp of when this entry was stored, used for
            TTL expiration checks.
        prompt_preview: First 80 characters of the original prompt, stored solely
            for debug logging when entries are evicted or expired.
    """

    result: ClassificationResult
    created_at: float
    prompt_preview: str


@dataclass
class CacheStats:
    """Running hit/miss/eviction counters for cache observability.

    Attributes:
        hits: Number of cache lookups that returned a valid (non-expired) entry.
        misses: Number of lookups that found no entry or an expired entry.
        evictions: Number of entries removed to make room when the cache was full.
    """

    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def total(self) -> int:
        """Total number of cache lookups (hits + misses)."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups that were hits, 0.0-1.0. Returns 0.0 if no lookups yet."""
        return self.hits / self.total if self.total > 0 else 0.0


class ClassificationCache:
    """Thread-safe in-memory LRU cache for prompt classification results.

    Uses an ``OrderedDict`` as an LRU structure: accessed entries move to the end,
    and the oldest (front) entry is evicted when capacity is reached. Each entry
    also has a TTL; expired entries are lazily removed on next ``get()``.

    All public methods acquire an ``asyncio.Lock`` so concurrent coroutines
    sharing the same event loop cannot corrupt internal state.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES, ttl_seconds: int = TTL_SECONDS):
        """Initialize the cache.

        Args:
            max_entries: Maximum number of entries before LRU eviction.
            ttl_seconds: Seconds before an entry is considered stale.
        """
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._stats = CacheStats()

    @staticmethod
    def _hash_key(prompt: str, quality_mode: str = "balanced", min_model: str = "haiku") -> str:
        """Produce a deterministic cache key from routing inputs.

        Concatenates prompt, quality_mode, and min_model with pipe separators,
        then SHA-256 hashes the result. SHA-256 is used (rather than a simpler
        hash) to avoid collisions across the large prompt space and to produce
        a fixed-length key regardless of prompt size.

        Args:
            prompt: The full user prompt text.
            quality_mode: The quality routing mode (e.g. "balanced", "quality").
            min_model: The minimum model tier (e.g. "haiku", "sonnet").

        Returns:
            A 64-character hex digest suitable as a dict key.
        """
        raw = f"{prompt}|{quality_mode}|{min_model}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def get(
        self, prompt: str, quality_mode: str = "balanced", min_model: str = "haiku"
    ) -> ClassificationResult | None:
        """Look up a cached classification for the given prompt and routing params.

        On a hit, the entry is promoted to most-recently-used. On a miss (no entry
        or expired entry), the miss counter is incremented.

        Args:
            prompt: The full user prompt text.
            quality_mode: The quality routing mode.
            min_model: The minimum model tier.

        Returns:
            The cached ClassificationResult, or None on miss or expiration.
        """
        key = self._hash_key(prompt, quality_mode, min_model)
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._stats.misses += 1
                return None

            # Check TTL — lazy expiration on read
            if time.monotonic() - entry.created_at > self._ttl:
                del self._cache[key]
                self._stats.misses += 1
                log.debug("Cache expired: %s", entry.prompt_preview)
                return None

            # Move to end (most recently used) to protect from LRU eviction
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
        """Store a classification result, evicting the oldest entry if at capacity.

        If the key already exists, the entry is updated in place and promoted to
        most-recently-used. Otherwise, the oldest entries are evicted as needed
        to stay within ``max_entries``.

        Args:
            prompt: The full user prompt text (first 80 chars stored for debugging).
            result: The ClassificationResult to cache.
            quality_mode: The quality routing mode used for this classification.
            min_model: The minimum model tier used for this classification.
        """
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
        """Remove all entries from the cache.

        Returns:
            The number of entries that were cleared.
        """
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    async def get_stats(self) -> dict:
        """Return a snapshot of cache statistics for observability.

        Returns:
            A dict containing: entries (current count), max_entries, ttl_seconds,
            hits, misses, hit_rate (formatted string), evictions,
            oldest_entry_age_hours, and memory_estimate_kb (rough ~2KB/entry).
        """
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
"""Lazily-initialized singleton instance. Use ``get_cache()`` to access."""


def get_cache() -> ClassificationCache:
    """Get or create the global classification cache singleton.

    The singleton is created on first call with default settings (1000 entries,
    1-hour TTL). All callers share the same instance so cache hits accumulate
    across the entire application.

    Returns:
        The shared ClassificationCache instance.
    """
    global _cache
    if _cache is None:
        _cache = ClassificationCache()
    return _cache
