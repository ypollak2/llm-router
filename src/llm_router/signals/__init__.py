"""Signal layer — score prompts via independent detectors, and act on them.

PiiSignal is the safety-critical one: when a prompt contains a secret, route it
to LOCAL models only so it never reaches an external API.

Ported from Chuzom's signals/ (base + pii). KeywordSignal / EmbeddingSignal are
follow-ups.
"""
from __future__ import annotations

from typing import Optional

from llm_router.signals.base import Signal, SignalScore
from llm_router.signals.pii import PiiSignal
from llm_router.types import LOCAL_PROVIDERS

__all__ = ["Signal", "SignalScore", "PiiSignal", "detect_pii", "force_local_for_pii"]

_PII = PiiSignal()


def detect_pii(prompt: str) -> Optional[SignalScore]:
    """Return the PiiSignal score if it fires (a secret was detected), else None."""
    score = _PII.evaluate(prompt)
    return score if score.fires else None


def _provider_of(model_id: str) -> str:
    head, _, _ = model_id.partition("/")
    return (head or model_id).lower()


def force_local_for_pii(chain: list, prompt: str) -> list:
    """If the prompt contains a secret, filter ``chain`` to LOCAL providers only
    so it is never dispatched to an external API. No-op when no secret is found.

    Returns the local-only subchain when PII fires AND local models exist; if PII
    fires but the chain has NO local model, returns an EMPTY list — the caller must
    then refuse rather than leak the secret to the cloud (fail-closed).
    """
    if detect_pii(prompt) is None:
        return chain
    return [m for m in chain if _provider_of(m) in LOCAL_PROVIDERS]
