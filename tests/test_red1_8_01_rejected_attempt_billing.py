"""Regression: RED1-8-01 — rejected-but-billed attempt cost must be settled.

_dispatch_model_loop accumulates the real cost of paid attempts a contract gate
or quality-escalation rejected (_failed_attempt_cost). Previously route_and_call
settled only the FINAL response's cost into the budget envelope + quota tracker,
under-counting true spend and letting cumulative spend exceed a cap undetected.
The aggregate is now carried out on LLMResponse.chain_attempt_cost_usd and added
to the settled cost.
"""
from __future__ import annotations

from dataclasses import replace

from llm_router.router import _enrich_response
from llm_router.types import LLMResponse, TaskType


def _resp(cost):
    return LLMResponse(content="a", model="openai/gpt-4o", input_tokens=1, output_tokens=1,
                       cost_usd=cost, latency_ms=1.0, provider="openai")


def test_enrich_carries_failed_attempt_cost():
    r = _enrich_response(_resp(0.03), None, "moderate", TaskType.CODE, ["m1", "m2"],
                         failed_attempt_cost=0.05)
    assert r.chain_attempt_cost_usd == 0.05
    # The settled cost the router computes = final + carried.
    true_cost = r.cost_usd + r.chain_attempt_cost_usd
    assert abs(true_cost - 0.08) < 1e-9


def test_enrich_defaults_to_zero_when_no_rejections():
    r = _enrich_response(_resp(0.02), None, "simple", TaskType.QUERY, ["m1"])
    assert r.chain_attempt_cost_usd == 0.0
    assert (r.cost_usd + r.chain_attempt_cost_usd) == 0.02


def test_field_is_frozen_safe_default():
    """A response built without the field (e.g. from a cache/legacy path) settles
    as just its own cost — getattr default keeps the settlement backward-safe."""
    r = _resp(0.01)
    assert getattr(r, "chain_attempt_cost_usd", 0.0) == 0.0
    # replace() preserves it (frozen dataclass immutability path used by router).
    r2 = replace(r, chain_attempt_cost_usd=0.04)
    assert r2.chain_attempt_cost_usd == 0.04 and r2.cost_usd == 0.01
