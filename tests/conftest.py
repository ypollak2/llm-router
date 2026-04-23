"""Shared pytest fixtures for all llm-router tests."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide a temporary database for tests.
    
    Sets up a clean SQLite database in a temp directory and ensures
    all config reads the temp path, not the user's real ~/.llm-router.
    
    CRITICAL: This fixture MUST be used by any test that writes to the database
    (including log_claude_usage, log_routing_decision, etc.). Failure to use this
    fixture will contaminate the production database.
    """
    db_dir = tmp_path / ".llm-router"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "test_usage.db"
    
    # Set env vars for config to pick up
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    
    # Reset singleton so config reads the new env vars
    import llm_router.config as config_module
    config_module._config = None
    
    # Verify isolation: make sure we're NOT using production path
    from llm_router.config import get_config
    config = get_config()
    assert str(config.llm_router_db_path) != str(Path.home() / ".llm-router" / "usage.db"), \
        f"CRITICAL: Fixture failed to isolate database. Using production path: {config.llm_router_db_path}"
    assert "test" in str(db_path).lower(), \
        f"CRITICAL: Database path should contain 'test': {db_path}"
    
    yield db_path
    
    # Cleanup: verify the isolated database was actually used (has non-zero size)
    if db_path.exists():
        assert db_path.stat().st_size > 0, f"Test database was never written to: {db_path}"


@pytest.fixture
def temp_router_dir(tmp_path, monkeypatch):
    """Provide a temporary router config directory.

    Patches module-level variables to use a temp directory for tests.
    """
    temp_home = tmp_path
    router_dir = temp_home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)

    # Patch module-level variables that were evaluated at import time
    import llm_router.hook_health
    monkeypatch.setattr(llm_router.hook_health, "_ROUTER_DIR", router_dir)
    monkeypatch.setattr(llm_router.hook_health, "_HOOK_HEALTH_FILE", router_dir / "hook_health.json")
    monkeypatch.setattr(llm_router.hook_health, "_HOOK_LOG_FILE", router_dir / "hook_errors.log")
    # Also patch Path.home for any runtime calls
    monkeypatch.setattr("pathlib.Path.home", lambda: temp_home)

    yield router_dir


@pytest.fixture
def temp_hooks_dir(tmp_path, monkeypatch):
    """Provide a temporary hooks directory.

    For tests that check hook permissions and execution.
    """
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    with patch("pathlib.Path.home", return_value=tmp_path):
        yield hooks_dir


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment for classification and routing tests."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    yield


@pytest.fixture
def minimal_env(monkeypatch):
    """Minimal environment with only one API key, for testing 'Recommended to Add' messages."""
    # Clear all API keys except one
    for key in ["OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY",
                "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
                "XAI_API_KEY", "COHERE_API_KEY", "OLLAMA_BASE_URL"]:
        monkeypatch.delenv(key, raising=False)

    # Set only one key to trigger "Recommended to Add"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    yield


@pytest.fixture
def no_providers_env(monkeypatch):
    """Create a truly empty config with no providers configured.

    This fixture mocks the config loader to return a RouterConfig with all
    API keys and Ollama disabled, regardless of local environment files.
    Used by tests that verify error handling when no providers are available.
    """
    # Create a manual config object without reading from env or .env
    from llm_router.types import QualityMode
    
    # Create a mock config with all providers disabled
    class EmptyConfig:
        openai_api_key = ""
        gemini_api_key = ""
        perplexity_api_key = ""
        anthropic_api_key = ""
        deepseek_api_key = ""
        groq_api_key = ""
        mistral_api_key = ""
        together_api_key = ""
        xai_api_key = ""
        cohere_api_key = ""
        ollama_base_url = ""
        llm_router_profile = "balanced"
        llm_router_claw_code = False
        llm_router_claude_subscription = False
        llm_router_enforce = "soft"
        llm_router_db_path = str(Path.home() / ".llm-router" / "routing.db")
        token_budget = 10_000_000
        quality = QualityMode.BALANCED
        min_model_floor = "haiku"
        semantic_cache_ttl = 86400
        health_circuit_breaker_threshold = 0.5
        health_circuit_breaker_ttl = 300
        health_request_timeout = 30
        
        def apply_keys_to_env(self):
            pass  # No-op
    
    empty_config = EmptyConfig()

    # Mock the get_config function to return our empty config
    import llm_router.config as config_module
    monkeypatch.setattr(config_module, "get_config", lambda: empty_config)

    # Also reset the singleton
    config_module._config = None

    yield empty_config


@pytest.fixture
def mock_acompletion():
    """Mock async completion for provider tests.
    
    Patches llm_router.providers.call_llm to return a mock LLM response,
    preventing actual API calls in tests. Also disables Codex injection
    and marks all providers as healthy to avoid skipping injected models.
    """
    from llm_router.types import LLMResponse

    mock_response = LLMResponse(
        content="Mock response",
        model="test/mock-model",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        latency_ms=100.0,
        provider="test",
    )

    async_mock = AsyncMock(return_value=mock_response)

    # Mock health tracker to mark all providers as healthy
    mock_tracker = MagicMock()
    mock_tracker.is_healthy.return_value = True

    with patch("llm_router.providers.call_llm", async_mock):
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.router.get_tracker", return_value=mock_tracker):
                yield async_mock


@pytest.fixture
def mock_litellm_response():
    """Factory for mock litellm completion responses (for tests patching litellm directly).
    
    Returns a mock object that mimics litellm.acompletion response with:
    - response.choices[0].message.content
    - response.usage.prompt_tokens / completion_tokens
    """
    def _make_response(content="Mock response", input_tokens=10, output_tokens=5, **kwargs):
        # Create mock litellm response structure
        # Accepts content, input_tokens, output_tokens as well as arbitrary kwargs
        mock_msg = MagicMock()
        mock_msg.content = content
        
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = input_tokens
        mock_usage.completion_tokens = output_tokens
        
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage
        
        return mock_response
    return _make_response


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """Reset config singleton before and after each test.

    Ensures that monkeypatched environment variables are picked up by get_config(),
    and prevents test pollution from config state changes.
    """
    import llm_router.config as config_module
    config_module._config = None
    yield
    config_module._config = None


@pytest.fixture(scope="session", autouse=True)
def _close_db_connections():
    """Force close all aiosqlite connections at end of test session.
    
    Prevents 'pytest is hanging on exit' due to unclosed async database connections.
    """
    yield
    # After all tests, force cleanup of aiosqlite connections
    try:
        import asyncio
        import gc
        
        # Close any pending event loops
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        
        if loop and not loop.is_closed():
            # Give any pending tasks a chance to finish
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
        
        # Force garbage collection to release aiosqlite threads
        gc.collect()
    except Exception:
        pass
