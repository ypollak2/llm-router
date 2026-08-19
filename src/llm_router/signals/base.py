"""Signal protocol — the contract every detector implements.

A Signal scores a prompt against a single criterion. Multiple signals are
combined by the decision engine (AND/OR/NOT/composite) into a model pick.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SignalScore:
    """Result of evaluating one signal against a prompt.

    Attributes:
        name: Signal identifier (matches config name).
        score: Score in [0.0, 1.0]. Above the signal's threshold = "fires".
        threshold: Configured threshold for firing.
        evidence: Human-readable explanation (which keyword matched, which
            exemplar was closest, etc.) — used by lineage/observability.
    """

    name: str
    score: float
    threshold: float
    evidence: str = ""

    @property
    def fires(self) -> bool:
        return self.score >= self.threshold


class Signal(Protocol):
    """Every signal implements evaluate(prompt) -> SignalScore.

    Signals are stateless. Configuration is captured at __init__ time and
    frozen via dataclass(frozen=True) in concrete subclasses.
    """

    name: str
    threshold: float

    def evaluate(self, prompt: str, context: dict | None = None) -> SignalScore:
        ...
