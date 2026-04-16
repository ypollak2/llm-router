"""Shared pytest fixtures for all llm-router tests."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    """Mock async completion for provider tests."""
    return AsyncMock()
