"""Tests for llm_router.lineage — inversion detection + SQLite persistence."""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.lineage import (
    Inversion,
    LineageStore,
    Tier,
    detect_inversion,
    make_record,
    tier_for_model,
)


# ─────────────────────────────────────────────────────────────────────
# tier_for_model
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("ollama/qwen3.5:latest", Tier.LOCAL),
        ("gemma:7b", Tier.LOCAL),
        ("anthropic/claude-3.5-haiku", Tier.CHEAP),
        ("google/gemini-2.5-flash", Tier.CHEAP),
        ("openai/gpt-4o-mini", Tier.CHEAP),
        ("anthropic/claude-3.5-sonnet", Tier.MID),
        ("openai/gpt-4o", Tier.MID),
        ("anthropic/claude-3-opus", Tier.PREMIUM),
        ("openai/o3", Tier.PREMIUM),
        ("openai/gpt-5", Tier.PREMIUM),
        ("anthropic/claude-4-opus", Tier.PREMIUM),
        ("totally-unknown-model", Tier.UNKNOWN),
    ],
)
def test_tier_for_model(model_id: str, expected: Tier) -> None:
    assert tier_for_model(model_id) == expected


# ─────────────────────────────────────────────────────────────────────
# detect_inversion
# ─────────────────────────────────────────────────────────────────────

def test_complex_to_local_is_up_inversion() -> None:
    assert detect_inversion("complex", Tier.LOCAL) == Inversion.UP


def test_complex_to_cheap_is_up_inversion() -> None:
    assert detect_inversion("complex", Tier.CHEAP) == Inversion.UP


def test_complex_to_mid_is_up_inversion() -> None:
    # complex's expected tier is PREMIUM, so MID is still an up-inversion.
    assert detect_inversion("complex", Tier.MID) == Inversion.UP


def test_complex_to_premium_is_no_inversion() -> None:
    assert detect_inversion("complex", Tier.PREMIUM) == Inversion.NONE


def test_simple_to_premium_is_down_inversion() -> None:
    assert detect_inversion("simple", Tier.PREMIUM) == Inversion.DOWN


def test_simple_to_mid_is_down_inversion() -> None:
    assert detect_inversion("simple", Tier.MID) == Inversion.DOWN


def test_simple_to_cheap_is_no_inversion() -> None:
    assert detect_inversion("simple", Tier.CHEAP) == Inversion.NONE


def test_simple_to_local_is_no_inversion() -> None:
    # cheaper than expected is fine — that's the whole point.
    assert detect_inversion("simple", Tier.LOCAL) == Inversion.NONE


def test_moderate_to_cheap_is_no_inversion() -> None:
    # moderate's expected upper bound is MID; CHEAP is fine.
    assert detect_inversion("moderate", Tier.CHEAP) == Inversion.NONE


def test_unknown_tier_yields_no_inversion() -> None:
    assert detect_inversion("complex", Tier.UNKNOWN) == Inversion.NONE


def test_unknown_complexity_yields_no_inversion() -> None:
    assert detect_inversion("mystery_complexity", Tier.PREMIUM) == Inversion.NONE


# ─────────────────────────────────────────────────────────────────────
# LineageStore — persistence + queries
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> LineageStore:
    return LineageStore(db_path=tmp_path / "lineage.db")


def _record(complexity: str, model: str, **overrides):
    return make_record(
        host=overrides.get("host", "claude-code"),
        prompt_fingerprint=overrides.get("prompt_fingerprint", "abc123"),
        task_type=overrides.get("task_type", "query"),
        complexity=complexity,
        classifier_method=overrides.get("classifier_method", "heuristic"),
        signal_scores=overrides.get("signal_scores", {"pii": 0.0}),
        fired_decisions=overrides.get("fired_decisions", ()),
        chain_attempted=overrides.get("chain_attempted", (model,)),
        model_chosen=model,
        outcome=overrides.get("outcome", "success"),
        latency_ms=overrides.get("latency_ms", 100),
        cost_usd=overrides.get("cost_usd", 0.001),
    )


def test_store_roundtrips_a_record(store: LineageStore) -> None:
    store.record(_record("simple", "ollama/qwen3.5:latest"))
    recent = store.recent()
    assert len(recent) == 1
    assert recent[0]["complexity"] == "simple"
    assert recent[0]["model_chosen"] == "ollama/qwen3.5:latest"
    assert recent[0]["inversion"] == "none"


def test_store_flags_up_inversion(store: LineageStore) -> None:
    store.record(_record("complex", "ollama/qwen3.5:latest"))
    inversions = store.inversions()
    assert len(inversions) == 1
    assert inversions[0]["inversion"] == Inversion.UP.value


def test_store_separates_up_and_down(store: LineageStore) -> None:
    store.record(_record("complex", "ollama/qwen3.5:latest"))  # up
    store.record(_record("simple", "openai/o3"))  # down
    store.record(_record("moderate", "openai/gpt-4o"))  # none
    up = store.inversions(kind=Inversion.UP)
    down = store.inversions(kind=Inversion.DOWN)
    assert len(up) == 1
    assert len(down) == 1
    assert up[0]["complexity"] == "complex"
    assert down[0]["complexity"] == "simple"


def test_summary_counts_inversions(store: LineageStore) -> None:
    store.record(_record("complex", "ollama/qwen3.5:latest"))  # up
    store.record(_record("complex", "ollama/qwen3.5:latest"))  # up
    store.record(_record("simple", "openai/o3"))  # down
    store.record(_record("moderate", "openai/gpt-4o"))  # none
    summary = store.summary()
    assert summary["total_decisions"] == 4
    assert summary["up_inversions"] == 2
    assert summary["down_inversions"] == 1
    assert summary["no_inversion"] == 1
    assert summary["inversion_rate"] == pytest.approx(0.75)


def test_signal_scores_persist_as_dict(store: LineageStore) -> None:
    rec = _record("simple", "ollama/qwen3.5:latest", signal_scores={"pii": 0.0, "code_keywords": 0.85})
    store.record(rec)
    fetched = store.recent()[0]
    import json
    parsed = json.loads(fetched["signal_scores"])
    assert parsed == {"pii": 0.0, "code_keywords": 0.85}


def test_explicit_tier_overrides_inference(store: LineageStore) -> None:
    # Force a tier even if the model_id substring would suggest otherwise.
    rec = make_record(
        host="cursor",
        prompt_fingerprint="xyz",
        task_type="code",
        complexity="complex",
        classifier_method="signal_engine",
        signal_scores={},
        fired_decisions=(),
        chain_attempted=("custom-special-model",),
        model_chosen="custom-special-model",
        model_tier=Tier.PREMIUM,
        outcome="success",
        latency_ms=42,
        cost_usd=0.02,
    )
    store.record(rec)
    fetched = store.recent()[0]
    assert fetched["model_tier"] == "premium"
    assert fetched["inversion"] == "none"
