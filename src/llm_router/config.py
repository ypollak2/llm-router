"""Configuration loaded from environment variables.

Uses Pydantic Settings to load API keys and router preferences from
environment variables (and optionally a ``.env`` file). The configuration
is accessed via the ``get_config()`` singleton, which also calls
``apply_keys_to_env()`` on first load to export keys into ``os.environ``
where LiteLLM expects them.

Configuration is organized into five sections:
  1. **Text LLM providers** — API keys for OpenAI, Anthropic, Gemini, etc.
  2. **Media providers** — API keys for fal, Stability, ElevenLabs, etc.
  3. **Router settings** — profile, tier, budget, database path.
  4. **Smart routing** — token budget, quality mode, min model floor.
  5. **Health / defaults** — circuit breaker tuning, request defaults.
"""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path

from pydantic_settings import BaseSettings

from llm_router.types import QualityMode, RoutingProfile, Tier

# ── Ollama reachability cache ─────────────────────────────────────────────────
# Checked at most once per TTL to avoid a network call on every routing
# decision. Starts as None so the first call always does a live probe.
_ollama_reachable_cache: bool | None = None
_ollama_cache_time: float = 0.0
_OLLAMA_PROBE_TTL = 60.0  # seconds


def probe_ollama(base_url: str) -> bool:
    """Return True if Ollama is reachable, with a 60-second result cache.

    The result is cached to avoid a network probe on every call to
    ``available_providers``, which is invoked per routing request.
    Cache is invalidated after ``_OLLAMA_PROBE_TTL`` seconds so that a
    freshly-started Ollama process is detected within one minute.

    Args:
        base_url: Ollama base URL, e.g. ``"http://localhost:11434"``.

    Returns:
        True if Ollama responds to ``GET /api/tags`` within 1 second.
    """
    global _ollama_reachable_cache, _ollama_cache_time
    now = time.monotonic()
    if _ollama_reachable_cache is not None and (now - _ollama_cache_time) < _OLLAMA_PROBE_TTL:
        return _ollama_reachable_cache
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=1):
            _ollama_reachable_cache = True
    except Exception:
        _ollama_reachable_cache = False
    _ollama_cache_time = now
    return _ollama_reachable_cache


