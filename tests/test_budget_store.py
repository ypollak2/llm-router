"""Tests for budget_store.py — persistent budget cap storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_router.budget_store import (
    get_cap,
    get_caps,
    list_caps,
    remove_cap,
    set_cap,
    _write_atomic,
    _BUDGETS_FILE,
)


@pytest.fixture(autouse=True)
def isolated_budgets(tmp_path, monkeypatch):
    """Redirect budgets.json to a tmp path for every test."""
    fake_file = tmp_path / "budgets.json"
    monkeypatch.setattr("llm_router.budget_store._BUDGETS_FILE", fake_file)
    monkeypatch.setattr("llm_router.budget_store._ROUTER_DIR", tmp_path)
    yield fake_file


# ── get_caps ──────────────────────────────────────────────────────────────────

class TestGetCaps:
    def test_returns_empty_when_missing(self):
        assert get_caps() == {}

    def test_returns_stored_caps(self, isolated_budgets):
        isolated_budgets.write_text(json.dumps({"openai": 20.0, "gemini": 5.0}))
        caps = get_caps()
        assert caps["openai"] == pytest.approx(20.0)
        assert caps["gemini"] == pytest.approx(5.0)

    def test_ignores_zero_and_negative(self, isolated_budgets):
        isolated_budgets.write_text(json.dumps({"openai": 0.0, "gemini": -1.0, "groq": 10.0}))
        caps = get_caps()
        assert "openai" not in caps
        assert "gemini" not in caps
        assert caps["groq"] == pytest.approx(10.0)

    def test_returns_empty_on_corrupt_json(self, isolated_budgets):
        isolated_budgets.write_text("not valid json {{{")
        assert get_caps() == {}


# ── set_cap ───────────────────────────────────────────────────────────────────

class TestSetCap:
    def test_persists_cap(self):
        set_cap("openai", 20.0)
        assert get_cap("openai") == pytest.approx(20.0)

    def test_overwrites_existing_cap(self):
        set_cap("openai", 10.0)
        set_cap("openai", 25.0)
        assert get_cap("openai") == pytest.approx(25.0)

    def test_raises_on_zero(self):
        with pytest.raises(ValueError, match="must be > 0"):
            set_cap("openai", 0.0)

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="must be > 0"):
            set_cap("openai", -5.0)

    def test_multiple_providers_independent(self):
        set_cap("openai", 20.0)
        set_cap("gemini", 5.0)
        set_cap("deepseek", 3.0)
        caps = get_caps()
        assert len(caps) == 3
        assert caps["openai"] == pytest.approx(20.0)
        assert caps["gemini"] == pytest.approx(5.0)

    def test_file_is_valid_json_after_write(self, isolated_budgets):
        set_cap("openai", 20.0)
        data = json.loads(isolated_budgets.read_text())
        assert data["openai"] == pytest.approx(20.0)

    def test_atomic_write_uses_tmp_file(self, tmp_path):
        """Verify atomic write: tmp file is cleaned up after rename."""
        caps = {"openai": 20.0}
        target = tmp_path / "budgets.json"
        with patch("llm_router.budget_store._BUDGETS_FILE", target), \
             patch("llm_router.budget_store._ROUTER_DIR", tmp_path):
            _write_atomic(caps)
        assert target.exists()
        tmp = target.with_suffix(".json.tmp")
        assert not tmp.exists(), "tmp file should be cleaned up after atomic replace"


# ── remove_cap ────────────────────────────────────────────────────────────────

class TestRemoveCap:
    def test_removes_existing_cap(self):
        set_cap("openai", 20.0)
        result = remove_cap("openai")
        assert result is True
        assert get_cap("openai") == pytest.approx(0.0)

    def test_returns_false_when_not_set(self):
        result = remove_cap("nonexistent_provider")
        assert result is False

    def test_other_caps_unaffected(self):
        set_cap("openai", 20.0)
        set_cap("gemini", 5.0)
        remove_cap("openai")
        assert get_cap("gemini") == pytest.approx(5.0)
        assert get_cap("openai") == pytest.approx(0.0)


# ── get_cap ───────────────────────────────────────────────────────────────────

class TestGetCap:
    def test_returns_zero_when_not_set(self):
        assert get_cap("openai") == pytest.approx(0.0)

    def test_returns_stored_value(self):
        set_cap("openai", 15.0)
        assert get_cap("openai") == pytest.approx(15.0)


# ── budget.py integration — store takes priority over env var ─────────────────

class TestBudgetIntegration:
    def test_store_cap_overrides_env_var(self, monkeypatch):
        """budget_store cap must take priority over LLM_ROUTER_BUDGET_OPENAI env var."""
        monkeypatch.setenv("LLM_ROUTER_BUDGET_OPENAI", "5.0")
        set_cap("openai", 30.0)

        from llm_router.budget import _get_cap
        from llm_router.config import get_config
        import llm_router.config as _cfg
        _cfg._config = None  # force reload
        cfg = get_config()
        result = _get_cap("openai", cfg)
        assert result == pytest.approx(30.0), "budget_store should override env var"

    def test_env_var_used_when_no_store_cap(self, monkeypatch):
        """When no budget_store cap, env var cap should be returned."""
        monkeypatch.setenv("LLM_ROUTER_BUDGET_OPENAI", "7.5")
        from llm_router.budget import _get_cap
        from llm_router.config import get_config
        import llm_router.config as _cfg
        _cfg._config = None
        cfg = get_config()
        result = _get_cap("openai", cfg)
        assert result == pytest.approx(7.5)
