"""Decision engine — first-match boolean composition over signal scores.

v0.0.1 ships the data model + a simple priority-ordered evaluator.
v0.0.2 will add the YAML loader and AND/OR/NOT operator nodes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from llm_router.signals.base import SignalScore

Operator = Literal["AND", "OR", "NOT", "SINGLE"]


@dataclass(frozen=True)
class Decision:
    """A single routing rule.

    Attributes:
        name: Rule identifier — surfaced in lineage.
        operator: How signal_refs combine.
        signal_refs: Names of signals to consult.
        action: Model identifier or chain alias to pick when this fires.
        priority: Lower wins. Ties broken by declaration order.
    """

    name: str
    operator: Operator
    signal_refs: tuple[str, ...]
    action: str
    priority: int = 100


@dataclass(frozen=True)
class DecisionResult:
    decision_name: str
    action: str
    fired_signals: tuple[str, ...]
    all_scores: tuple[SignalScore, ...]


class DecisionEngine:
    """Evaluate decisions against a bag of signal scores.

    Usage:
        engine = DecisionEngine(decisions=[d1, d2, ...])
        result = engine.choose(scores={"pii_secret": ..., "code_keywords": ...})
        result.action  -> "local_only" / "code_chain" / etc.
    """

    def __init__(self, decisions: list[Decision]) -> None:
        # Stable sort: priority asc, then declaration order.
        self._decisions = sorted(enumerate(decisions), key=lambda kv: (kv[1].priority, kv[0]))

    def choose(
        self,
        scores: dict[str, SignalScore],
        default_action: str = "default_chain",
        boosts: dict[str, float] | None = None,
    ) -> DecisionResult:
        """Pick the first firing decision (priority asc) or default.

        Args:
            scores: bag of SignalScore from the signal layer.
            default_action: chain alias returned when nothing fires.
            boosts: optional {signal_name: multiplier} applied to the
                score (not threshold) before firing checks. Set by the
                agent layer to bias routing per agent profile. A boost
                > 1 makes the signal fire more aggressively; < 1 makes
                it fire less. Score is clamped to [0, 1] after boost.
        """
        effective_scores = (
            _apply_boosts(scores, boosts) if boosts else scores
        )
        for _, decision in self._decisions:
            fired = self._evaluate(decision, effective_scores)
            if fired:
                return DecisionResult(
                    decision_name=decision.name,
                    action=decision.action,
                    fired_signals=tuple(s.name for s in fired),
                    all_scores=tuple(effective_scores.values()),
                )
        return DecisionResult(
            decision_name="<default>",
            action=default_action,
            fired_signals=(),
            all_scores=tuple(effective_scores.values()),
        )

    @staticmethod
    def _evaluate(decision: Decision, scores: dict[str, SignalScore]) -> list[SignalScore]:
        referenced = [scores[r] for r in decision.signal_refs if r in scores]
        if not referenced:
            return []
        fired = [s for s in referenced if s.fires]
        if decision.operator == "AND":
            return referenced if len(fired) == len(referenced) else []
        if decision.operator == "OR":
            return fired
        if decision.operator == "NOT":
            return [] if fired else referenced
        if decision.operator == "SINGLE":
            return fired[:1]
        raise ValueError(f"unknown operator: {decision.operator}")


def _apply_boosts(
    scores: dict[str, SignalScore], boosts: dict[str, float]
) -> dict[str, SignalScore]:
    """Return a new score map with multipliers applied to the score field.

    Threshold is NOT boosted — that would defeat the point. Score is
    clamped to [0, 1] after multiplication.
    Evidence is annotated so lineage shows the boost was applied.
    """
    out: dict[str, SignalScore] = {}
    for name, signal in scores.items():
        multiplier = boosts.get(name)
        if multiplier is None or multiplier == 1.0:
            out[name] = signal
            continue
        boosted_score = max(0.0, min(1.0, signal.score * multiplier))
        out[name] = SignalScore(
            name=signal.name,
            score=boosted_score,
            threshold=signal.threshold,
            evidence=f"{signal.evidence} (boost×{multiplier})",
        )
    return out