class RouterConfig(BaseSettings):
    """Central configuration for the LLM Router.

    All fields are loaded from environment variables (case-insensitive) or a
    ``.env`` file. Providers with empty API keys are considered unconfigured
    and excluded from routing.
    """

    # ── Text LLM providers ──
    openai_api_key: str = ""
    gemini_api_key: str = ""
    perplexity_api_key: str = ""
    anthropic_api_key: str = ""
    mistral_api_key: str = ""
    deepseek_api_key: str = ""
    groq_api_key: str = ""
    together_api_key: str = ""
    xai_api_key: str = ""
    cohere_api_key: str = ""

    # ── Claude Pro/Max subscription ──
    # Set to True when using llm-router inside Claude Code (Pro/Max subscription).
    # When enabled, all anthropic/* models are EXCLUDED from routing chains.
    # Rationale: you are already using Claude Code — routing back to Claude via
    # the Anthropic API would require a SEPARATE API key AND additional billing.
    # The router's job in this mode is to route tasks to non-Claude alternatives
    # (Codex, Ollama, Gemini, GPT-4o, Perplexity, etc.) to save your Claude quota.
    llm_router_claude_subscription: bool = False

    # ── Ollama (local inference — no API key needed) ──
    # Set ollama_base_url to enable Ollama as a task answerer (e.g. http://localhost:11434).
    # Ollama models are injected in two scenarios:
    #   1. BUDGET profile (simple tasks) — always prepended to the budget chain
    #      so they run first for free before falling back to cloud providers.
    #   2. ANY profile when Claude quota pressure >= 85% — prepended to spare
    #      subscription tokens regardless of complexity tier.
    # Note: OLLAMA_URL (used by hooks) is separate — it controls which model
    # classifies prompts locally. OLLAMA_BASE_URL here controls which models
    # ANSWER tasks. Both can be set for full local-first operation.
    # Example: ollama_budget_models="llama3.2,qwen2.5-coder:7b"
    ollama_base_url: str = ""               # empty = Ollama disabled
    ollama_budget_models: str = ""          # e.g. "llama3.2,qwen2.5-coder:7b"

    # ── Media providers ──
    fal_key: str = ""               # fal.ai — Flux, video, audio
    stability_api_key: str = ""     # Stability AI — Stable Diffusion
    elevenlabs_api_key: str = ""    # ElevenLabs — voice/TTS
    runway_api_key: str = ""        # Runway — video generation
    replicate_api_token: str = ""   # Replicate — various models

    # ── Router settings ──
    llm_router_profile: RoutingProfile = RoutingProfile.BALANCED
    llm_router_tier: Tier = Tier.FREE
    llm_router_db_path: Path = Path.home() / ".llm-router" / "usage.db"
    llm_router_monthly_budget: float = 20.0  # $20/month default cap
    llm_router_daily_spend_limit: float = 0.0  # 0 = disabled; >0 fires alert when crossed

    # ── Smart routing settings ──
    daily_token_budget: int = 500_000       # 500k tokens/day default cap
    quality_mode: QualityMode = QualityMode.BALANCED
    min_model: str = "haiku"                # floor: never route below this

    # ── Context injection settings ──
    context_enabled: bool = True          # inject session/history context into routed calls
    context_max_messages: int = 5         # max recent session messages to include
    context_max_previous_sessions: int = 3  # max past session summaries to include
    context_max_tokens: int = 1500        # token budget for all injected context

    # ── Compaction settings ──
    compaction_mode: str = "structural"  # off | structural | full
    compaction_threshold: int = 4000     # token threshold to trigger compaction

    # ── Prompt caching (Anthropic only) ──
    # Auto-injects cache_control breakpoints on long stable context (system prompts,
    # conversation history) to save up to 90% on repeated Anthropic API calls.
    # min_tokens: Anthropic requires ≥1024 for Sonnet/Opus, ≥2048 for Haiku.
    prompt_cache_enabled: bool = True
    prompt_cache_min_tokens: int = 1024

    # ── Health check settings ──
    health_failure_threshold: int = 2
    health_cooldown_seconds: int = 30

    # ── Request defaults ──
    default_max_tokens: int = 4096
    default_temperature: float = 0.7
    request_timeout: int = 120
    # Media generation (especially video) can take several minutes; separate
    # timeout prevents premature cancellation of long-running generation jobs.
    media_request_timeout: int = 600

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Maps each Pydantic field name to (provider_name, litellm_env_var).
    # This dual mapping serves two purposes:
    #   1. provider_name: used by available_providers to check which providers
    #      have keys configured.
    #   2. litellm_env_var: the specific env var name that LiteLLM expects
    #      (which sometimes differs from our field name, e.g.
    #      perplexity_api_key -> PERPLEXITYAI_API_KEY).
    _PROVIDER_MAP: dict[str, tuple[str, str]] = {
        "openai_api_key": ("openai", "OPENAI_API_KEY"),
        "gemini_api_key": ("gemini", "GEMINI_API_KEY"),
        "perplexity_api_key": ("perplexity", "PERPLEXITYAI_API_KEY"),
        "anthropic_api_key": ("anthropic", "ANTHROPIC_API_KEY"),
        "mistral_api_key": ("mistral", "MISTRAL_API_KEY"),
        "deepseek_api_key": ("deepseek", "DEEPSEEK_API_KEY"),
        "groq_api_key": ("groq", "GROQ_API_KEY"),
        "together_api_key": ("together", "TOGETHER_API_KEY"),
        "xai_api_key": ("xai", "XAI_API_KEY"),
        "cohere_api_key": ("cohere", "COHERE_API_KEY"),
        "fal_key": ("fal", "FAL_KEY"),
        "stability_api_key": ("stability", "STABILITY_API_KEY"),
        "elevenlabs_api_key": ("elevenlabs", "ELEVENLABS_API_KEY"),
        "runway_api_key": ("runway", "RUNWAY_API_KEY"),
        "replicate_api_token": ("replicate", "REPLICATE_API_TOKEN"),
    }

    @property
    def available_providers(self) -> set[str]:
        """Return the set of all providers that have a non-empty API key configured.

        This includes both text and media providers. Used by the router to
        filter the model chain to only models whose provider is available.

        Ollama is treated specially: it has no API key, so it is included
        whenever ``ollama_base_url`` is set.

        Returns:
            Set of provider name strings (e.g. ``{"openai", "anthropic", "fal"}``).
        """
        providers = set()
        for field_name, (provider_name, _) in self._PROVIDER_MAP.items():
            if getattr(self, field_name, ""):
                providers.add(provider_name)
        if self.ollama_base_url and probe_ollama(self.ollama_base_url):
            providers.add("ollama")
        # In Claude subscription mode, anthropic is intentionally excluded:
        # we never route back to Claude via API when already inside Claude Code.
        # Routing to anthropic/* would require a separate API key AND add duplicate
        # billing on top of the Pro/Max subscription — wrong in every scenario.
        if self.llm_router_claude_subscription:
            providers.discard("anthropic")
        return providers

    @property
    def text_providers(self) -> set[str]:
        """Return available providers that support text LLM completion.

        Note that OpenAI and Gemini appear in both text and media sets,
        since they offer both capabilities.

        Returns:
            Subset of ``available_providers`` that support text generation.
        """
        return self.available_providers & {
            "openai", "gemini", "perplexity", "anthropic",
            "mistral", "deepseek", "groq", "together", "xai", "cohere", "ollama",
        }

    @property
    def media_providers(self) -> set[str]:
        """Return available providers that support media generation (image/video/audio).

        Returns:
            Subset of ``available_providers`` that support media generation.
        """
        return self.available_providers & {
            "openai", "gemini", "fal", "stability", "elevenlabs", "runway", "replicate",
        }

    def ollama_models_for_profile(self, profile: "RoutingProfile") -> list[str]:
        """Return Ollama model IDs for the BUDGET profile (legacy behaviour).

        Kept for backward compatibility. Prefer ``all_ollama_models()`` when
        injecting Ollama under quota pressure regardless of profile.
        """
        if not self.ollama_base_url or profile != RoutingProfile.BUDGET:
            return []
        return self.all_ollama_models()

    def all_ollama_models(self) -> list[str]:
        """Return all configured Ollama model IDs regardless of routing profile.

        Used by the pressure-aware routing layer to inject local/free models
        when Claude subscription quota is running high (>= 85%).

        Returns:
            List of LiteLLM model IDs like ``["ollama/llama3.2", "ollama/qwen2.5-coder:7b"]``,
            or an empty list when Ollama is not configured.
        """
        if not self.ollama_base_url:
            return []
        return [f"ollama/{m.strip()}" for m in self.ollama_budget_models.split(",") if m.strip()]

    def apply_keys_to_env(self) -> None:
        """Export all configured API keys into ``os.environ``.

        LiteLLM reads API keys from environment variables at call time rather
        than accepting them as constructor arguments. This method bridges the
        gap by copying keys from our Pydantic config into the environment
        using the LiteLLM-expected variable names (from ``_PROVIDER_MAP``).

        In subscription mode (``llm_router_claude_subscription=True``),
        ``ANTHROPIC_API_KEY`` is intentionally NOT exported. This ensures
        LiteLLM cannot make Anthropic API calls even if an ``anthropic/*``
        model slips through any code path — a hard guarantee on top of the
        ``available_providers`` filter.

        Called automatically by ``get_config()`` on first load.
        """
        import os
        for field_name, (provider_name, env_var) in self._PROVIDER_MAP.items():
            value = getattr(self, field_name, "")
            if not value:
                continue
            # Never export the Anthropic key in subscription mode — prevents
            # LiteLLM from making direct Anthropic API calls that would incur
            # separate billing on top of the Claude Code subscription.
            if self.llm_router_claude_subscription and provider_name == "anthropic":
                continue
            os.environ[env_var] = value
        # LiteLLM reads Ollama's base URL from OLLAMA_API_BASE
        if self.ollama_base_url:
            os.environ.setdefault("OLLAMA_API_BASE", self.ollama_base_url)


_config: RouterConfig | None = None


def get_config() -> RouterConfig:
    """Return the singleton ``RouterConfig`` instance.

    On first call, loads configuration from environment variables / ``.env``
    and exports API keys into ``os.environ`` for LiteLLM. Subsequent calls
    return the cached instance.

    Returns:
        The global ``RouterConfig`` singleton.
    """
    global _config
    if _config is None:
        _config = RouterConfig()
        _config.apply_keys_to_env()
    return _config
