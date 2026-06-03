"""Plan 07 Cat F — deferred integration sites.

Cat F shipped the empirical token-shape calibration module + router.py
projection. Two further call-sites were deferred to keep the original
diff small:

* ``session_spend._estimate_cost`` had its own pricing dict that was
  silently drifting from ``calibration._PRICING_PER_M``.
* ``hooks/auto-route.py._estimate_cost`` used a static cost_map per
  (task_type, complexity) that ignored both the empirical p50 output
  distribution and the real per-model rates.

These tests pin the new contracts so a future refactor can't quietly
reintroduce a parallel pricing table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from llm_router.calibration import cost_for_tokens
from llm_router.types import TaskType


# ── cost_for_tokens public helper ───────────────────────────────────────────


class TestCostForTokens:
    """The public pricing surface other modules now share."""

    def test_known_model_input_plus_output(self):
        """Sanity: $3/M input + $15/M output for Claude Sonnet 4-6."""
        # 200 × 3 + 250 × 15 = 600 + 3750 = 4350 → $0.00435
        assert cost_for_tokens("claude-sonnet-4-6", 200, 250) == pytest.approx(0.00435)

    def test_strips_provider_prefix(self):
        """Lookup tolerates both ``provider/model`` and bare model names."""
        bare = cost_for_tokens("claude-sonnet-4-6", 100, 100)
        prefixed = cost_for_tokens("anthropic/claude-sonnet-4-6", 100, 100)
        assert bare == prefixed > 0

    def test_free_provider_returns_zero(self):
        """ollama/codex/gemini_cli must price at zero, not at fallback."""
        assert cost_for_tokens("ollama/qwen", 1000, 1000) == 0.0
        assert cost_for_tokens("codex/gpt-4o", 1000, 1000) == 0.0
        assert cost_for_tokens("gemini_cli/flash", 1000, 1000) == 0.0

    def test_unknown_model_returns_zero(self):
        """Unknown models price at zero — callers decide on the fallback."""
        assert cost_for_tokens("weird/model", 1000, 1000) == 0.0


# ── session_spend._estimate_cost ────────────────────────────────────────────


class TestSessionSpendEstimateCost:
    """The session-spend deferred site."""

    def test_known_model_matches_calibration(self):
        """No more drift — sessionspend cost equals calibration cost."""
        from llm_router.session_spend import _estimate_cost
        assert _estimate_cost("openai/gpt-4o", 200, 300) == pytest.approx(
            cost_for_tokens("openai/gpt-4o", 200, 300)
        )

    def test_free_provider_returns_zero(self):
        """Ollama remains zero — the unknown-model fallback must skip free providers."""
        from llm_router.session_spend import _estimate_cost
        assert _estimate_cost("ollama/qwen", 500, 500) == 0.0

    def test_unknown_model_uses_conservative_fallback(self):
        """Unknown models bias high so anomaly detection still has a signal."""
        from llm_router.session_spend import _estimate_cost
        # Pre-Cat-F behaviour: 0.01 per 1K output → 0.003 for 300 tokens.
        # Post-Cat-F: same fallback rate, so the change is invisible to
        # downstream anomaly thresholds.
        assert _estimate_cost("weird/model", 200, 300) == pytest.approx(0.003)


# ── auto-route._estimate_cost ───────────────────────────────────────────────


def _load_hook_module():
    """Import the hook file by path — it lives outside the regular package."""
    spec = importlib.util.spec_from_file_location(
        "auto_route_hook",
        Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAutoRouteEstimateCost:
    """The hook deferred site — displayed routing savings."""

    @pytest.fixture(scope="class")
    def hook(self):
        return _load_hook_module()

    def test_returns_savings_dict(self, hook):
        """Contract: the function returns ``{"savings": "$..."}`` for use in directive text."""
        out = hook._estimate_cost("query", "moderate")
        assert "savings" in out
        assert out["savings"].startswith("$")

    def test_realistic_query_baseline(self, hook):
        """Query/moderate must use empirical p50 output for calibrated models.

        Old code returned a static ``"$0.0005"`` regardless of pricing changes.
        Post-Cat-F, query/moderate against Claude Sonnet 4-6 with 200 input
        and calibrated p50_output=230 evaluates to roughly $0.00405.
        """
        out = hook._estimate_cost("query", "moderate")
        # Parse the float back out to verify it's calibration-driven, not a string match.
        val = float(out["savings"].lstrip("$"))
        assert 0.003 < val < 0.006, f"unexpected baseline cost {out['savings']!r}"

    def test_legacy_fallback_renders_when_calibration_unavailable(self, hook):
        """If the calibration import path is broken, the static map still ships a string."""
        out = hook._legacy_static_savings("code", "complex")
        assert out == {"savings": "$0.010"}

    def test_unknown_task_type_does_not_crash(self, hook):
        """Coerced to QUERY internally; must still emit a savings string."""
        out = hook._estimate_cost("nonsense", "moderate")
        assert out["savings"].startswith("$")

    def test_unknown_complexity_does_not_crash(self, hook):
        """Falls back to the moderate input-token bucket inside the helper."""
        out = hook._estimate_cost("query", "extreme")
        assert out["savings"].startswith("$")


# ── Single-source-of-truth check ────────────────────────────────────────────


class TestSingleSourceOfTruth:
    """Regression guard: no module should ship its own pricing dict.

    Both deferred sites used to maintain a parallel pricing table; this test
    pins the migration by asserting the constants are gone.
    """

    def test_session_spend_has_no_pricing_dict(self):
        from llm_router import session_spend
        assert not hasattr(session_spend, "_COST_PER_1K_OUT"), (
            "session_spend reintroduced a parallel pricing dict — "
            "route through calibration.cost_for_tokens instead."
        )

    def test_auto_route_has_no_constant_cost_map(self):
        """The hook may still ship the legacy fallback as a helper, but the
        cost_map must not be a top-level constant. (Inline inside the fallback
        helper is fine; that's by design.)"""
        text = (
            Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py"
        ).read_text()
        # Heuristic: the only "cost_map" reference should be inside _legacy_static_savings.
        # If a future edit re-promotes it to module scope, this regex catches it.
        import re
        module_scope_assignments = re.findall(
            r"^cost_map\s*=", text, flags=re.MULTILINE,
        )
        assert not module_scope_assignments, (
            "cost_map should live inside _legacy_static_savings, not at module scope."
        )

    def test_taskype_import_used(self):
        """Smoke: TaskType is the enum the hook coerces to."""
        assert TaskType("query") is TaskType.QUERY
