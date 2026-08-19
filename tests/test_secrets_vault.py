"""Tests for the pluggable secrets-vault interface (#52).

Proves: the env default is backward-compatible, a real vault backend can
be registered + selected without touching callers, and every path fails
OPEN to env so a vault outage never breaks provider-key resolution.
"""
from __future__ import annotations

import pytest

from llm_router import secrets_vault as sv


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_ROUTER_SECRETS_BACKEND", raising=False)
    sv.reset_vault_for_tests()
    # Snapshot + restore the backend registry so custom test backends don't leak.
    saved = dict(sv._BACKENDS)
    yield
    sv._BACKENDS.clear()
    sv._BACKENDS.update(saved)
    sv.reset_vault_for_tests()


def test_env_backend_reads_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "  sk-abc  ")
    assert sv.EnvSecretsVault().get_provider_key("openai") == "sk-abc"


def test_env_backend_unknown_provider_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")  # whitespace → None
    v = sv.EnvSecretsVault()
    assert v.get_provider_key("anthropic") is None
    assert v.get_provider_key("nonesuch") is None


def test_get_provider_key_default_is_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert sv.get_provider_key("gemini") == "g-key"


def test_register_and_select_custom_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeVault:
        def get_provider_key(self, provider: str):
            return f"vault:{provider}"

    sv.register_backend("fake", FakeVault)
    monkeypatch.setenv("LLM_ROUTER_SECRETS_BACKEND", "fake")
    sv.reset_vault_for_tests()
    assert isinstance(sv.get_vault(), FakeVault)
    assert sv.get_provider_key("openai") == "vault:openai"


def test_unknown_backend_falls_open_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SECRETS_BACKEND", "does-not-exist")
    monkeypatch.setenv("OPENAI_API_KEY", "env-fallback")
    sv.reset_vault_for_tests()
    assert isinstance(sv.get_vault(), sv.EnvSecretsVault)
    assert sv.get_provider_key("openai") == "env-fallback"


def test_backend_exception_fails_open_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomVault:
        def get_provider_key(self, provider: str):
            raise RuntimeError("vault unreachable")

    sv.register_backend("boom", BoomVault)
    monkeypatch.setenv("LLM_ROUTER_SECRETS_BACKEND", "boom")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic")
    sv.reset_vault_for_tests()
    # get_provider_key must NOT raise — degrades to env.
    assert sv.get_provider_key("anthropic") == "env-anthropic"


def test_config_seam_delegates_to_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router.config import get_config

    monkeypatch.setenv("OPENAI_API_KEY", "cfg-key")
    sv.reset_vault_for_tests()
    assert get_config().provider_api_key("openai") == "cfg-key"


def test_perplexity_env_name_matches_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: must be PERPLEXITYAI_API_KEY (config._PROVIDER_MAP), not PERPLEXITY_API_KEY.
    assert sv.PROVIDER_ENV["perplexity"] == "PERPLEXITYAI_API_KEY"
    monkeypatch.setenv("PERPLEXITYAI_API_KEY", "pplx")
    assert sv.get_provider_key("perplexity") == "pplx"
