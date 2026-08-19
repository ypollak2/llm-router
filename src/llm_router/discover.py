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

# Warm-path tracking — path to file that records last successful Ollama call time
_OLLAMA_LAST_OK = os.path.expanduser("~/.llm-router/ollama_last_ok.txt")
_OLLAMA_WARM_TTL = 60  # seconds


def is_ollama_available() -> bool:
    """Check if Ollama is configured and reachable.

    Returns:
        True if Ollama responds to /api/tags (via configured URL or localhost:11434 fallback)
    """
    import time

    config = get_config()
    # If OLLAMA_BASE_URL is not set, fall back to the default local address.
    # Ollama's default install doesn't require setting the env var, so auto-detect.
    # During tests, require explicit config to avoid real network calls.
    if not config.ollama_base_url and os.getenv("PYTEST_CURRENT_TEST"):
        return False
    ollama_url = config.ollama_base_url or "http://localhost:11434"

    now = time.monotonic()

    # Check cache keyed on the effective URL (may be localhost fallback)
    if ollama_url in _ollama_cache:
        cached_result, cached_time = _ollama_cache[ollama_url]
        if (now - cached_time) < _OLLAMA_CACHE_TTL:
            return cached_result

    # Probe Ollama with connection timeout to prevent indefinite hangs.
    # On network errors or timeouts, conservatively assume Ollama is unavailable
    # but cache the result so we don't retry on every request.
    result = False
    try:
        import json
        import socket
        import urllib.request

        # Extract host:port from the URL to validate before attempting connection
        from urllib.parse import urlparse
        parsed = urlparse(ollama_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 11434

        # Quick socket check (more reliable timeout than urlopen)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)  # 1-second socket timeout
        sock.connect((host, port))
        sock.close()

        # Socket connected, now try the actual HTTP call
        with urllib.request.urlopen(
            f"{ollama_url}/api/tags",
            timeout=2
        ) as resp:
            data = json.loads(resp.read())
            result = True
            # Write discovery cache with actual model names
            _update_discovery_cache(data.get("models", []))
    except socket.timeout:
        log.debug("Ollama connection timeout: %s", ollama_url)
        result = False
    except (socket.gaierror, socket.error, ConnectionRefusedError, TimeoutError) as e:
        log.debug("Ollama connection failed (expected if not running): %s — %s", ollama_url, type(e).__name__)
        result = False
    except Exception as e:
        log.debug("Ollama probe failed: %s", e)
        result = False

    _ollama_cache[ollama_url] = (result, now)
    return result


# Substrings that mark an Ollama model as embedding-only (cannot generate text).
# Covers the common families: nomic-embed-text, mxbai-embed-large, snowflake-
# arctic-embed, bge-*, gte-*, e5-*, all-minilm.
_EMBEDDING_NAME_MARKERS: tuple[str, ...] = (
    "embed", "bge-", "-bge", "gte-", "e5-", "minilm",
)


def _is_embedding_model(name: str, meta: dict | None = None) -> bool:
    """True when an Ollama model is embedding-only (no text generation).

    Detected by the conventional name markers above and, when the Ollama
    ``/api/tags`` response includes it, an embedding-family hint under
    ``details.family`` (e.g. ``bert``/``nomic-bert``). Fails safe: unknown
    models are treated as generation-capable, never silently dropped.
    """
    low = name.lower()
    if any(marker in low for marker in _EMBEDDING_NAME_MARKERS):
        return True
    family = ((meta or {}).get("details") or {}).get("family", "")
    if isinstance(family, str) and ("bert" in family.lower() or "embed" in family.lower()):
        return True
    return False


def _update_discovery_cache(ollama_models: list[dict]) -> None:
    """Write discovered Ollama models to ~/.llm-router/discovery.json.

    Called on successful Ollama probe so the cache stays fresh.
    """
    import json
    import time

    models = {}
    for m in ollama_models:
        name = m.get("name", "")
        if not name:
            continue
        # Lever ②: embedding-only models (nomic-embed-text, mxbai-embed-large,
        # bge-*, etc.) cannot generate text — Ollama rejects /api/generate for
        # them ("does not support generate"). Tagging them generation-capable
        # here put them into routing chains, where they always errored and, on
        # premium/complex routes, contributed to chain exhaustion. Skip them at
        # the source so they never enter a generation chain.
        if _is_embedding_model(name, m):
            continue
        model_id = f"ollama/{name}"
        models[model_id] = {
            "model_id": model_id,
            "provider": "ollama",
            "provider_tier": "local",
            "task_types": ["query", "generate", "analyze", "code"],
        }

    cache_data = {
        "cached_at": time.time(),
        "models": models,
    }

    try:
        os.makedirs(os.path.dirname(_DISCOVERY_CACHE), exist_ok=True)
        with open(_DISCOVERY_CACHE, "w") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        log.debug("Failed to write discovery cache: %s", e)


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
    if config.moonshot_api_key:
        providers.add("moonshot")
    if config.minimax_api_key:
        providers.add("minimax")
    if config.zhipu_api_key:
        providers.add("zhipu")
    if config.arcee_api_key:
        providers.add("arcee")

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
        # If in subscription mode, exclude anthropic models from the fallback too
        # so that we don't accidentally call the paid API.
        config = get_config()
        if config.llm_router_claude_subscription:
            filtered = [m for m in static_chain if not m.startswith("anthropic/")]
            if filtered:
                return filtered

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


def _probe_ollama_direct(base_url: str = "http://localhost:11434") -> list[str]:
    """Probe Ollama /api/tags directly and return installed model IDs.

    Used as a fallback when OLLAMA_BASE_URL is not set but Ollama may still
    be running at the default address. Updates the discovery cache on success.
    """
    import json
    import socket
    import urllib.request
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 11434
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect((host, port))
        sock.close()
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2) as resp:
            data = json.loads(resp.read())
        models = data.get("models", [])
        _update_discovery_cache(models)
        return [
            f"ollama/{m['name']}" for m in models
            if m.get("name") and not _is_embedding_model(m["name"], m)
        ]
    except Exception:
        return []


