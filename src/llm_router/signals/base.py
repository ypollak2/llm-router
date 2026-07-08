"""Signal protocol — the contract every detector implements.

A Signal scores a prompt against a single criterion. Signals are stateless and
pure; composition into routing decisions happens in the caller.

Ported from Chuzom's signals/base.py (no chuzom deps).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class SignalScore:
    """Result of evaluating one signal against a prompt.

    ``score`` is in [0, 1]; at/above ``threshold`` the signal "fires".
    ``evidence`` is a human-readable explanation for observability — it must
    NEVER contain sensitive matched values (e.g. a detected secret).
    """

    name: str
    score: float
    threshold: float
    evidence: str = ""

    @property
    def fires(self) -> bool:
        return self.score >= self.threshold


class Signal(Protocol):
    name: str
    threshold: float

    def evaluate(self, prompt: str, context: Optional[dict] = None) -> SignalScore:
        ...
