"""#14 / R1 — the leaderboard overrides the hardcoded-Claude preference at the ceiling.

The North Star (#162) is "capability = live leaderboard, not Claude-is-best." The
concrete property that must hold in the router: when the leaderboard ranks a
NON-Claude model above Claude in capability for a task, the chain must place that
model ahead of Claude — Claude must not be a hardcoded ceiling. And when the
leaderboard has no data, the chain must fall back safely to the static (Claude-
including) order rather than dropping models.

These pin the behavior of ``apply_benchmark_ordering`` — the function the live
chain-builder (``profiles.get_model_chain``) actually calls — so a regression back
to a hardcoded Claude-top would fail here.
"""
from __future__ import annotations

import llm_router.benchmarks as bm
from llm_router.types import RoutingProfile, TaskType


def test_leaderboard_number_one_beats_claude_at_the_ceiling(monkeypatch):
    """A non-Claude model the leaderboard scores highest for CODE must lead the
    chain, ahead of Claude — even though the static chain lists Claude first."""
    data = {
        "tiers": {"code": {"premium": ["x/top-coder", "anthropic/claude-opus"]}},
        "task_scores": {"code": {"x/top-coder": 0.99, "anthropic/claude-opus": 0.80}},
    }
    monkeypatch.setattr(bm, "get_benchmark_data", lambda: data)
    # Static order deliberately puts Claude FIRST — the leaderboard must override it.
    chain = ["anthropic/claude-opus", "x/top-coder"]
    out = bm.apply_benchmark_ordering(chain, TaskType.CODE, RoutingProfile.PREMIUM)
    assert out.index("x/top-coder") < out.index("anthropic/claude-opus"), (
        f"leaderboard #1 (non-Claude) must lead the chain, not the hardcoded Claude: {out}")


def test_claude_leads_when_the_leaderboard_ranks_it_top(monkeypatch):
    """Symmetric guard: when the leaderboard genuinely ranks Claude highest, Claude
    leads — the router follows the leaderboard in BOTH directions, not a fixed bias."""
    data = {
        "tiers": {"code": {"premium": ["anthropic/claude-opus", "x/mid-coder"]}},
        "task_scores": {"code": {"anthropic/claude-opus": 0.99, "x/mid-coder": 0.70}},
    }
    monkeypatch.setattr(bm, "get_benchmark_data", lambda: data)
    chain = ["x/mid-coder", "anthropic/claude-opus"]  # static puts non-Claude first
    out = bm.apply_benchmark_ordering(chain, TaskType.CODE, RoutingProfile.PREMIUM)
    assert out.index("anthropic/claude-opus") < out.index("x/mid-coder"), (
        f"when the leaderboard ranks Claude #1, Claude leads: {out}")


def test_no_leaderboard_data_falls_back_to_static_chain(monkeypatch):
    """Safe fallback: with no leaderboard data, the static chain is returned
    unchanged (no models dropped, no reorder) — the hardcoded order is the
    default ONLY when the leaderboard is silent."""
    monkeypatch.setattr(bm, "get_benchmark_data", lambda: None)
    chain = ["anthropic/claude-opus", "x/top-coder", "ollama/qwen3"]
    out = bm.apply_benchmark_ordering(chain, TaskType.CODE, RoutingProfile.PREMIUM)
    assert out == chain, f"no leaderboard data must return the static chain unchanged: {out}"
