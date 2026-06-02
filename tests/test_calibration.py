"""Tests for src/llm_router/calibration.py — Plan 07 Cat F (Cost realism).

Calibration replaces hardcoded output-token guesses (80 in legacy estimators,
500 in router.py:1571) with empirical p50/p95 distributions per (model, task).

The RouterArena exercise found Claude Sonnet 4 actually averages 250 output
tokens for QUERY tasks — 3x the legacy 80-token assumption. This module
encodes that data and provides a fallback path for unseen (model, task) pairs.
"""

from dataclasses import FrozenInstanceError

import pytest

from llm_router.types import TaskType


class TestTokenShapeProfile:
    """The empirical token-shape datum, one per (model, task_type)."""

    def test_is_frozen(self):
        """Profiles are immutable per project convention (frozen dataclasses)."""
        from llm_router.calibration import TokenShapeProfile

        profile = TokenShapeProfile(
            model="claude-sonnet-4-6",
            task_type=TaskType.QUERY,
            n_samples=100,
            p50_output=230,
            p95_output=2048,
            avg_input=200,
            avg_output=250,
        )
        with pytest.raises(FrozenInstanceError):
            profile.n_samples = 200  # type: ignore[misc]

    def test_carries_all_design_doc_fields(self):
        """All seven fields from Plan 07 Cat F spec are present."""
        from llm_router.calibration import TokenShapeProfile

        profile = TokenShapeProfile(
            model="gpt-4o-mini",
            task_type=TaskType.CODE,
            n_samples=50,
            p50_output=180,
            p95_output=1500,
            avg_input=150,
            avg_output=200,
        )
        assert profile.model == "gpt-4o-mini"
        assert profile.task_type is TaskType.CODE
        assert profile.n_samples == 50
        assert profile.p50_output == 180
        assert profile.p95_output == 1500
        assert profile.avg_input == 150
        assert profile.avg_output == 200


class TestInitialCalibration:
    """Seeded data from the RouterArena 2026-06 exercise."""

    def test_seeded_with_claude_sonnet_query(self):
        """The single empirically-measured (model, task) from RouterArena exists.

        Source: Plan 07 lines 462-475 — claude-sonnet-4 QUERY, n=1114,
        p50=230, p95=2048, avg_input=200, avg_output=250.
        """
        from llm_router.calibration import INITIAL_CALIBRATION

        key = ("claude-sonnet-4-6", TaskType.QUERY)
        assert key in INITIAL_CALIBRATION, (
            "Calibration must include the RouterArena empirical datum"
        )
        profile = INITIAL_CALIBRATION[key]
        assert profile.n_samples >= 1000
        assert 200 <= profile.p50_output <= 300
        assert profile.p95_output >= 1024
        assert 150 <= profile.avg_input <= 300


