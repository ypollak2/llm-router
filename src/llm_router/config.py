"""Configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

from llm_router.types import RoutingProfile


class RouterConfig(BaseSettings):
    gemini_api_key: str = ""
    openai_api_key: str = ""
    perplexity_api_key: str = ""

    llm_router_profile: RoutingProfile = RoutingProfile.BALANCED
    llm_router_db_path: Path = Path.home() / ".llm-router" / "usage.db"

    # Health check settings
    health_failure_threshold: int = 3
    health_cooldown_seconds: int = 60

    # Request defaults
    default_max_tokens: int = 4096
    default_temperature: float = 0.7
    request_timeout: int = 120

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def available_providers(self) -> set[str]:
        providers = set()
        if self.gemini_api_key:
            providers.add("gemini")
        if self.openai_api_key:
            providers.add("openai")
        if self.perplexity_api_key:
            providers.add("perplexity")
        return providers

    def apply_keys_to_env(self) -> None:
        """Set API keys as environment variables for LiteLLM."""
        import os

        if self.gemini_api_key:
            os.environ["GEMINI_API_KEY"] = self.gemini_api_key
        if self.openai_api_key:
            os.environ["OPENAI_API_KEY"] = self.openai_api_key
        if self.perplexity_api_key:
            os.environ["PERPLEXITYAI_API_KEY"] = self.perplexity_api_key


_config: RouterConfig | None = None


def get_config() -> RouterConfig:
    global _config
    if _config is None:
        _config = RouterConfig()
        _config.apply_keys_to_env()
    return _config
