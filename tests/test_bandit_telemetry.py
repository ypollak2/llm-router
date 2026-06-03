"""Plan 07 Cat E — outcome telemetry + epsilon-greedy bandit.

Two surfaces under test:

* :mod:`llm_router.telemetry` — ``ModelStats`` arithmetic and the
  ``aggregate_stats`` SQL aggregation over ``routing_decisions``.
* :mod:`llm_router.bandit` — the ``EpsilonGreedyBandit.reorder`` exploit/
  explore logic, with telemetry stubbed so each test pins one concern.

We avoid touching the real router code path here — the wiring inside
``router._build_and_filter_chain`` and ``router.route_and_call`` is exercised
by the broader router integration suite. These tests pin the *contracts* the
router relies on so a future refactor cannot silently degrade them.
"""

from __future__ import annotations

import random

import pytest

from llm_router.bandit import DEFAULT_EPSILON, EpsilonGreedyBandit
from llm_router.telemetry import MIN_SAMPLES_FOR_SIGNAL, ModelStats


# ── ModelStats arithmetic ────────────────────────────────────────────────────


class TestModelStats:
    """Per-(profile, subject, model) stat row returned by ``aggregate_stats``."""

    def test_expected_value_balances_success_and_cost(self):
        """EV = success_rate / avg_cost — cheaper success beats expensive success."""
        cheap = ModelStats(
            model="ollama/qwen", n_samples=100, success_rate=0.80,
            avg_cost=0.0001, avg_latency_ms=500,
        )
        pricey = ModelStats(
            model="anthropic/opus", n_samples=100, success_rate=0.95,
            avg_cost=0.05, avg_latency_ms=2000,
        )
        # Cheap model is ~420x cheaper per success — should crush opus on EV.
        assert cheap.expected_value > pricey.expected_value

    def test_expected_value_handles_free_provider_cost(self):
        """Free providers (avg_cost=0) must produce a large but finite EV.

        Without the ``1e-9`` floor, dividing by zero would crash the bandit on
        Ollama/Codex rows and the whole reorder would short-circuit.
        """
        free = ModelStats(
            model="ollama/qwen", n_samples=50, success_rate=0.70,
            avg_cost=0.0, avg_latency_ms=400,
        )
        ev = free.expected_value
        assert ev > 0
        assert ev != float("inf")

    def test_expected_value_zero_when_no_successes(self):
        """A model with 0% success rate has zero expected value regardless of cost."""
        flop = ModelStats(
            model="bad/model", n_samples=40, success_rate=0.0,
            avg_cost=0.01, avg_latency_ms=900,
        )
        assert flop.expected_value == 0.0


# ── Bandit cold-start ────────────────────────────────────────────────────────


def _stub_aggregate(stats: list[ModelStats]):
    """Build an awaitable that returns ``stats`` regardless of inputs.

    Used to pin telemetry inside bandit tests so a flaky DB or an evolving
    aggregation query can never break the bandit contract.
    """

    async def _fn(profile: str, subject: str, candidates: list[str], *, window_days: int = 30):
        return list(stats)

    return _fn


class TestBanditColdStart:
    """When telemetry is empty or thin, the static policy order must win."""

    async def test_returns_input_when_under_two_candidates(self, monkeypatch):
        """A single-element chain has nothing to reorder."""
        bandit = EpsilonGreedyBandit()
        out = await bandit.reorder(["openai/gpt-4o"], profile="balanced", subject="code")
        assert out == ["openai/gpt-4o"]

    async def test_no_telemetry_returns_input_unchanged(self, monkeypatch):
        """No rows in the DB → leave order alone (static policy is the prior)."""
        monkeypatch.setattr(
            "llm_router.bandit.aggregate_stats", _stub_aggregate([])
        )
        bandit = EpsilonGreedyBandit()
        chain = ["ollama/qwen", "openai/gpt-4o", "anthropic/sonnet"]
        out = await bandit.reorder(chain, profile="balanced", subject="code")
        assert out == chain

    async def test_under_min_samples_returns_input_unchanged(self, monkeypatch):
        """Below MIN_SAMPLES_FOR_SIGNAL per candidate → static order."""
        thin = [
            ModelStats(
                model="ollama/qwen", n_samples=MIN_SAMPLES_FOR_SIGNAL - 1,
                success_rate=0.90, avg_cost=0.0, avg_latency_ms=400,
            ),
        ]
        monkeypatch.setattr("llm_router.bandit.aggregate_stats", _stub_aggregate(thin))
        bandit = EpsilonGreedyBandit()
        chain = ["openai/gpt-4o", "ollama/qwen"]
        out = await bandit.reorder(chain, profile="balanced", subject="code")
        assert out == chain


# ── Bandit exploit ───────────────────────────────────────────────────────────


