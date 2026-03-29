"""Configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

from llm_router.types import QualityMode, RoutingProfile, Tier


class RouterConfig(BaseSettings):
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
    llm_router_monthly_budget: float = 0.0  # 0 = unlimited

    # ── Smart routing settings ──
    daily_token_budget: int = 0             # 0 = unlimited, e.g. 1000000
    quality_mode: QualityMode = QualityMode.BALANCED
    min_model: str = "haiku"                # floor: never route below this

    # ── Health check settings ──
    health_failure_threshold: int = 3
    health_cooldown_seconds: int = 60

    # ── Request defaults ──
    default_max_tokens: int = 4096
    default_temperature: float = 0.7
    request_timeout: int = 120

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Map of env var field name → (provider name, LiteLLM env var name)
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
        providers = set()
        for field_name, (provider_name, _) in self._PROVIDER_MAP.items():
            if getattr(self, field_name, ""):
                providers.add(provider_name)
        return providers

    @property
    def text_providers(self) -> set[str]:
        return self.available_providers & {
            "openai", "gemini", "perplexity", "anthropic",
            "mistral", "deepseek", "groq", "together", "xai", "cohere",
        }

    @property
    def media_providers(self) -> set[str]:
        return self.available_providers & {
            "openai", "fal", "stability", "elevenlabs", "runway", "replicate",
        }

    def apply_keys_to_env(self) -> None:
        """Set API keys as environment variables for LiteLLM."""
        import os
        for field_name, (_, env_var) in self._PROVIDER_MAP.items():
            value = getattr(self, field_name, "")
            if value:
                os.environ[env_var] = value


_config: RouterConfig | None = None


def get_config() -> RouterConfig:
    global _config
    if _config is None:
        _config = RouterConfig()
        _config.apply_keys_to_env()
    return _config
