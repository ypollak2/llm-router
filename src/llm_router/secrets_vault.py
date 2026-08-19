"""Pluggable provider API-key lookup with fail-open env fallback."""

from __future__ import annotations

import os
from typing import Callable, Protocol

from llm_router.logging import get_logger

log = get_logger("llm_router.secrets_vault")

PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    # NB: LiteLLM expects PERPLEXITYAI_API_KEY (matches config._PROVIDER_MAP).
    "perplexity": "PERPLEXITYAI_API_KEY",
}


class SecretsVault(Protocol):
    """Provider-key lookup interface for env and future real vault backends."""

    def get_provider_key(self, provider: str) -> str | None:
        """Return the API key for *provider*, or None if unavailable."""


class EnvSecretsVault:
    """Backward-compatible backend that reads provider API keys from env."""

    def get_provider_key(self, provider: str) -> str | None:
        env_name = PROVIDER_ENV.get(provider)
        if env_name is None:
            return None
        value = os.environ.get(env_name)
        if value is None:
            return None
        value = value.strip()
        return value or None


_BackendFactory = Callable[[], SecretsVault]

_BACKENDS: dict[str, _BackendFactory] = {"env": EnvSecretsVault}
_vault: SecretsVault | None = None


def register_backend(name: str, factory: _BackendFactory) -> None:
    """Register a zero-arg vault factory; real vault plugins call this at import."""
    _BACKENDS[name] = factory


def get_vault() -> SecretsVault:
    """Return the configured vault, falling open to env for unknown backends."""
    global _vault
    if _vault is not None:
        return _vault

    backend_name = (os.environ.get("LLM_ROUTER_SECRETS_BACKEND") or "env").strip() or "env"
    factory = _BACKENDS.get(backend_name)
    if factory is None:
        log.warning("secrets_vault_backend_unknown", backend=backend_name)
        factory = EnvSecretsVault

    _vault = factory()
    return _vault


def get_provider_key(provider: str) -> str | None:
    """Resolve a provider key, never raising; backend failures fall open to env."""
    try:
        return get_vault().get_provider_key(provider)
    except Exception as exc:
        log.warning("secrets_vault_lookup_failed", provider=provider, error=str(exc))
        return EnvSecretsVault().get_provider_key(provider)


def reset_vault_for_tests() -> None:
    """Clear the cached vault singleton for tests."""
    global _vault
    _vault = None


__all__ = [
    "SecretsVault",
    "EnvSecretsVault",
    "PROVIDER_ENV",
    "register_backend",
    "get_vault",
    "get_provider_key",
    "reset_vault_for_tests",
]
