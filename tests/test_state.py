"""Tests for state.py — thread-safe shared mutable state."""

from __future__ import annotations

import threading

import pytest

from llm_router import state


@pytest.fixture(autouse=True)
def reset_state():
    """Reset all state globals before each test."""
    state.set_active_profile(None)
    state.set_last_usage(None)
    state.set_active_agent(None)
    yield
    state.set_active_profile(None)
    state.set_last_usage(None)
    state.set_active_agent(None)


class TestActiveProfile:
    def test_default_returns_config_profile(self):
        assert state.get_active_profile() is not None  # comes from config default

    def test_set_and_get(self):
        from llm_router.types import RoutingProfile
        state.set_active_profile(RoutingProfile.PREMIUM)
        assert state.get_active_profile() == RoutingProfile.PREMIUM

    def test_clear_reverts_to_config(self):
        from llm_router.types import RoutingProfile
        state.set_active_profile(RoutingProfile.PREMIUM)
        state.set_active_profile(None)
        assert state.get_active_profile() is not None  # back to config default

    def test_concurrent_set_no_corruption(self):
        """100 concurrent threads setting different profiles must not corrupt state."""
        from llm_router.types import RoutingProfile
        errors: list[str] = []
        profiles = [RoutingProfile.BUDGET, RoutingProfile.BALANCED, RoutingProfile.PREMIUM]

        def setter(p):
            try:
                state.set_active_profile(p)
                result = state.get_active_profile()
                if result not in profiles:
                    errors.append(f"Unexpected value: {result!r}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=setter, args=(profiles[i % 3],))
            for i in range(100)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"


class TestLastUsage:
    def test_returns_none_when_unset(self):
        assert state.get_last_usage() is None

    def test_set_and_get(self):
        from unittest.mock import MagicMock
        usage = MagicMock()
        usage.session_pct = 50.0
        state.set_last_usage(usage)
        result = state.get_last_usage()
        assert result is not None
        assert result.session_pct == 50.0

    def test_returns_copy_not_reference(self):
        """Mutating the returned object must not affect the stored value."""
        from unittest.mock import MagicMock
        usage = MagicMock()
        usage.session_pct = 10.0
        state.set_last_usage(usage)

        copy1 = state.get_last_usage()
        copy2 = state.get_last_usage()
        # Two calls must return independent copies
        assert copy1 is not copy2


class TestActiveAgent:
    def test_default_is_none(self):
        assert state.get_active_agent() is None

    def test_set_and_get(self):
        state.set_active_agent("claude_code")
        assert state.get_active_agent() == "claude_code"

    def test_clear(self):
        state.set_active_agent("codex")
        state.set_active_agent(None)
        assert state.get_active_agent() is None

    def test_concurrent_set_no_corruption(self):
        """Concurrent agent sets must not produce None or garbage values."""
        agents = ["claude_code", "codex", None]
        errors: list[str] = []

        def setter(a):
            try:
                state.set_active_agent(a)
                result = state.get_active_agent()
                if result not in agents:
                    errors.append(f"Unexpected agent: {result!r}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=setter, args=(agents[i % 3],))
            for i in range(100)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