class TestPredictCost:
    """The lookup function that replaces hardcoded output-token guesses."""

    def test_uses_empirical_when_samples_sufficient(self):
        """With seeded calibration, prediction reflects p50, not the legacy 80."""
        from llm_router.calibration import predict_cost

        # Sonnet rates: $3/M input, $15/M output. With input=200, p50_output=230:
        # cost ≈ (200 * 3 + 230 * 15) / 1_000_000 = $0.00405
        cost = predict_cost(
            model="claude-sonnet-4-6",
            task_type=TaskType.QUERY,
            input_tokens=200,
            quantile=0.5,
        )
        assert 0.003 < cost < 0.006, (
            f"Expected ~$0.004 for Sonnet QUERY 200in/230out, got ${cost:.5f}"
        )

    def test_p95_quantile_is_strictly_higher_than_p50(self):
        """Worst-case quantile returns higher cost than median."""
        from llm_router.calibration import predict_cost

        cost_p50 = predict_cost(
            "claude-sonnet-4-6", TaskType.QUERY, input_tokens=200, quantile=0.5
        )
        cost_p95 = predict_cost(
            "claude-sonnet-4-6", TaskType.QUERY, input_tokens=200, quantile=0.95
        )
        assert cost_p95 > cost_p50

    def test_falls_back_for_unknown_model_task(self):
        """No data → static fallback returns a non-negative float, no exception."""
        from llm_router.calibration import predict_cost

        cost = predict_cost(
            model="unknown-model-xyz-9",
            task_type=TaskType.GENERATE,
            input_tokens=100,
        )
        assert isinstance(cost, float)
        assert cost >= 0.0

    def test_zero_input_tokens(self):
        """Edge: zero input still returns valid cost (output-driven)."""
        from llm_router.calibration import predict_cost

        cost = predict_cost(
            "claude-sonnet-4-6", TaskType.QUERY, input_tokens=0, quantile=0.5
        )
        assert cost >= 0.0
        # Output-only cost: 230 * 15 / 1M = $0.00345
        assert cost < 0.01

    def test_default_quantile_is_p50(self):
        """quantile defaults to 0.5 per design doc."""
        from llm_router.calibration import predict_cost

        cost_default = predict_cost("claude-sonnet-4-6", TaskType.QUERY, 200)
        cost_explicit_p50 = predict_cost(
            "claude-sonnet-4-6", TaskType.QUERY, 200, quantile=0.5
        )
        assert cost_default == cost_explicit_p50

    def test_provider_prefix_is_stripped_for_lookup(self):
        """The router passes "anthropic/claude-sonnet-4-6" but the calibration
        key is "claude-sonnet-4-6". `predict_cost` must accept either form
        and return identical results.

        This is the contract the router.py:1571 integration relies on.
        """
        from llm_router.calibration import predict_cost

        with_prefix = predict_cost(
            "anthropic/claude-sonnet-4-6", TaskType.QUERY, 200, quantile=0.95
        )
        without_prefix = predict_cost(
            "claude-sonnet-4-6", TaskType.QUERY, 200, quantile=0.95
        )
        assert with_prefix == without_prefix
        assert with_prefix > 0.0  # not silently falling through to "unknown model = 0"

    def test_router_escalation_contract(self):
        """Reproduces the exact call signature router.py:1571 will use after
        Cat F integration. The new quantile=0.95 path on Claude Sonnet 4-6 with
        a realistic prompt size must produce a meaningfully larger projection
        than the legacy `(input, 500)` static guess — otherwise the integration
        gives no real signal over what was there before.

        Legacy:  (2000 in * 3 + 500 out * 15) / 1M = $0.01350
        New p95: (2000 in * 3 + 2048 out * 15) / 1M = $0.03672  (~2.7x higher)
        """
        from llm_router.calibration import predict_cost

        legacy = (2000 * 3 + 500 * 15) / 1_000_000
        new_p95 = predict_cost(
            "anthropic/claude-sonnet-4-6",
            TaskType.QUERY,
            input_tokens=2000,
            quantile=0.95,
        )
        assert new_p95 > 2.0 * legacy, (
            f"Expected p95 projection to be >2x the legacy 500-token guess; "
            f"got new=${new_p95:.5f} vs legacy=${legacy:.5f}"
        )

    def test_realistic_estimate_exceeds_legacy_80_token_assumption(self):
        """The whole point of Cat F: real cost is meaningfully higher than the
        legacy assumption that output ≈ 80 tokens. For Sonnet QUERY:
          legacy: (200 * 3 + 80 * 15) / 1M = $0.0018
          real:   (200 * 3 + 230 * 15) / 1M = $0.00405
        Ratio > 2.0 — exactly the bias Plan 07 set out to fix.
        """
        from llm_router.calibration import predict_cost

        legacy_assumption = (200 * 3 + 80 * 15) / 1_000_000
        realistic = predict_cost("claude-sonnet-4-6", TaskType.QUERY, 200, quantile=0.5)
        assert realistic > 2.0 * legacy_assumption


class TestProjectionCheck:
    """Logs a warning when actual cost overshoots prediction by threshold ×."""

    def test_no_warning_within_threshold(self, caplog):
        """Predicted 0.01, actual 0.015 (1.5x): no warning at default 2.0x threshold."""
        from llm_router.calibration import projection_check

        with caplog.at_level("WARNING"):
            projection_check(predicted=0.01, actual=0.015)
        assert not any(
            "Cost projection blown" in r.message for r in caplog.records
        )

    def test_warns_when_actual_exceeds_threshold(self, caplog):
        """Predicted 0.01, actual 0.05 (5x) at default 2.0x: warning fires."""
        from llm_router.calibration import projection_check

        with caplog.at_level("WARNING"):
            projection_check(predicted=0.01, actual=0.05)
        blown = [r for r in caplog.records if "Cost projection blown" in r.message]
        assert len(blown) == 1
        # The warning must include the ratio for triage
        msg = blown[0].message
        assert "5" in msg  # ratio ≈ 5.0

    def test_custom_threshold(self, caplog):
        """Threshold is configurable: 3.0x means 0.015 actual on 0.01 predicted is fine."""
        from llm_router.calibration import projection_check

        with caplog.at_level("WARNING"):
            projection_check(predicted=0.01, actual=0.02, threshold=3.0)
        assert not any(
            "Cost projection blown" in r.message for r in caplog.records
        )

    def test_zero_predicted_does_not_divide_by_zero(self, caplog):
        """Edge: predicted=0 with positive actual must not crash on ratio computation."""
        from llm_router.calibration import projection_check

        # Should not raise; behavior choice: warn (any non-zero actual blows a
        # zero prediction) or silently return. Either is fine — must not crash.
        with caplog.at_level("WARNING"):
            projection_check(predicted=0.0, actual=0.01)
