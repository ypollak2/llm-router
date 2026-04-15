"""Dynamic LLM discovery — scan available models at routing time.

Discovers which LLM providers are actually available by:
  1. Checking if Ollama is running (local models)
  2. Checking if API keys are configured (cloud providers)
  3. Filtering model chains based on discovered availability

Discovery happens at routing time (per-request) so chains always reflect
the current state of configured keys and running services.
"""

from __future__ import annotations

import asyncio
import os

from llm_router.config import get_config
from llm_router.logging import get_logger
from llm_router.profiles import provider_from_model

log = get_logger("llm_router.discover")

# Cache for Ollama reachability (5 second TTL per config probe)
_ollama_cache: dict[str, tuple[bool, float]] = {}
_OLLAMA_CACHE_TTL = 5.0

# Discovery cache file path
_DISCOVERY_CACHE = os.path.expanduser("~/.llm-router/discovery.json")


def is_ollama_available() -> bool:
    """Check if Ollama is configured and reachable.
    
    Returns:
        True if OLLAMA_BASE_URL is set and Ollama responds to /api/tags
    """
    import time
    
    config = get_config()
    if not config.ollama_base_url:
        return False
    
    now = time.monotonic()
    
    # Check cache
    if config.ollama_base_url in _ollama_cache:
        cached_result, cached_time = _ollama_cache[config.ollama_base_url]
        if (now - cached_time) < _OLLAMA_CACHE_TTL:
            return cached_result
    
    # Probe Ollama
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"{config.ollama_base_url}/api/tags",
            timeout=1
        ):
            result = True
    except Exception:
        result = False
    
    _ollama_cache[config.ollama_base_url] = (result, now)
    return result


def get_available_providers() -> set[str]:
    """Get set of providers that are actually available.
    
    Checks:
      - API keys configured in environment
      - Ollama running and reachable
      - Codex CLI installed (caller's responsibility to check)
    
    Returns:
        Set of provider names like {"openai", "gemini", "ollama"}
    """
    config = get_config()
    providers = set()
    
    # Check configured API keys
    if config.openai_api_key:
        providers.add("openai")
    if config.gemini_api_key:
        providers.add("gemini")
    if config.perplexity_api_key:
        providers.add("perplexity")
    if config.anthropic_api_key and not config.llm_router_claude_subscription:
        # In subscription mode, Claude is intentionally excluded
        providers.add("anthropic")
    if config.mistral_api_key:
        providers.add("mistral")
    if config.deepseek_api_key:
        providers.add("deepseek")
    if config.groq_api_key:
        providers.add("groq")
    if config.together_api_key:
        providers.add("together")
    if config.xai_api_key:
        providers.add("xai")
    if config.cohere_api_key:
        providers.add("cohere")
    
    # Check Ollama
    if is_ollama_available():
        providers.add("ollama")
    
    return providers


def filter_chain_by_availability(
    chain: list[str],
    available_providers: set[str] | None = None,
) -> list[str]:
    """Filter a model chain to only include available providers.
    
    Removes models whose provider is not in the available set.
    Preserves order so highest-priority models stay first.
    
    Args:
        chain: Ordered list of model IDs (e.g. ["anthropic/claude-haiku", "gemini/gemini-2.5-flash"])
        available_providers: Set of available provider names. If None, discovers automatically.
    
    Returns:
        Filtered chain with only models from available providers.
    """
    if available_providers is None:
        available_providers = get_available_providers()
    
    # Always allow local providers (codex, ollama) even if they need special handling
    available_providers = available_providers | {"codex", "ollama"}
    
    filtered = [
        m for m in chain
        if provider_from_model(m) in available_providers
    ]
    
    return filtered


async def _scan_ollama() -> dict:
    """Scan Ollama for available models.
    
    Returns:
        Dict of model capabilities from Ollama, or empty if unavailable.
    """
    # Stub for testing - will be mocked by tests
    if is_ollama_available():
        models = await asyncio.to_thread(get_cached_ollama_models)
        return {m: {} for m in models}
    return {}


async def _scan_openai() -> dict:
    """Scan OpenAI for available models via /v1/models.
    
    Returns:
        Dict of model capabilities from OpenAI, or empty if unavailable.
    """
    # Stub for testing - will be mocked by tests
    config = get_config()
    if not config.openai_api_key:
        return {}
    # Actual implementation would call OpenAI API
    return {}