class TestBanditExploit:
    """With epsilon=0 the bandit is purely greedy."""

    async def test_swaps_best_ev_to_front(self, monkeypatch):
        """Best-EV candidate moves to position 0; rest keep relative order."""
        good = [
            ModelStats(
                model="ollama/qwen", n_samples=200, success_rate=0.85,
                avg_cost=0.0001, avg_latency_ms=400,
            ),
            ModelStats(
                model="openai/gpt-4o", n_samples=200, success_rate=0.90,
                avg_cost=0.005, avg_latency_ms=1200,
            ),
        ]
        monkeypatch.setattr("llm_router.bandit.aggregate_stats", _stub_aggregate(good))
        bandit = EpsilonGreedyBandit(epsilon=0.0)
        chain = ["openai/gpt-4o", "anthropic/opus", "ollama/qwen"]
        out = await bandit.reorder(chain, profile="balanced", subject="code")
        # ollama has best EV (cheap successes) — it leads, others keep order.
        assert out[0] == "ollama/qwen"
        assert out[1:] == ["openai/gpt-4o", "anthropic/opus"]

    async def test_no_op_when_best_is_already_first(self, monkeypatch):
        """Don't pointlessly rebuild the list when the chain is already optimal."""
        good = [
            ModelStats(
                model="openai/gpt-4o", n_samples=200, success_rate=0.95,
                avg_cost=0.001, avg_latency_ms=800,
            ),
            ModelStats(
                model="anthropic/sonnet", n_samples=200, success_rate=0.85,
                avg_cost=0.005, avg_latency_ms=1500,
            ),
        ]
        monkeypatch.setattr("llm_router.bandit.aggregate_stats", _stub_aggregate(good))
        bandit = EpsilonGreedyBandit(epsilon=0.0)
        chain = ["openai/gpt-4o", "anthropic/sonnet"]
        out = await bandit.reorder(chain, profile="balanced", subject="code")
        assert out == chain


# ── Bandit explore ──────────────────────────────────────────────────────────


class TestBanditExplore:
    """With epsilon=1 the bandit always explores away from the empirical best."""

    async def test_explore_never_picks_the_known_best(self, monkeypatch):
        """The whole point of exploration: surface evidence on alternatives."""
        good = [
            ModelStats(
                model="ollama/qwen", n_samples=200, success_rate=0.85,
                avg_cost=0.0001, avg_latency_ms=400,
            ),
            ModelStats(
                model="openai/gpt-4o", n_samples=200, success_rate=0.90,
                avg_cost=0.005, avg_latency_ms=1200,
            ),
        ]
        monkeypatch.setattr("llm_router.bandit.aggregate_stats", _stub_aggregate(good))
        # Pinned RNG so the test is deterministic.
        bandit = EpsilonGreedyBandit(epsilon=1.0, rng=random.Random(0))
        chain = ["openai/gpt-4o", "anthropic/sonnet", "ollama/qwen"]
        out = await bandit.reorder(chain, profile="balanced", subject="code")
        # ollama/qwen is the EV winner — explore must NOT put it first.
        assert out[0] != "ollama/qwen"
        # The exploration candidate must come from the original chain.
        assert out[0] in chain


class TestBanditDefaults:
    """Public-API guard: defaults shouldn't drift silently."""

    def test_default_epsilon_is_ten_percent(self):
        """Hard-coded check so a tuning change shows up in code review."""
        assert DEFAULT_EPSILON == 0.10

    def test_bandit_default_uses_module_epsilon(self):
        """No-arg constructor should match the module constant."""
        assert EpsilonGreedyBandit().epsilon == DEFAULT_EPSILON


# ── Aggregation contract (DB-touching) ──────────────────────────────────────


@pytest.mark.slow
class TestAggregateStatsIntegration:
    """Round-trip through SQLite to verify the migration + index + query land.

    Marked slow because it touches the real ``_get_db`` initializer; the
    cold-start tests above keep the fast suite covering the bandit logic.
    """

    async def test_empty_candidates_short_circuits(self, temp_db):
        """No candidates → no rows aggregated, no DB hit."""
        from llm_router.telemetry import aggregate_stats

        out = await aggregate_stats(profile="balanced", subject="code", candidates=[])
        assert out == []

    async def test_subject_column_migrated(self, temp_db):
        """The ``subject`` column must exist after _get_db initializes.

        Regression guard: if anyone re-orders or removes MIGRATE_ROUTING_DECISIONS_ADD_SUBJECT,
        this test breaks before the bandit silently routes blind.
        """
        from llm_router.cost import _get_db

        db = await _get_db()
        try:
            async with db.execute("PRAGMA table_info(routing_decisions)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
        finally:
            await db.close()
        assert "subject" in cols

    async def test_aggregates_persisted_rows(self, temp_db):
        """End-to-end: write rows via log_routing_decision, read via aggregate_stats."""
        from llm_router.cost import log_routing_decision
        from llm_router.telemetry import aggregate_stats

        async def _log(model: str, success: bool, cost: float) -> None:
            await log_routing_decision(
                prompt="hash-this",
                task_type="code",
                profile="balanced",
                classifier_type="heuristic",
                classifier_model=None,
                classifier_confidence=0.9,
                classifier_latency_ms=0.0,
                complexity="moderate",
                recommended_model=model,
                base_model=model,
                was_downshifted=False,
                budget_pct_used=0.0,
                quality_mode="balanced",
                final_model=model,
                final_provider=model.split("/")[0],
                success=success,
                input_tokens=100,
                output_tokens=80,
                cost_usd=cost,
                latency_ms=500.0,
                subject="code",
            )

        # 3 successes for gpt-4o, 1 failure (75% sr); 2 successes for haiku (100% sr).
        await _log("openai/gpt-4o", True, 0.005)
        await _log("openai/gpt-4o", True, 0.005)
        await _log("openai/gpt-4o", True, 0.005)
        await _log("openai/gpt-4o", False, 0.005)
        await _log("anthropic/claude-haiku-4-5-20251001", True, 0.001)
        await _log("anthropic/claude-haiku-4-5-20251001", True, 0.001)

        stats = await aggregate_stats(
            profile="balanced",
            subject="code",
            candidates=[
                "openai/gpt-4o",
                "anthropic/claude-haiku-4-5-20251001",
            ],
        )
        by_model = {s.model: s for s in stats}
        assert by_model["openai/gpt-4o"].n_samples == 4
        assert by_model["openai/gpt-4o"].success_rate == pytest.approx(0.75)
        assert by_model["anthropic/claude-haiku-4-5-20251001"].success_rate == pytest.approx(1.0)
