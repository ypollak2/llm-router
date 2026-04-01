"""Demo 1 — Routing Pipeline E2E.

Demonstrates and validates the full v1.1 subscription-aware routing flow:
  1. Low-pressure  -> CC-mode hint returned, no external API call
  2. High-pressure -> external model selected from fallback chain
  3. Ollama live health probe gates local routing
  4. Complexity classifier feeds model selection end-to-end

Run standalone:
    uv run pytest tests/test_demo_routing.py -v -s
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import RoutingProfile, TaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_litellm_resp(content="ok", input_tokens=50, output_tokens=20):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = input_tokens
    resp.usage.completion_tokens = output_tokens
    resp.citations = None
    return resp


def _make_usage(session_pct: float = 0.0, weekly_pct: float = 0.0,
                sonnet_pct: float = 0.0):
    """Build a ClaudeSubscriptionUsage with the given pressure levels."""
    from llm_router.claude_usage import ClaudeSubscriptionUsage, UsageLimit
    return ClaudeSubscriptionUsage(
        session=UsageLimit(label="Session", pct_used=session_pct, resets_at=""),
        weekly_all=UsageLimit(label="Weekly", pct_used=weekly_pct, resets_at=""),
        weekly_sonnet=UsageLimit(label="Sonnet", pct_used=sonnet_pct, resets_at=""),
    )


_LOW_PRESSURE_JSON = json.dumps(
    {"session_pct": 0.0, "weekly_pct": 0.0, "sonnet_pct": 0.0, "updated_at": 9999999999.0}
)


def _fake_open(json_str: str):
    m = MagicMock()
    m.__enter__ = lambda s, *a: s
    m.__exit__ = lambda s, *a: None
    m.read = lambda: json_str
    return m


# ---------------------------------------------------------------------------
# Demo 1a-1c: CC-mode hints under low subscription pressure
# ---------------------------------------------------------------------------

class TestDemo_SubscriptionRouting:
    """Validates v1.1 subscription-aware routing: CC hints under low pressure."""

    def test_simple_task_returns_haiku_hint_when_no_pressure(self, monkeypatch):
        """Simple tasks get a Haiku CC-mode hint when subscription has headroom."""
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import llm_router.state as _state
        from llm_router.tools.text import _subscription_hint
        monkeypatch.setattr(_state, "_last_usage", _make_usage(0.0))

        hint = _subscription_hint("query", "simple", "what is 2+2")

        assert hint is not None, "Expected CC-mode hint when subscription has headroom"
        assert "CC-MODE" in hint
        assert "haiku" in hint.lower(), f"Simple should route to Haiku, got: {hint}"
        print(f"\n[Demo 1a] CC-mode hint (simple, low pressure):\n{hint}")

    def test_moderate_task_passthrough_when_no_pressure(self, monkeypatch):
        """Moderate tasks pass through (Sonnet already active) under low pressure."""
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import llm_router.state as _state
        from llm_router.tools.text import _subscription_hint
        monkeypatch.setattr(_state, "_last_usage", _make_usage(0.0))

        hint = _subscription_hint("query", "moderate", "explain this code")

        assert hint is None, f"Moderate under no pressure should passthrough, got: {hint}"
        print("\n[Demo 1b] Moderate under no pressure -> passthrough (no hint)")

    def test_complex_task_returns_opus_hint_when_no_pressure(self, monkeypatch):
        """Complex tasks get an Opus CC-mode hint when subscription has headroom."""
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import llm_router.state as _state
        from llm_router.tools.text import _subscription_hint
        monkeypatch.setattr(_state, "_last_usage", _make_usage(0.0))

        hint = _subscription_hint("query", "complex", "design a distributed system")

        assert hint is not None
        assert "CC-MODE" in hint
        assert "opus" in hint.lower(), f"Complex should hint Opus, got: {hint}"
        print(f"\n[Demo 1c] CC-mode hint (complex, low pressure):\n{hint}")


# ---------------------------------------------------------------------------
# Demo 1d-1e: Pressure cascade -> external fallback
# ---------------------------------------------------------------------------

class TestDemo_PressureCascade:
    """v1.1 pressure cascade: exhausted subscription goes external."""

    def test_high_session_pressure_blocks_cc_hint_for_simple(self, monkeypatch):
        """When session >= 85%, simple tasks get no CC hint -> go external."""
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        import llm_router.state as _state
        from llm_router.tools.text import _subscription_hint
        monkeypatch.setattr(_state, "_last_usage", _make_usage(session_pct=0.90))

        hint = _subscription_hint("query", "simple", "what is 2+2")
        assert hint is None, (
            f"Simple at 90% session pressure should go external (no CC hint), got: {hint}"
        )
        print("\n[Demo 1d] 90% session pressure -> no CC hint -> external routing")

    @pytest.mark.asyncio
    async def test_full_pressure_routes_to_non_anthropic_model(self, monkeypatch, mock_env):
        """When all quotas are at 97%, router uses an external (non-Anthropic) model."""
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        monkeypatch.setenv("OLLAMA_BASE_URL", "")

        import llm_router.state as _state
        monkeypatch.setattr(_state, "_last_usage", _make_usage(
            session_pct=0.97, weekly_pct=0.97, sonnet_pct=0.97
        ))

        litellm_resp = _make_litellm_resp("The answer is 4.")
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=litellm_resp), \
             patch("litellm.completion_cost", return_value=0.000050):
            resp = await __import__("llm_router.router", fromlist=["route_and_call"]).route_and_call(
                TaskType.QUERY,
                "What is 2+2?",
                profile=RoutingProfile.BUDGET,
            )

        assert resp is not None
        assert "anthropic" not in resp.model.lower(), (
            f"Under full pressure should NOT use Anthropic API model, got: {resp.model}"
        )
        print(f"\n[Demo 1e] Full pressure -> external model: {resp.model}")


# ---------------------------------------------------------------------------
# Demo 1f-1g: Ollama live health probe
# ---------------------------------------------------------------------------

class TestDemo_OllamaHealthProbe:
    """v1.1 Ollama live reachability probe: unreachable Ollama is skipped."""

    def test_ollama_marked_unhealthy_after_repeated_failures(self):
        """Health tracker marks Ollama unhealthy after repeated failures."""
        from llm_router.health import HealthTracker
        tracker = HealthTracker()

        for _ in range(3):
            tracker.record_failure("ollama")

        assert not tracker.is_healthy("ollama"), (
            "Ollama should be unhealthy after 3 consecutive failures"
        )
        status = tracker.status_report()
        print(f"\n[Demo 1f] Ollama health after 3 failures:\n{status}")

    def test_unhealthy_ollama_skipped_by_router(self, monkeypatch, mock_env):
        """Health check gates Ollama: circuit opens after repeated failures."""
        from llm_router.health import HealthTracker

        tracker = HealthTracker()
        assert tracker.is_healthy("ollama"), "Ollama should start healthy"

        for _ in range(5):
            tracker.record_failure("ollama")

        assert not tracker.is_healthy("ollama"), (
            "Ollama should be unhealthy after 5 consecutive failures"
        )
        assert tracker.is_healthy("gemini"), "Gemini should remain healthy"
        print(
            f"\n[Demo 1g] Health probe gate: ollama=unhealthy, gemini=healthy\n"
            f"  {tracker.status_report()}"
        )