def get_cached_ollama_models() -> list[str]:
    """Get cached list of Ollama models (from discovery cache file).

    Installed Ollama models change only when the user runs ``ollama pull``
    or ``ollama rm`` — not per-session — so we use a 24-hour TTL instead of
    the 1-hour default used by provider-API discovery.

    Returns:
        List of ollama/model-name strings from cache, or empty list if no cache.
    """
    # 86400s = 24h — conservative for installed model list (changes require explicit pull/rm)
    cached = _load_cache(ttl=86400)
    if cached:
        return [
            m_id for m_id, m_data in cached.items()
            if m_data.get("provider") == "ollama"
            # Lever ②: exclude embedding-only models even from a STALE cache
            # written before this filter existed (24h TTL) — they can never
            # generate, so they must never reach a routing chain.
            and not _is_embedding_model(m_id.split("/", 1)[-1], m_data)
        ]

    # Cache empty or stale — try a live probe to refresh it.
    # First check if OLLAMA_BASE_URL is set; otherwise try localhost:11434 directly.
    if is_ollama_available():
        cached = _load_cache(ttl=86400)
        if cached:
            return [
                m_id for m_id, m_data in cached.items()
                if m_data.get("provider") == "ollama"
            ]

    # Fallback: probe localhost:11434 even if OLLAMA_BASE_URL is not configured.
    # This handles the common case where Ollama is running but the env var was
    # never explicitly set (e.g. default install, no .env configuration).
    # Skip during tests to avoid real network calls that break isolation.
    if not os.getenv("PYTEST_CURRENT_TEST"):
        models = _probe_ollama_direct()
        if models:
            log.debug("Ollama discovered via default localhost probe: %s", models)
            return models

    return []


def is_ollama_warm() -> bool:
    """Return True if Ollama responded successfully within the last 60 seconds.

    Reads ~/.llm-router/ollama_last_ok.txt (a plain Unix timestamp written by
    mark_ollama_ok() after each successful call). No filesystem read needed
    during normal Ollama operation once warm — just a stat + read.
    """
    import time

    try:
        with open(_OLLAMA_LAST_OK) as f:
            last_ok = float(f.read().strip())
        return (time.time() - last_ok) < _OLLAMA_WARM_TTL
    except (FileNotFoundError, ValueError, OSError):
        return False


def mark_ollama_ok() -> None:
    """Record a successful Ollama call timestamp for warm-path routing."""
    import time

    try:
        os.makedirs(os.path.dirname(_OLLAMA_LAST_OK), exist_ok=True)
        with open(_OLLAMA_LAST_OK, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def filter_ollama_by_installed(chain: list[str]) -> list[str]:
    """Remove Ollama model entries whose model isn't in the installed cache.

    Ollama provider availability (is_ollama_available) only confirms the
    daemon is running — it doesn't mean every model in the routing chain
    is installed.  This function cross-checks each ``ollama/*`` entry
    against the discovery cache and drops models that aren't installed,
    preventing 50-second LiteLLM hangs on missing models.

    Non-Ollama entries pass through unchanged.
    """
    installed = set(get_cached_ollama_models())
    if not installed:
        # Cache empty — can't validate, let the chain through unmodified.
        return chain

    result = []
    for model in chain:
        if model.startswith("ollama/") and model not in installed:
            log.debug("Dropping %s — not installed in Ollama", model)
            continue
        result.append(model)
    return result
