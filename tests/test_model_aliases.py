"""Tests for version-agnostic model aliasing & family fallback.

These guard the "Opus 5 ships tomorrow" promise: a new model version must not
break chains, guards, or cost lookups, and adopting it should be a one-line edit.
"""
from __future__ import annotations

import llm_router.model_aliases as ma


def test_model_family_strips_version():
    assert ma.model_family("anthropic/claude-opus-4-8") == "anthropic/claude-opus"
    assert ma.model_family("anthropic/claude-haiku-4-5-20251001") == "anthropic/claude-haiku"
    assert ma.model_family("ollama/qwen3:32b") == "ollama/qwen3:32b"  # no version suffix


def test_model_matches_family_and_exact():
    assert ma.model_matches("anthropic/claude-opus-4-8", "anthropic/claude-opus")
    assert ma.model_matches("anthropic/claude-opus-5", "anthropic/claude-opus")      # future
    assert ma.model_matches("anthropic/claude-opus-4-8", "anthropic/claude-opus-4-8")  # exact
    assert not ma.model_matches("anthropic/claude-sonnet-4-6", "anthropic/claude-opus")


def test_resolve_alias_known_and_unknown():
    assert ma.resolve_model_alias("anthropic/claude-opus:latest") == ma.LATEST_CLAUDE["anthropic/claude-opus"]
    assert ma.resolve_model_alias("openai/gpt-4o") == "openai/gpt-4o"          # non-alias unchanged
    assert ma.resolve_model_alias("unknown/model:latest") == "unknown/model:latest"  # unknown family unchanged


def test_family_lookup_inherits():
    table = {"anthropic/claude-opus-4-8": 0.075, "openai/gpt-4o": 0.005}
    assert ma.family_lookup(table, "anthropic/claude-opus-4-8") == 0.075   # exact
    assert ma.family_lookup(table, "anthropic/claude-opus-5") == 0.075     # future inherits family
    assert ma.family_lookup(table, "totally/unknown", 0.0) == 0.0         # default


def test_chains_resolve_aliases_in_routing_table():
    from llm_router.profiles import ROUTING_TABLE
    from llm_router.types import RoutingProfile, TaskType
    chain = ROUTING_TABLE[(RoutingProfile.PREMIUM, TaskType.ANALYZE)]
    assert not any(":latest" in m for m in chain), "aliases must be resolved in runtime table"
    assert chain[0] == ma.LATEST_CLAUDE["anthropic/claude-opus"]


def test_opus5_simulation_one_line_bump(monkeypatch):
    """Simulate Opus 5 shipping: bump LATEST_CLAUDE only, chains follow, nothing breaks."""
    monkeypatch.setitem(ma.LATEST_CLAUDE, "anthropic/claude-opus", "anthropic/claude-opus-5")
    # the resolver now yields opus-5 for the alias
    assert ma.resolve_model_alias("anthropic/claude-opus:latest") == "anthropic/claude-opus-5"
    # family guard still recognizes opus-5 as the opus family
    assert ma.model_matches("anthropic/claude-opus-5", "anthropic/claude-opus")