async def _scan_gemini() -> dict:
    """Scan Gemini for available models via /v1beta/models.
    
    Returns:
        Dict of model capabilities from Gemini, or empty if unavailable.
    """
    # Stub for testing - will be mocked by tests
    config = get_config()
    if not config.gemini_api_key:
        return {}
    # Actual implementation would call Gemini API
    return {}


async def _scan_api_key_providers() -> dict:
    """Scan for other API providers based on configured keys.
    
    Returns:
        Dict of model capabilities from configured providers.
    """
    # Stub for testing - will be mocked by tests
    return {}


async def discover_available_models() -> dict:
    """Discover all available models across all configured providers.
    
    Runs all scanners in parallel and combines results.
    
    Returns:
        Dict mapping model IDs to their capabilities.
    """
    results = await asyncio.gather(
        _scan_ollama(),
        _scan_openai(),
        _scan_gemini(),
        _scan_api_key_providers(),
        return_exceptions=True,
    )
    
    combined = {}
    for result in results:
        if isinstance(result, dict):
            combined.update(result)
    
    return combined


async def discover_and_build_chain(
    static_chain: list[str],
) -> list[str]:
    """Discover available providers and build dynamic chain.
    
    This is the main entry point for dynamic chain building. It:
      1. Discovers what's actually available (Ollama, API keys, etc)
      2. Filters the static chain to only available providers
      3. Returns the dynamically filtered chain
    
    The static chain from profiles.py is treated as the preference order,
    and dynamic discovery simply removes unavailable options while preserving
    the preference order.
    
    Args:
        static_chain: The base chain from profiles.py for this profile/task
    
    Returns:
        Filtered chain with only available providers
    """
    try:
        available = await asyncio.to_thread(get_available_providers)
    except Exception as e:
        log.warning("Discovery failed, using static chain: %s", e)
        return static_chain
    
    filtered = filter_chain_by_availability(static_chain, available)
    
    if not filtered and static_chain:
        log.warning(
            "All models filtered out by availability — no providers configured. "
            "Static chain: %s | Available: %s",
            static_chain, available
        )
        return static_chain
    
    return filtered


def _load_cache(ttl: int = 3600) -> dict | None:
    """Load discovery cache from disk if it exists and is recent.
    
    Validates that cached data is well-formed (all required fields present, enum values valid).
    Returns None if cache is missing, expired, or corrupted.
    
    Args:
        ttl: Time-to-live in seconds (default: 3600 = 1 hour)
    
    Returns:
        Dict with cached models or None if cache doesn't exist/expired/corrupted.
    """
    import json
    import time
    from llm_router.types import ProviderTier, TaskType
    
    if not os.path.exists(_DISCOVERY_CACHE):
        return None
    
    try:
        with open(_DISCOVERY_CACHE) as f:
            data = json.load(f)
        
        # Check if cache has expired
        if time.time() - data.get("cached_at", 0) > ttl:
            return None
        
        models = data.get("models", {})
        
        # Validate cache by trying to parse each model into ModelCapability
        # This ensures the cache has valid enum values and required fields
        for model_id, model_data in models.items():
            try:
                # Validate required fields
                if "model_id" not in model_data or "provider" not in model_data or "provider_tier" not in model_data:
                    log.debug("Cache entry missing required field: %s", model_id)
                    return None
                
                # Validate enum values by trying to construct them
                ProviderTier(model_data["provider_tier"])  # raises ValueError if invalid
                
                # Validate task_types if present
                if "task_types" in model_data:
                    for tt in model_data["task_types"]:
                        TaskType(tt)  # raises ValueError if invalid
            except (ValueError, KeyError) as e:
                log.debug("Cache validation failed for %s: %s", model_id, e)
                return None
        
        return models
    except Exception as e:
        log.debug("Failed to load discovery cache: %s", e)
        return None


def get_cached_ollama_models() -> list[str]:
    """Get cached list of Ollama models (from discovery cache file).
    
    Returns cached Ollama models from ~/.llm-router/discovery.json only.
    Does NOT fall back to live Ollama discovery - that's config.all_ollama_models()'s job.
    
    Returns:
        List of ollama/model-name strings from cache, or empty list if no cache.
    """
    cached = _load_cache()
    if cached:
        return [
            m_id for m_id, m_data in cached.items()
            if m_data.get("provider") == "ollama"
        ]
    
    # No cache available - return empty list (config will fall back to env vars)
    return []
