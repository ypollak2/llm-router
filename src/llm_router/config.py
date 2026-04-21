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

import os as _os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Literal

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

    # ── claw-code mode ──
    # Set to True when running inside claw-code (open-source Claude alternative).
    # In claw-code every API call is paid — there is no subscription quota.
    # Effect: Ollama is injected at the front of ALL routing chains (not just
    # BUDGET or when Claude quota is high) so free local inference is always
    # tried first before spending money on cloud APIs.
    # Set automatically by `llm-router install` when ~/.claw-code/ is detected.
    llm_router_claw_code: bool = False

    # ── Ollama (local inference — no API key needed) ──
    # Set ollama_base_url to enable Ollama as a task answerer (e.g. http://localhost:11434).
    # When configured, Ollama models are ALWAYS prepended to the routing chain
    # regardless of profile or quota pressure — they are free and local, so there
    # is no reason to skip them. If a model can't answer the task it fails fast
    # and the chain falls through to paid APIs.
    # Note: OLLAMA_URL (used by hooks) is separate — it controls which model
    # classifies prompts locally. OLLAMA_BASE_URL here controls which models
    # ANSWER tasks. Both should be set for full local-first operation.
    # Example: ollama_budget_models="gemma4:latest,qwen3.5:latest"
    ollama_base_url: str = ""               # empty = Ollama disabled
    ollama_budget_models: str = ""          # comma-separated model names

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

    # ── Quota-balanced routing settings (v7.1.0) ──
    # Used by QUOTA_BALANCED profile to track Codex daily quota independently.
    codex_daily_limit: int = 1000           # Codex free tier = 1000 requests/day

    # ── Team Dashboard settings (v3.0) ──
    # llm_router_team_endpoint: webhook URL for push notifications.
    # Channel auto-detected: hooks.slack.com → Slack, discord.com → Discord,
    # api.telegram.org/bot* → Telegram, anything else → generic JSON POST.
    llm_router_team_endpoint: str = ""   # e.g. https://hooks.slack.com/...
    llm_router_user_id: str = ""         # override auto-detected git email
    llm_router_team_chat_id: str = ""    # Telegram chat_id (only for Telegram)

    # ── Digest settings (v3.3) ──
    # Separate from team_endpoint — digest goes to a different channel/webhook.
    # Falls back to llm_router_team_endpoint if not set.
    llm_router_webhook_url: str = ""     # LLM_ROUTER_WEBHOOK_URL

    # ── Tool slim mode (v4.0) ──
    # Reduce the number of registered MCP tools to save context tokens.
    # Values: "off" (all 41 tools), "routing" (12 tools), "core" (4 tools).
    # Set LLM_ROUTER_SLIM=routing in the MCP server env to activate.
    llm_router_slim: str = "off"         # LLM_ROUTER_SLIM

    # ── Cost-threshold escalation (v4.0) ──
    # Block any single call estimated above this cost until approved via
    # llm_approve_route. 0.0 = disabled (default). Example: 0.10 = $0.10/call cap.
    llm_router_escalate_above: float = 0.0   # LLM_ROUTER_ESCALATE_ABOVE (per-call USD)
    # Hard stop: cancel all calls once session spend exceeds this total.
    # 0.0 = disabled. Example: 1.0 = $1.00/session hard stop.
    llm_router_hard_stop_above: float = 0.0  # LLM_ROUTER_HARD_STOP_ABOVE (session USD)

    # ── HuggingFace Inference API ──
    # Used by the discovery layer to access free-tier hosted models.
    # Accepts HF_TOKEN or HUGGINGFACE_API_KEY from environment.
    huggingface_api_key: str = ""   # HF_TOKEN / HUGGINGFACE_API_KEY

    # ── Adaptive Universal Router settings (v5.0+) ──
    # Discovery cache TTL in seconds. After this window, available models are
    # re-scanned (Ollama list, HF API check, env var re-read). Default: 30 min.
    llm_router_discovery_ttl: int = 1800     # LLM_ROUTER_DISCOVERY_TTL

    # Benchmark data refresh interval in days. After this many days the cached
    # leaderboard scores are re-fetched in the background. Default: 7 days.
    llm_router_benchmark_ttl_days: int = 7   # LLM_ROUTER_BENCHMARK_TTL_DAYS

    # Per-provider monthly budget caps (USD). 0.0 = no cap (unlimited).
    # When a provider's tracked spend reaches its cap, budget_availability
    # drops to 0.0 and it sinks to the bottom of all routing chains automatically.
    llm_router_budget_openai: float = 0.0       # LLM_ROUTER_BUDGET_OPENAI
    llm_router_budget_gemini: float = 0.0       # LLM_ROUTER_BUDGET_GEMINI
    llm_router_budget_groq: float = 0.0         # LLM_ROUTER_BUDGET_GROQ
    llm_router_budget_deepseek: float = 0.0     # LLM_ROUTER_BUDGET_DEEPSEEK
    llm_router_budget_together: float = 0.0     # LLM_ROUTER_BUDGET_TOGETHER
    llm_router_budget_perplexity: float = 0.0   # LLM_ROUTER_BUDGET_PERPLEXITY
    llm_router_budget_mistral: float = 0.0      # LLM_ROUTER_BUDGET_MISTRAL

    # ── Enterprise integrations (v5.1) ──
    helicone_api_key: str = ""
    llm_router_helicone_pull: bool = False  # Pull spend from Helicone API
    llm_router_litellm_budget_db: str = ""  # Path to LiteLLM proxy budget DB
    # How to combine spend seen across multiple tracking systems:
    # "max" assumes sources overlap and keeps the highest single observed total.
    # "sum" treats sources as independent traffic channels and adds them together.
    llm_router_spend_aggregation: Literal["max", "sum"] = "max"

    # ── Community Benchmarks settings (v3.4) ──
    # Set to true to opt in to anonymous routing quality sharing (future upload).
    # In v3.4 this only prepares a local export file; upload requires a future
    # server endpoint to be ready.
    llm_router_community: bool = False   # LLM_ROUTER_COMMUNITY

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

    # ── Caveman mode (token-efficient output) ──
    # Caveman reduces output tokens by ~75% via structured terseness rules:
    # removes filler, uses fragments, preserves only technical substance.
    # Intensity: "off" | "lite" | "full" | "ultra" (default "full")
    caveman_mode: str = "full"  # off, lite, full, ultra

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
        "huggingface_api_key": ("huggingface", "HF_TOKEN"),
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
            "huggingface",
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

        Try live discovery cache first; fall back to OLLAMA_BUDGET_MODELS env var
        for backward compatibility when cache is empty or missing.

        Returns:
            List of LiteLLM model IDs like ``["ollama/llama3.2", "ollama/qwen2.5-coder:7b"]``,
            or an empty list when Ollama is not configured.
        """
        if not self.ollama_base_url:
            return []

        # Try live discovery cache first (Phase 1 of v5.0)
        try:
            from llm_router.discover import get_cached_ollama_models
            cached_models = get_cached_ollama_models()
            if cached_models:
                return cached_models
        except Exception:
            pass

        # Fall back to env var for backward compatibility
        return [f"ollama/{m.strip()}" for m in self.ollama_budget_models.split(",") if m.strip()]

    def model_post_init(self, __context: dict) -> None:
        """Load fallback configuration from ~/.llm-router/config.yaml if needed.

        This is called after Pydantic loads the config from .env / env vars.
        If .env is not readable (e.g. blocked by security team), we try to load
        from ~/.llm-router/config.yaml as a fallback. This allows enterprise
        users to configure Ollama and API keys without needing project-level
        .env access.

        Priority: .env (already loaded) → ~/.llm-router/config.yaml (fallback)
                  → env vars → defaults

        Fields that are still empty after .env load are filled from config.yaml.

        NOTE: Fallback loading is skipped in test mode to prevent test isolation
        issues. Tests explicitly configure RouterConfig via constructor params.
        """
        # Skip in test mode (pytest sets this env var)
        if _os.getenv("PYTEST_CURRENT_TEST"):
            return

        try:
            from llm_router.safe_config import load_safe_config
            safe_config_data = load_safe_config()
            if not safe_config_data:
                return

            # Only fill in fields that are still empty (don't override .env)
            for field_name, value in safe_config_data.items():
                if not value or not isinstance(value, (str, bool, int, float)):
                    continue
                current = getattr(self, field_name, None)
                # Only apply if current value is empty/False
                if not current:
                    try:
                        setattr(self, field_name, value)
                    except (ValueError, AttributeError):
                        pass  # Silently skip invalid fields
        except Exception:
            pass  # Silently fail — fallback config is optional

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
_config_lock = threading.Lock()


def get_config() -> RouterConfig:
    """Return the singleton ``RouterConfig`` instance.

    On first call, loads configuration from environment variables / ``.env``
    and exports API keys into ``os.environ`` for LiteLLM. Subsequent calls
    return the cached instance.

    In subscription mode the Anthropic key is actively removed from
    ``os.environ`` on every call — not just skipped during init — so that
    a pre-existing ``ANTHROPIC_API_KEY`` (e.g. set before the server started)
    cannot slip through to LiteLLM.

    Returns:
        The global ``RouterConfig`` singleton.
    """
    import os as _os
    global _config
    # Thread-safe singleton initialization using double-checked locking pattern
    if _config is None:
        with _config_lock:
            # Double-check inside lock to prevent race condition
            if _config is None:
                _config = RouterConfig()
                _config.apply_keys_to_env()
    # Active purge: remove ANTHROPIC_API_KEY from the live environment every
    # time get_config() is called in subscription mode. This handles the case
    # where the key was already present before the server started.
    if _config.llm_router_claude_subscription:
        _os.environ.pop("ANTHROPIC_API_KEY", None)
    return _config
