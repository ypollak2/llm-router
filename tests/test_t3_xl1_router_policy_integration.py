"""T3-XL1 router integration: policy-aware candidate ordering + gates.

Mirrors the layering used by T3-S1 (cost cap) and T1-M3 (RBAC):

* **Signature contract** — ``route_and_call`` accepts
  ``agent_session_id``; ``_dispatch_model_loop`` accepts
  ``routing_policy``.
* **Helper exercises** — direct unit tests on the reorder / gate /
  mode-parse helpers.
* **Three-mode env gate** — ``LLM_ROUTER_AGENT_POLICY_MODE`` =
  ``off|warn|strict``. ``off`` makes the integration a no-op;
  ``strict`` skips non-preferred candidates and raises
  ``PermissionDenied`` if the whole chain is skipped.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-008.
"""
from __future__ import annotations

import inspect
from types import MappingProxyType

import pytest

from llm_router import router as router_mod
from llm_router.agents.base import AgentRoutingPolicy
from llm_router.router import (
    _apply_routing_policy,
    _policy_mode,
    route_and_call,
)


# ── 1. Signature contracts ──────────────────────────────────────────────────


def test_route_and_call_accepts_agent_session_id_keyword() -> None:
    sig = inspect.signature(route_and_call)
    assert "agent_session_id" in sig.parameters
    param = sig.parameters["agent_session_id"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_dispatch_model_loop_accepts_routing_policy_keyword() -> None:
    sig = inspect.signature(router_mod._dispatch_model_loop)
    assert "routing_policy" in sig.parameters
    assert sig.parameters["routing_policy"].default is None


# ── 2. _apply_routing_policy — candidate reorder ────────────────────────────


def test_reorder_noop_when_policy_none() -> None:
    candidates = ["openai/gpt-4o", "anthropic/sonnet", "google/gemini"]
    assert _apply_routing_policy(candidates, None, classification="moderate") == candidates


def test_reorder_noop_when_policy_has_no_preferences() -> None:
    """Empty policy = no reordering, no surprises."""
    candidates = ["openai/gpt-4o", "anthropic/sonnet"]
    policy = AgentRoutingPolicy()
    assert _apply_routing_policy(candidates, policy, classification="moderate") == candidates


def test_reorder_promotes_preferred_providers_to_head() -> None:
    """Preferred providers float to the head in the policy's order."""
    candidates = [
        "openai/gpt-4o",
        "anthropic/sonnet",
        "google/gemini-2.5-flash",
        "anthropic/opus",
    ]
    policy = AgentRoutingPolicy(preferred_providers=("anthropic", "google"))
    reordered = _apply_routing_policy(candidates, policy, classification="moderate")
    # Anthropic models come first, then Google, then OpenAI tail.
    assert reordered[0].startswith("anthropic/")
    assert reordered[1].startswith("anthropic/")
    assert reordered[2].startswith("google/")
    assert reordered[3].startswith("openai/")


def test_reorder_preserves_relative_order_within_provider() -> None:
    """Within a provider, the original chain order survives the reorder."""
    candidates = ["anthropic/sonnet", "anthropic/opus", "anthropic/haiku"]
    policy = AgentRoutingPolicy(preferred_providers=("anthropic",))
    reordered = _apply_routing_policy(candidates, policy, classification="moderate")
    assert reordered == candidates


def test_reorder_keeps_non_preferred_in_original_order_at_tail() -> None:
    """Models with no policy preference keep their original chain order."""
    candidates = ["openai/gpt-4o", "google/gemini-pro", "azure/gpt-4o"]
    policy = AgentRoutingPolicy(preferred_providers=("anthropic",))  # none match
    reordered = _apply_routing_policy(candidates, policy, classification="moderate")
    assert reordered == candidates  # nothing to promote → original order


def test_reorder_applies_classification_model_preferences_first() -> None:
    """Per-classification model preferences win over provider preference."""
    candidates = [
        "openai/gpt-4o",
        "anthropic/haiku-4-5",
        "anthropic/opus-4-7",
    ]
    policy = AgentRoutingPolicy(
        preferred_providers=("anthropic",),
        preferred_models_by_classification=MappingProxyType(
            {"simple": ("anthropic/haiku-4-5",)}
        ),
    )
    reordered = _apply_routing_policy(candidates, policy, classification="simple")
    # haiku-4-5 (classification-preferred) wins outright over opus-4-7.
    assert reordered[0] == "anthropic/haiku-4-5"


def test_reorder_classification_irrelevant_for_other_complexity() -> None:
    """Classification preferences for 'simple' don't kick in for 'complex'."""
    candidates = [
        "openai/gpt-4o",
        "anthropic/haiku-4-5",
        "anthropic/opus-4-7",
    ]
    policy = AgentRoutingPolicy(
        preferred_providers=("anthropic",),
        preferred_models_by_classification=MappingProxyType(
            {"simple": ("anthropic/haiku-4-5",)}
        ),
    )
    reordered = _apply_routing_policy(candidates, policy, classification="complex")
    # Only provider preference applies here → both anthropics first, original order preserved.
    assert reordered[0].startswith("anthropic/")
    assert reordered[1].startswith("anthropic/")
    assert reordered[2].startswith("openai/")


# ── 3. _policy_mode env parsing ─────────────────────────────────────────────


def test_policy_mode_defaults_to_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env set = warn mode. Fail-open posture: a missing config never
    breaks routing, but operators get a log line for non-preferred picks."""
    monkeypatch.delenv("LLM_ROUTER_AGENT_POLICY_MODE", raising=False)
    assert _policy_mode() == "warn"


def test_policy_mode_recognises_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_AGENT_POLICY_MODE", "off")
    assert _policy_mode() == "off"


def test_policy_mode_recognises_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_AGENT_POLICY_MODE", "strict")
    assert _policy_mode() == "strict"


def test_policy_mode_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_AGENT_POLICY_MODE", "STRICT")
    assert _policy_mode() == "strict"


def test_policy_mode_invalid_value_falls_back_to_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo / garbage env value defaults to warn (fail-open). The
    convention from prior tracks: misconfigured policy must not break
    routing."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_POLICY_MODE", "yolo")
    assert _policy_mode() == "warn"


# ── 4. Off mode short-circuits ──────────────────────────────────────────────


def test_off_mode_skips_reorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """In off mode, even a strict-conflicting policy is a no-op."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_POLICY_MODE", "off")
    candidates = ["openai/gpt-4o", "anthropic/sonnet"]
    policy = AgentRoutingPolicy(preferred_providers=("anthropic",))
    # off-mode contract: reorder helper returns candidates unchanged.
    assert _apply_routing_policy(
        candidates, policy, classification="moderate"
    ) == candidates
