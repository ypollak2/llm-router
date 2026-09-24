"""Classification cache: SHA-256 exact-match LRU of ClassificationResult.

Keys = (prompt, quality_mode, min_model), so the router doesn't re-classify
identical prompts. Imported as `from llm_router.cache import get_cache`.

The response-reuse cache is `llm_router.semantic_cache`. A same-named
`SemanticCache` stub lived here (cache/store.py) whose get() always returned
None, with zero callers; removed per audit 2026-09-24 (04_structure_duplication,
confirmed in 13_verify_structure_docs).
"""
from llm_router.cache.classification import (
    CacheEntry,
    CacheStats,
    ClassificationCache,
    get_cache,
)

__all__ = [
    # Legacy classification cache
    "CacheEntry",
    "CacheStats",
    "ClassificationCache",
    "get_cache",
]
