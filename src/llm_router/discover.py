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
from functools import lru_cache

from llm_router.config import get_config
from llm_router.logging import get_logger
from llm_router.profiles import provider_from_model

log = get_logger("llm_router.discover")

# Cache for Ollama reachability (5 second TTL per config probe)
_ollama_cache: dict[str, tuple[bool, float]] = {}
_OLLAMA_CACHE_TTL = 5.0


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


@lru_cache(maxsize=1)
def get_cached_ollama_models() -> list[str]:
    """Get cached list of Ollama models (populated by discovery).
    
    Returns:
        List of ollama/model-name strings, or empty if Ollama not available
    """
    if not is_ollama_available():
        return []
    
    config = get_config()
    if not config.ollama_base_url:
        return []
    
    try:
        import urllib.request
        import json
        
        with urllib.request.urlopen(
            f"{config.ollama_base_url}/api/tags",
            timeout=2
        ) as response:
            data = json.loads(response.read().decode())
            models = data.get("models", [])
            return [f"ollama/{m.get('name', '')}" for m in models if m.get('name')]
    except Exception as e:
        log.debug("Failed to list Ollama models: %s", e)
        return []
