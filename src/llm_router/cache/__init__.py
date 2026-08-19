"""Cache layer — two caches with different keys + use cases.

Two independent caches live here:

1. **ClassificationCache** (legacy, ported from llm-router):
       SHA-256 exact-match LRU. Keys = (prompt, quality_mode, min_model).
       Caches ClassificationResult so the router doesn't re-classify
       identical prompts. Imported as `from llm_router.cache import get_cache`.

2. **SemanticCache** (v0.0.2 stub, sqlite-vec backend in v0.0.2 impl):
       Embedding-similarity lookup. Reuses *responses* across
       semantically-equivalent prompts (paraphrases, near-duplicates).
       Imported as `from llm_router.cache import SemanticCache`.

The two coexist because they answer different questions:
    - ClassificationCache: "did we already classify this exact prompt?"
    - SemanticCache: "did we already answer something semantically similar?"
"""
from llm_router.cache.classification import (
    CacheEntry,
    CacheStats,
    ClassificationCache,
    get_cache,
)
from llm_router.cache.store import SemanticCache, SemanticCacheEntry

__all__ = [
    # Legacy classification cache
    "CacheEntry",
    "CacheStats",
    "ClassificationCache",
    "get_cache",
    # New semantic response cache
    "SemanticCache",
    "SemanticCacheEntry",
]
