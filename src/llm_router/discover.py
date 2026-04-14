"""Universal Model Discovery — scan all available models for this user.

Discovers what LLM models are reachable from this machine and encodes their
capability, cost tier, and quota *type* into :class:`~llm_router.types.ModelCapability`
records.  Results are cached to ``~/.llm-router/discovery.json`` with a
configurable TTL (default 30 minutes).

## Quota types (why they matter for routing)

Different providers impose limits in fundamentally different ways:

  ``"none"``            Local models — no monetary cost, no quota, always available.
  ``"session_window"``  Claude Pro 5h-session + weekly limits — pressure resets
                        every 5 hours (session) and every Monday (weekly). Use
                        the Budget Oracle for real-time state.
  ``"daily_rate"``      Groq free tier — tokens/min and tokens/day rate limits.
                        Pressure is transient; resets at midnight UTC and after
                        every per-minute window. Can go from 100% → 0% in minutes.
  ``"monthly_requests"``HuggingFace Inference API free tier — request quota resets
                        on the 1st of each month.
  ``"monthly_spend"``   API-key providers (OpenAI, Gemini, DeepSeek …) — dollar
                        spend tracked against an optional user-configured cap.
                        Resets at the provider's billing cycle start.

## Discovery sources

  1. Ollama     — ``GET /api/tags``; maps to LOCAL / free
  2. HuggingFace— ``HF_TOKEN`` env var; marks free-tier hosted models available
  3. API keys   — env var presence; marks paid providers configured
  4. Codex CLI  — ``codex --version`` binary check; marks Codex quota available
  5. Claude sub — ``usage.json`` snapshot; marks subscription active

## Model-to-benchmark alias registry

Local model names (``qwen3:32b``, ``gemma4:latest``) don't appear on leaderboards.
The alias registry maps model family + parameter count → canonical benchmark name
so the scorer can look up quality scores for local models.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path

from llm_router.config import get_config
from llm_router.types import ModelCapability, ProviderTier, TaskType, LOCAL_PROVIDERS

# ── Paths ─────────────────────────────────────────────────────────────────────
_ROUTER_DIR = Path.home() / ".llm-router"
_DISCOVERY_CACHE = _ROUTER_DIR / "discovery.json"
_USAGE_JSON = _ROUTER_DIR / "usage.json"

# ── Known-model registry ──────────────────────────────────────────────────────
# Maps Ollama model name patterns → (task_types, benchmark_alias).
# task_types: which task types this model is well-suited for.
# benchmark_alias: canonical name to look up in the benchmark registry.
_OLLAMA_MODEL_REGISTRY: list[tuple[str, frozenset[TaskType], str]] = [
    # (name_prefix_lower, task_types, benchmark_alias)
    ("qwen3-coder",   frozenset({TaskType.CODE, TaskType.ANALYZE}),         "qwen3-coder"),
    ("qwen3",         frozenset({TaskType.CODE, TaskType.ANALYZE,
                                  TaskType.QUERY, TaskType.GENERATE}),       "qwen3"),
    ("qwen2.5-coder", frozenset({TaskType.CODE, TaskType.ANALYZE}),          "qwen2.5-coder"),
    ("qwen2.5",       frozenset({TaskType.CODE, TaskType.QUERY,
                                  TaskType.GENERATE, TaskType.ANALYZE}),     "qwen2.5"),
    ("codestral",     frozenset({TaskType.CODE, TaskType.ANALYZE}),          "codestral"),
    ("deepseek-coder",frozenset({TaskType.CODE, TaskType.ANALYZE}),          "deepseek-coder"),
    ("deepseek",      frozenset({TaskType.CODE, TaskType.QUERY,
                                  TaskType.ANALYZE, TaskType.GENERATE}),     "deepseek-v3"),
    ("llama3",        frozenset({TaskType.QUERY, TaskType.GENERATE,
                                  TaskType.ANALYZE, TaskType.CODE}),         "llama-3"),
    ("llama",         frozenset({TaskType.QUERY, TaskType.GENERATE,
                                  TaskType.ANALYZE}),                        "llama"),
    ("gemma4",        frozenset({TaskType.QUERY, TaskType.GENERATE,
                                  TaskType.ANALYZE, TaskType.CODE}),         "gemma"),
    ("gemma",         frozenset({TaskType.QUERY, TaskType.GENERATE}),        "gemma"),
    ("mistral",       frozenset({TaskType.QUERY, TaskType.GENERATE,
                                  TaskType.CODE, TaskType.ANALYZE}),         "mistral"),
    ("phi4",          frozenset({TaskType.QUERY, TaskType.GENERATE,
                                  TaskType.CODE}),                           "phi-4"),
    ("phi",           frozenset({TaskType.QUERY, TaskType.GENERATE}),        "phi"),
    ("granite",       frozenset({TaskType.CODE, TaskType.ANALYZE}),          "granite"),
    ("command",       frozenset({TaskType.QUERY, TaskType.GENERATE,
                                  TaskType.ANALYZE}),                        "command-r"),
]

# Heuristic: parameter count → rough latency estimate (ms P50) on typical hardware.
_PARAM_LATENCY_MS: list[tuple[float, float]] = [
    (1.0,  80.0),   # ≤1B  → ~80ms
    (3.0,  200.0),  # ≤3B  → ~200ms
    (8.0,  400.0),  # ≤8B  → ~400ms
    (14.0, 700.0),  # ≤14B → ~700ms
    (32.0, 1400.0), # ≤32B → ~1.4s
    (70.0, 3000.0), # ≤70B → ~3s
    (float("inf"), 8000.0),  # >70B → ~8s
]

# HuggingFace free-tier models available via Inference API (curated subset).
# Full list changes frequently; this covers the most stable free-tier models.
_HF_FREE_MODELS: list[tuple[str, frozenset[TaskType], str]] = [
    ("HuggingFaceH4/zephyr-7b-beta",
     frozenset({TaskType.QUERY, TaskType.GENERATE}), "zephyr-7b"),
    ("mistralai/Mistral-7B-Instruct-v0.2",
     frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE}), "mistral-7b"),
    ("meta-llama/Meta-Llama-3-8B-Instruct",
     frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE}), "llama-3-8b"),
    ("google/gemma-2-2b-it",
     frozenset({TaskType.QUERY, TaskType.GENERATE}), "gemma-2-2b"),
    ("Qwen/Qwen2.5-7B-Instruct",
     frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE}), "qwen2.5-7b"),
    ("codellama/CodeLlama-34b-Instruct-hf",
     frozenset({TaskType.CODE, TaskType.ANALYZE}), "codellama-34b"),
]

# ── Public API ────────────────────────────────────────────────────────────────


async def discover_available_models(force: bool = False) -> dict[str, ModelCapability]:
    """Discover all models available to this user and return a capability map.

    Results are cached to ``~/.llm-router/discovery.json``.  Set ``force=True``
    to bypass the cache and re-scan everything.

    Args:
        force: Ignore cached results and re-run all scanners.

    Returns:
        Dict mapping model_id → :class:`~llm_router.types.ModelCapability`.
    """
    cfg = get_config()
    ttl = cfg.llm_router_discovery_ttl

    # Cache hit
    if not force:
        cached = _load_cache(ttl)
        if cached is not None:
            return cached

    # Parallel scan — all sources run concurrently
    results = await asyncio.gather(
        _scan_ollama(cfg),
        _scan_huggingface(cfg),
        _scan_api_key_providers(cfg),
        _scan_codex(),
        return_exceptions=True,
    )

    models: dict[str, ModelCapability] = {}
    for batch in results:
        if isinstance(batch, Exception):
            continue  # scanner failure is silent — skip that source
        for cap in batch:
            models[cap.model_id] = cap

    _save_cache(models)
    return models


def get_local_model_ids() -> list[str]:
    """Return model IDs from the cache that belong to local providers.

    Used by the chain builder to inject local models first without waiting
    for a full async discovery cycle.

    Returns:
        List of model_ids for all LOCAL_PROVIDERS, or empty list if no cache.
    """
    cached = _load_cache(ttl=float("inf"))  # any age is fine for this helper
    if cached is None:
        return []
    return [
        mid for mid, cap in cached.items()
        if cap.provider in LOCAL_PROVIDERS and cap.available
    ]


# ── Cache helpers ─────────────────────────────────────────────────────────────


def _load_cache(ttl: float) -> dict[str, ModelCapability] | None:
    """Load discovery cache from disk.  Returns None if missing or stale."""
    try:
        data = json.loads(_DISCOVERY_CACHE.read_text())
        if time.time() - data.get("cached_at", 0) > ttl:
            return None
        return {
            mid: _cap_from_dict(cap_dict)
            for mid, cap_dict in data.get("models", {}).items()
        }
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_cache(models: dict[str, ModelCapability]) -> None:
    """Persist discovery results to disk."""
    try:
        _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "cached_at": time.time(),
            "models": {mid: _cap_to_dict(cap) for mid, cap in models.items()},
        }
        _DISCOVERY_CACHE.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


def _cap_to_dict(cap: ModelCapability) -> dict:
    return {
        "model_id": cap.model_id,
        "provider": cap.provider,
        "provider_tier": cap.provider_tier.value,
        "task_types": [t.value for t in cap.task_types],
        "cost_per_1k": cap.cost_per_1k,
        "latency_p50_ms": cap.latency_p50_ms,
        "context_window": cap.context_window,
        "available": cap.available,
    }


def _cap_from_dict(d: dict) -> ModelCapability:
    return ModelCapability(
        model_id=d["model_id"],
        provider=d["provider"],
        provider_tier=ProviderTier(d["provider_tier"]),
        task_types=frozenset(TaskType(t) for t in d["task_types"]),
        cost_per_1k=d.get("cost_per_1k", 0.0),
        latency_p50_ms=d.get("latency_p50_ms", 0.0),
        context_window=d.get("context_window", 8192),
        available=d.get("available", True),
    )


# ── Scanners ──────────────────────────────────────────────────────────────────


async def _scan_ollama(cfg) -> list[ModelCapability]:
    """Scan Ollama for locally available models."""
    if not cfg.ollama_base_url:
        return []
    try:
        url = f"{cfg.ollama_base_url.rstrip('/')}/api/tags"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []

    models = []
    for entry in data.get("models", []):
        name: str = entry.get("name", "")
        if not name:
            continue
        details = entry.get("details", {})
        param_size_str: str = details.get("parameter_size", "")
        task_types, alias = _classify_ollama_model(name)
        latency = _estimate_latency(param_size_str)
        ctx = _estimate_context(details)

        models.append(ModelCapability(
            model_id=f"ollama/{name}",
            provider="ollama",
            provider_tier=ProviderTier.LOCAL,
            task_types=task_types,
            cost_per_1k=0.0,
            latency_p50_ms=latency,
            context_window=ctx,
            available=True,
        ))
    return models


async def _scan_huggingface(cfg) -> list[ModelCapability]:
    """Mark HuggingFace free-tier models as available when HF_TOKEN is set."""
    if not cfg.huggingface_api_key:
        return []
    # We trust the static list — verifying each model via HTTP would be slow
    # and the free-tier list changes rarely.
    return [
        ModelCapability(
            model_id=f"huggingface/{model_id}",
            provider="huggingface",
            provider_tier=ProviderTier.FREE_CLOUD,
            task_types=task_types,
            cost_per_1k=0.0,       # free tier (rate-limited, not dollar-billed)
            latency_p50_ms=2000.0, # HF Inference API typically ~2s cold, ~500ms warm
            context_window=8192,
            available=True,
        )
        for model_id, task_types, _ in _HF_FREE_MODELS
    ]


async def _scan_api_key_providers(cfg) -> list[ModelCapability]:
    """Create capability records for all configured API-key providers.

    Uses a curated list of the best model per provider × task type.
    These are the models the existing profiles.py chains use, so they
    are guaranteed to be wired up in LiteLLM.
    """
    # (provider, model_suffix, task_types, tier, cost_per_1k, latency_ms, ctx)
    _PROVIDER_MODELS: list[tuple] = [
        # OpenAI
        ("openai", "gpt-4o-mini",     frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE}),
         ProviderTier.CHEAP_PAID,  0.00015, 800,  128_000),
        ("openai", "gpt-4o",          frozenset({TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE}),
         ProviderTier.EXPENSIVE,   0.005,   1200, 128_000),
        ("openai", "o3-mini",         frozenset({TaskType.CODE, TaskType.ANALYZE}),
         ProviderTier.EXPENSIVE,   0.004,   3000, 200_000),
        # Gemini
        ("gemini", "gemini/gemini-2.0-flash-exp",
         frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE, TaskType.ANALYZE}),
         ProviderTier.CHEAP_PAID,  0.000075, 600, 1_000_000),
        ("gemini", "gemini/gemini-2.5-pro-exp-03-25",
         frozenset({TaskType.CODE, TaskType.ANALYZE, TaskType.RESEARCH}),
         ProviderTier.SUBSCRIPTION, 0.00125, 2000, 1_000_000),
        # Groq (free-tier rate limited — quota_type: daily_rate)
        ("groq",   "groq/llama-3.3-70b-versatile",
         frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE, TaskType.ANALYZE}),
         ProviderTier.FREE_CLOUD,  0.0,     400,  128_000),
        ("groq",   "groq/qwen-qwq-32b",
         frozenset({TaskType.CODE, TaskType.ANALYZE}),
         ProviderTier.FREE_CLOUD,  0.0,     800,  128_000),
        # DeepSeek
        ("deepseek", "deepseek/deepseek-chat",
         frozenset({TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE}),
         ProviderTier.CHEAP_PAID,  0.00028, 1000, 64_000),
        ("deepseek", "deepseek/deepseek-reasoner",
         frozenset({TaskType.CODE, TaskType.ANALYZE}),
         ProviderTier.CHEAP_PAID,  0.00055, 4000, 64_000),
        # Perplexity (research only)
        ("perplexity", "perplexity/sonar-pro",
         frozenset({TaskType.RESEARCH}),
         ProviderTier.CHEAP_PAID,  0.003,   1500, 200_000),
        # Mistral
        ("mistral", "mistral/mistral-large-latest",
         frozenset({TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE}),
         ProviderTier.CHEAP_PAID,  0.002,   900,  128_000),
        # Together
        ("together", "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
         frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE}),
         ProviderTier.CHEAP_PAID,  0.00088, 600,  128_000),
        # Cohere
        ("cohere", "cohere/command-r-plus",
         frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.ANALYZE}),
         ProviderTier.CHEAP_PAID,  0.0025,  1000, 128_000),
    ]

    available = cfg.available_providers
    result = []
    for provider, model_suffix, task_types, tier, cost, latency, ctx in _PROVIDER_MODELS:
        if provider not in available:
            continue
        # Use model_suffix as the full model_id when it already contains a prefix,
        # otherwise build "provider/model_suffix".
        model_id = model_suffix if "/" in model_suffix else f"{provider}/{model_suffix}"
        result.append(ModelCapability(
            model_id=model_id,
            provider=provider,
            provider_tier=tier,
            task_types=task_types,
            cost_per_1k=cost,
            latency_p50_ms=float(latency),
            context_window=ctx,
            available=True,
        ))
    return result


async def _scan_codex() -> list[ModelCapability]:
    """Check if Codex CLI is available and add its models."""
    try:
        from llm_router.codex_agent import is_codex_plugin_available
        if not is_codex_plugin_available():
            return []
    except Exception:
        return []

    # Codex CLI routes to OpenAI API under the hood (gpt-5.4 / o3)
    # but uses the user's Codex prepaid quota — treated as FREE_CLOUD tier.
    _CODEX_MODELS = [
        ("codex/gpt-5.4",  frozenset({TaskType.CODE, TaskType.ANALYZE, TaskType.QUERY}), 600),
        ("codex/o3",       frozenset({TaskType.CODE, TaskType.ANALYZE}),                 4000),
    ]
    return [
        ModelCapability(
            model_id=mid,
            provider="codex",
            provider_tier=ProviderTier.FREE_CLOUD,
            task_types=task_types,
            cost_per_1k=0.0,  # prepaid quota, not per-call billed
            latency_p50_ms=float(lat),
            context_window=200_000,
            available=True,
        )
        for mid, task_types, lat in _CODEX_MODELS
    ]


# ── Model classification helpers ──────────────────────────────────────────────


def _classify_ollama_model(name: str) -> tuple[frozenset[TaskType], str]:
    """Map an Ollama model name to task types and benchmark alias."""
    name_lower = name.lower().split(":")[0]  # strip tag (e.g. ":latest")
    for prefix, task_types, alias in _OLLAMA_MODEL_REGISTRY:
        if name_lower.startswith(prefix):
            return task_types, alias
    # Unknown model — assume general purpose
    return frozenset({TaskType.QUERY, TaskType.GENERATE, TaskType.CODE}), name_lower


def _estimate_latency(param_size_str: str) -> float:
    """Estimate P50 latency from parameter size string (e.g. ``"32B"``)."""
    try:
        # Strip non-numeric suffix (B, M, K) and convert
        s = param_size_str.upper().rstrip("B").rstrip("M").rstrip("K")
        params_b = float(s)
        if "M" in param_size_str.upper():
            params_b /= 1000.0
    except (ValueError, AttributeError):
        return 1500.0  # unknown — assume mid-range

    for threshold, latency in _PARAM_LATENCY_MS:
        if params_b <= threshold:
            return latency
    return 8000.0


def _estimate_context(details: dict) -> int:
    """Estimate context window from Ollama model details."""
    # Ollama doesn't expose context window in /api/tags; use family heuristics.
    family = (details.get("family") or "").lower()
    families = [f.lower() for f in (details.get("families") or [])]
    all_families = {family} | set(families)

    if any(f in all_families for f in ("qwen3", "qwen2.5", "llama3")):
        return 128_000
    if any(f in all_families for f in ("gemma4", "gemma")):
        return 128_000
    if "mistral" in all_families:
        return 32_000
    return 8_192


# ── Convenience: quota type description ──────────────────────────────────────


def quota_type_for(cap: ModelCapability) -> str:
    """Return a human-readable quota type string for *cap*.

    Used in dashboard output to explain why a model's availability changes.

    Returns one of: ``"none"``, ``"session_window"``, ``"daily_rate"``,
    ``"monthly_requests"``, ``"monthly_spend"``.
    """
    if cap.provider in LOCAL_PROVIDERS:
        return "none"
    if cap.provider == "anthropic":
        return "session_window"  # 5h session + weekly
    if cap.provider == "groq":
        return "daily_rate"       # tokens/day + requests/min
    if cap.provider == "huggingface":
        return "monthly_requests"  # HF free tier
    if cap.provider == "codex":
        return "monthly_spend"     # Codex prepaid quota
    return "monthly_spend"         # generic API key provider
