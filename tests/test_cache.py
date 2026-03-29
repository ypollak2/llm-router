"""Tests for the prompt classification cache."""

import time


from llm_router.cache import ClassificationCache, get_cache
from llm_router.types import ClassificationResult, Complexity, TaskType


def _make_result(
    complexity: Complexity = Complexity.MODERATE,
    confidence: float = 0.9,
) -> ClassificationResult:
    return ClassificationResult(
        complexity=complexity,
        confidence=confidence,
        reasoning="test",
        inferred_task_type=TaskType.CODE,
        classifier_model="test-model",
        classifier_cost_usd=0.001,
        classifier_latency_ms=50.0,
    )


class TestCacheBasics:
    async def test_put_and_get(self):
        cache = ClassificationCache()
        result = _make_result()
        await cache.put("hello world", result)
        cached = await cache.get("hello world")
        assert cached is not None
        assert cached.complexity == Complexity.MODERATE
        assert cached.confidence == 0.9

    async def test_miss_returns_none(self):
        cache = ClassificationCache()
        assert await cache.get("nonexistent prompt") is None

    async def test_different_params_different_keys(self):
        cache = ClassificationCache()
        result_balanced = _make_result(Complexity.MODERATE)
        result_best = _make_result(Complexity.COMPLEX)

        await cache.put("same prompt", result_balanced, quality_mode="balanced")
        await cache.put("same prompt", result_best, quality_mode="best")

        cached_balanced = await cache.get("same prompt", quality_mode="balanced")
        cached_best = await cache.get("same prompt", quality_mode="best")

        assert cached_balanced is not None
        assert cached_best is not None
        assert cached_balanced.complexity == Complexity.MODERATE
        assert cached_best.complexity == Complexity.COMPLEX


class TestCacheTTL:
    async def test_expired_entry_returns_none(self):
        cache = ClassificationCache(ttl_seconds=1)
        await cache.put("prompt", _make_result())

        # Entry should be valid immediately
        assert await cache.get("prompt") is not None

        # Simulate time passing by patching the entry's created_at
        key = cache._hash_key("prompt")
        cache._cache[key] = cache._cache[key].__class__(
            result=cache._cache[key].result,
            created_at=time.monotonic() - 2,  # 2 seconds ago, TTL is 1s
            prompt_preview=cache._cache[key].prompt_preview,
        )

        assert await cache.get("prompt") is None


class TestCacheLRU:
    async def test_evicts_oldest_when_full(self):
        cache = ClassificationCache(max_entries=3)

        await cache.put("prompt1", _make_result())
        await cache.put("prompt2", _make_result())
        await cache.put("prompt3", _make_result())

        # All three should be present
        assert await cache.get("prompt1") is not None
        assert await cache.get("prompt2") is not None
        assert await cache.get("prompt3") is not None

        # Adding a 4th should evict the oldest (prompt1, since prompt1 was
        # moved to end by the get() call above — actually prompt2 is oldest
        # after the get calls reordered them)
        # Let's reset and test cleanly
        cache = ClassificationCache(max_entries=3)
        await cache.put("a", _make_result())
        await cache.put("b", _make_result())
        await cache.put("c", _make_result())
        await cache.put("d", _make_result())  # should evict "a"

        assert await cache.get("a") is None  # evicted
        assert await cache.get("b") is not None
        assert await cache.get("d") is not None

    async def test_access_refreshes_position(self):
        cache = ClassificationCache(max_entries=3)

        await cache.put("a", _make_result())
        await cache.put("b", _make_result())
        await cache.put("c", _make_result())

        # Access "a" to move it to the end
        await cache.get("a")

        # Adding "d" should now evict "b" (oldest after "a" was refreshed)
        await cache.put("d", _make_result())

        assert await cache.get("a") is not None  # refreshed, should survive
        assert await cache.get("b") is None  # evicted
        assert await cache.get("c") is not None
        assert await cache.get("d") is not None


class TestCacheStats:
    async def test_stats_tracking(self):
        cache = ClassificationCache()

        await cache.put("hit_me", _make_result())
        await cache.get("hit_me")       # hit
        await cache.get("hit_me")       # hit
        await cache.get("miss_me")      # miss

        stats = await cache.get_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["entries"] == 1
        assert stats["hit_rate"] == "66.7%"

    async def test_clear(self):
        cache = ClassificationCache()
        await cache.put("a", _make_result())
        await cache.put("b", _make_result())

        count = await cache.clear()
        assert count == 2

        stats = await cache.get_stats()
        assert stats["entries"] == 0


class TestCacheHashKey:
    def test_deterministic(self):
        h1 = ClassificationCache._hash_key("hello", "balanced", "haiku")
        h2 = ClassificationCache._hash_key("hello", "balanced", "haiku")
        assert h1 == h2

    def test_different_prompts_different_keys(self):
        h1 = ClassificationCache._hash_key("hello", "balanced", "haiku")
        h2 = ClassificationCache._hash_key("world", "balanced", "haiku")
        assert h1 != h2

    def test_different_modes_different_keys(self):
        h1 = ClassificationCache._hash_key("hello", "balanced", "haiku")
        h2 = ClassificationCache._hash_key("hello", "best", "haiku")
        assert h1 != h2


class TestGetCacheSingleton:
    def test_returns_same_instance(self):
        import llm_router.cache as cache_module
        cache_module._cache = None  # reset
        c1 = get_cache()
        c2 = get_cache()
        assert c1 is c2
        cache_module._cache = None  # cleanup
