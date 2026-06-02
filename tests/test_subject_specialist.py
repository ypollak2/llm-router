"""Plan 07 Phase 3 B.2a — apply_subject_specialist pure transformation.

A policy can declare subject-specific model overrides (e.g.
``specialists.code = openai/gpt-4o`` to route every code subject through
gpt-4o regardless of complexity). apply_subject_specialist rewrites a
candidate chain so the specialist is the first model tried.

This test file covers the pure transformation only. Wiring into
router.py is deferred to a separate commit once the RoutingPolicy
hand-off through the chain-construction pipeline is designed.
"""

from __future__ import annotations

import pytest

from llm_router.policy import RoutingPolicy
from llm_router.types import (
    ClassificationResult,
    Complexity,
    Subject,
    TaskType,
)


def _classification(subject: Subject = Subject.GENERAL) -> ClassificationResult:
    """Build a minimal ClassificationResult for tests."""
    return ClassificationResult(
        complexity=Complexity.MODERATE,
        confidence=0.9,
        reasoning="test",
        inferred_task_type=TaskType.QUERY,
        classifier_model="fake",
        classifier_cost_usd=0.0,
        classifier_latency_ms=0.0,
        subject=subject,
    )


class TestNoOpBehavior:
    """When the policy has nothing relevant, chain is unchanged."""

    def test_empty_specialists_returns_chain_unchanged(self) -> None:
        from llm_router.policy import apply_subject_specialist

        chain = ["openai/gpt-4o-mini", "openai/gpt-4o"]
        policy = RoutingPolicy(
            name="no_spec", description="empty specialists dict"
        )
        out = apply_subject_specialist(chain, _classification(Subject.CODE), policy)
        assert out == chain
        assert out is not chain  # always returns a new list

    def test_subject_not_in_specialists_returns_chain_unchanged(self) -> None:
        from llm_router.policy import apply_subject_specialist

        chain = ["openai/gpt-4o-mini", "openai/gpt-4o"]
        policy = RoutingPolicy(
            name="other_spec",
            description="specialist for medical only",
            specialists={"medical": "anthropic/claude-opus-4-6"},
        )
        out = apply_subject_specialist(chain, _classification(Subject.CODE), policy)
        assert out == chain

    def test_general_subject_is_not_overridden_by_default(self) -> None:
        """GENERAL is the catchall — policies should explicitly opt in."""
        from llm_router.policy import apply_subject_specialist

        chain = ["openai/gpt-4o-mini"]
        policy = RoutingPolicy(
            name="no_general",
            description="no override for general subject",
            specialists={"code": "openai/gpt-4o"},
        )
        out = apply_subject_specialist(chain, _classification(Subject.GENERAL), policy)
        assert out == chain


class TestSpecialistPrepend:
    """When a specialist is declared, it moves to position 0."""

    def test_specialist_prepended_when_absent(self) -> None:
        from llm_router.policy import apply_subject_specialist

        chain = ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4-6"]
        policy = RoutingPolicy(
            name="code_spec",
            description="",
            specialists={"code": "openrouter/qwen-coder"},
        )
        out = apply_subject_specialist(chain, _classification(Subject.CODE), policy)
        assert out == [
            "openrouter/qwen-coder",
            "openai/gpt-4o-mini",
            "anthropic/claude-sonnet-4-6",
        ]

    def test_specialist_already_first_returns_equivalent_chain(self) -> None:
        from llm_router.policy import apply_subject_specialist

        chain = ["openai/gpt-4o", "openai/gpt-4o-mini"]
        policy = RoutingPolicy(
            name="code_spec",
            description="",
            specialists={"code": "openai/gpt-4o"},
        )
        out = apply_subject_specialist(chain, _classification(Subject.CODE), policy)
        assert out == ["openai/gpt-4o", "openai/gpt-4o-mini"]

    def test_specialist_already_mid_chain_moves_to_front(self) -> None:
        """Dedupe semantics — never duplicate the specialist."""
        from llm_router.policy import apply_subject_specialist

        chain = ["openai/gpt-4o-mini", "openai/gpt-4o", "anthropic/claude-sonnet-4-6"]
        policy = RoutingPolicy(
            name="code_spec",
            description="",
            specialists={"code": "openai/gpt-4o"},
        )
        out = apply_subject_specialist(chain, _classification(Subject.CODE), policy)
        assert out == [
            "openai/gpt-4o",                   # moved to front
            "openai/gpt-4o-mini",              # preserved
            "anthropic/claude-sonnet-4-6",     # preserved
        ]

    def test_empty_chain_yields_specialist_only(self) -> None:
        """A specialist with no fallback is a valid (if risky) configuration."""
        from llm_router.policy import apply_subject_specialist

        policy = RoutingPolicy(
            name="code_spec",
            description="",
            specialists={"code": "openrouter/qwen-coder"},
        )
        out = apply_subject_specialist([], _classification(Subject.CODE), policy)
        assert out == ["openrouter/qwen-coder"]


class TestImmutability:
    def test_input_chain_is_not_mutated(self) -> None:
        from llm_router.policy import apply_subject_specialist

        original = ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4-6"]
        snapshot = list(original)
        policy = RoutingPolicy(
            name="code_spec",
            description="",
            specialists={"code": "openai/gpt-4o"},
        )
        _ = apply_subject_specialist(original, _classification(Subject.CODE), policy)
        assert original == snapshot, "apply_subject_specialist must not mutate input"


@pytest.mark.parametrize(
    ("subject", "specialists", "chain", "expected"),
    [
        # No-op cases
        (Subject.GENERAL, {}, ["a", "b"], ["a", "b"]),
        (Subject.CODE, {"medical": "m"}, ["a", "b"], ["a", "b"]),
        # Prepend cases
        (Subject.CODE, {"code": "c"}, ["a", "b"], ["c", "a", "b"]),
        (Subject.MEDICAL, {"medical": "m"}, [], ["m"]),
        # Dedupe cases
        (Subject.CODE, {"code": "a"}, ["a", "b"], ["a", "b"]),
        (Subject.CODE, {"code": "b"}, ["a", "b", "c"], ["b", "a", "c"]),
    ],
    ids=[
        "no-op-empty-specialists",
        "no-op-different-subject",
        "prepend-new",
        "empty-chain-yields-specialist",
        "already-first",
        "dedupe-mid-chain",
    ],
)
def test_apply_subject_specialist_matrix(
    subject: Subject,
    specialists: dict[str, str],
    chain: list[str],
    expected: list[str],
) -> None:
    from llm_router.policy import apply_subject_specialist

    policy = RoutingPolicy(
        name="matrix", description="", specialists=specialists
    )
    out = apply_subject_specialist(chain, _classification(subject), policy)
    assert out == expected
