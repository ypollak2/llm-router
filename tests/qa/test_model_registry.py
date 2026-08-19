"""Model registry tests — YAML parsing + filtering + Pareto frontier."""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.lineage import Tier
from llm_router.model_registry import (
    ModelMetadata,
    ModelRegistry,
    _BUNDLED_DEFAULTS,
)


# ────────────────────────────────────────────────────────────────────────
# Basic construction + lookup
# ────────────────────────────────────────────────────────────────────────

def test_from_models_indexes_by_id():
    reg = ModelRegistry.from_models(_BUNDLED_DEFAULTS)
    assert reg.get("openai/gpt-4o-mini") is not None
    assert reg.get("nonexistent/model") is None


def test_all_returns_every_model():
    reg = ModelRegistry.from_models(_BUNDLED_DEFAULTS)
    assert len(reg.all()) == len(_BUNDLED_DEFAULTS)


def test_by_tier_filters_correctly():
    reg = ModelRegistry.from_models(_BUNDLED_DEFAULTS)
    locals_ = reg.by_tier(Tier.LOCAL)
    assert all(m.tier == Tier.LOCAL for m in locals_)
    assert len(locals_) >= 1


def test_by_provider_groups_by_company():
    reg = ModelRegistry.from_models(_BUNDLED_DEFAULTS)
    anthropics = reg.by_provider("anthropic")
    assert all(m.provider == "anthropic" for m in anthropics)
    assert len(anthropics) >= 1


def test_with_capability_finds_vision_models():
    reg = ModelRegistry.from_models(_BUNDLED_DEFAULTS)
    vision_models = reg.with_capability("vision")
    assert len(vision_models) >= 1
    for m in vision_models:
        assert "vision" in m.capabilities


# ────────────────────────────────────────────────────────────────────────
# YAML loading — shipped config/models.yaml
# ────────────────────────────────────────────────────────────────────────

def test_load_default_finds_config_models_yaml():
    """The shipped config/models.yaml must load without error."""
    ROOT = Path(__file__).resolve().parent.parent.parent
    path = ROOT / "config" / "models.yaml"
    assert path.exists(), "config/models.yaml must ship with LLM Router"
    reg = ModelRegistry.from_yaml(path)
    assert len(reg.all()) >= 10


def test_load_default_handles_missing_file(tmp_path, monkeypatch):
    """When config/models.yaml is absent, fall back to bundled defaults."""
    monkeypatch.chdir(tmp_path)
    # Force the package-level fallback by simulating a fresh cwd
    reg = ModelRegistry.load_default()
    assert len(reg.all()) >= 1


def test_yaml_loader_rejects_missing_required_field(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "models:\n"
        "  - id: x\n"
        "    provider: y\n"
    )
    with pytest.raises(ValueError, match="missing required keys"):
        ModelRegistry.from_yaml(bad)


def test_yaml_loader_rejects_invalid_tier(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "models:\n"
        "  - id: x\n"
        "    provider: y\n"
        "    tier: ULTRA-PREMIUM-MEGA\n"
        "    quality_score: 0.9\n"
        "    price_per_1m_input_usd: 1.0\n"
        "    price_per_1m_output_usd: 2.0\n"
    )
    with pytest.raises(ValueError, match="invalid tier"):
        ModelRegistry.from_yaml(bad)


def test_yaml_loader_rejects_missing_models_root(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("other_root: []\n")
    with pytest.raises(ValueError, match="models"):
        ModelRegistry.from_yaml(bad)


# ────────────────────────────────────────────────────────────────────────
# Pareto frontier — only non-dominated models
# ────────────────────────────────────────────────────────────────────────

def test_pareto_frontier_excludes_dominated_models():
    """A model is dominated if any other is BOTH cheaper AND higher quality."""
    reg = ModelRegistry.from_models([
        ModelMetadata(id="a", provider="x", tier=Tier.LOCAL,
                      quality_score=0.5, price_per_1m_input_usd=1.0,
                      price_per_1m_output_usd=1.0),
        # Dominated by 'a' — same quality, higher price
        ModelMetadata(id="b", provider="x", tier=Tier.LOCAL,
                      quality_score=0.5, price_per_1m_input_usd=5.0,
                      price_per_1m_output_usd=5.0),
        # On frontier — higher quality at higher price
        ModelMetadata(id="c", provider="x", tier=Tier.MID,
                      quality_score=0.9, price_per_1m_input_usd=10.0,
                      price_per_1m_output_usd=10.0),
    ])
    front = reg.pareto_frontier()
    front_ids = {m.id for m in front}
    assert "a" in front_ids
    assert "c" in front_ids
    assert "b" not in front_ids


def test_pareto_frontier_on_bundled_defaults_includes_endpoints():
    reg = ModelRegistry.from_models(_BUNDLED_DEFAULTS)
    front = reg.pareto_frontier()
    front_ids = {m.id for m in front}
    # Cheapest (Ollama, $0) and highest-quality (o3) should be on the frontier
    assert "ollama/qwen3.5:latest" in front_ids
    # The highest-quality premium model should be on the frontier
    front_qualities = {m.quality_score for m in front}
    assert max(front_qualities) >= 0.90


# ────────────────────────────────────────────────────────────────────────
# Cheaper-with-equal-quality
# ────────────────────────────────────────────────────────────────────────

def test_cheaper_with_equal_quality_finds_downshifts():
    reg = ModelRegistry.from_models([
        ModelMetadata(id="expensive", provider="x", tier=Tier.PREMIUM,
                      quality_score=0.85, price_per_1m_input_usd=10.0,
                      price_per_1m_output_usd=10.0),
        ModelMetadata(id="cheap_equiv", provider="y", tier=Tier.MID,
                      quality_score=0.84, price_per_1m_input_usd=2.0,
                      price_per_1m_output_usd=2.0),
        ModelMetadata(id="too_low_quality", provider="z", tier=Tier.CHEAP,
                      quality_score=0.60, price_per_1m_input_usd=0.5,
                      price_per_1m_output_usd=0.5),
    ])
    target = reg.get("expensive")
    suggestions = reg.cheaper_with_equal_quality(
        target, quality_tolerance=0.05
    )
    assert len(suggestions) == 1
    assert suggestions[0].id == "cheap_equiv"


# ────────────────────────────────────────────────────────────────────────
# Cost efficiency
# ────────────────────────────────────────────────────────────────────────

def test_cost_efficiency_is_quality_per_dollar():
    m = ModelMetadata(id="x", provider="y", tier=Tier.MID,
                     quality_score=0.8, price_per_1m_input_usd=2.0,
                     price_per_1m_output_usd=8.0)
    # avg = 2*0.3 + 8*0.7 = 0.6 + 5.6 = 6.2
    # efficiency = 0.8 / 6.2 ≈ 0.129
    assert abs(m.cost_efficiency - (0.8 / 6.2)) < 0.001


def test_cost_efficiency_for_free_model_is_infinite():
    m = ModelMetadata(id="free", provider="ollama", tier=Tier.LOCAL,
                     quality_score=0.7, price_per_1m_input_usd=0.0,
                     price_per_1m_output_usd=0.0)
    assert m.cost_efficiency == float("inf")
