"""Shared pytest fixtures for all llm-router tests."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide a temporary database for tests.
    
    Sets up a clean SQLite database in a temp directory and ensures
    all config reads the temp path, not the user's real ~/.llm-router.
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
    
    return db_path


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
    def _make_response():
        # Create mock litellm response structure
        mock_msg = MagicMock()
        mock_msg.content = "Mock response"
        
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage
        
        return mock_response
    return _make_response


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
