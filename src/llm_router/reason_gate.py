# SPDX-License-Identifier: MIT
"""Calibrated reason-gate — the confidence-driven successor to the single
``_COMPLEXITY_DEEP`` regex in ``classify.py``.

semantic-router's headline lever ("When to Reason", +10pp on MMLU-Pro) is
deciding *when chain-of-thought actually helps* rather than always/never
reasoning. LLM Router already has a ``DEEP_REASONING`` tier, but today it fires from
one binary regex match. This module turns that into a **calibrated score** over
several intrinsic features, with a frozen threshold — so the decision can be
tuned on clean data instead of by hand-editing a regex (which is exactly the
maintenance path that drifted into RA-template matching).

Design mirrors ``semantic_classify``:
  * **Feature-based, no benchmark literals.** Features are generic prompt
    properties (a deep-reasoning keyword hit, complexity keyword hit, math-symbol
    density, length, code-fence presence, optional domain). No RA wrapper string
    appears here — a CI guard asserts it.
  * **Logistic blend.** ``sigmoid(Σ wᵢ·fᵢ) ≥ threshold`` ⇒ needs reasoning.
  * **Regex-equivalent by default, calibratable upward.** The default weights
    make a deep-keyword hit *sufficient* on its own (reproducing today's
    behaviour, so existing routing is unchanged), while giving calibration real
    signal (math density, length, domain) to *add* true positives the regex
    misses. Fitted weights live in the same centroid artifact under a
    ``reason_gate`` block; absent that block, the safe defaults apply.
  * **Abstain-safe.** No artifact ⇒ defaults ⇒ behaviour ≈ the old regex. It can
    only ever be a superset of the previous DEEP_REASONING triggers.

``classify._complexity`` calls :func:`needs_reasoning` in place of its
``_COMPLEXITY_DEEP.search`` branch. The compiled regexes are imported lazily
from ``classify`` (both modules import each other only inside functions, so
there is no import cycle) — keeping a single source of truth for the patterns.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from llm_router.logging import get_logger
from llm_router.types import Subject

log = get_logger("llm_router.reason_gate")

# Domains where chain-of-thought reliably helps (used as the optional subject
# feature). Generic ML knowledge about task types, not RA data.
_REASONING_SUBJECTS = frozenset(
    {Subject.MATH.value, Subject.PHYSICS.value, Subject.REASONING.value, Subject.LAW.value}
)

# Characters that count toward "math density" — digits + common math operators +
# the LaTeX escape. A dense proof/derivation scores high; prose scores ~0.
_MATH_CHARS = set("0123456789+-*/=<>^%∑∫∏√≤≥≈≠∈∀∃·×÷±\\")

# Default logistic weights. Tuned so a deep-keyword hit alone crosses threshold
# (sigmoid(-4+8)=0.98) — reproducing the old regex decision — while nothing else
# fires without calibration. `bias` is the intercept; keys match `_features`.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "bias": -4.0,
    "deep": 8.0,      # deep-reasoning keyword hit → sufficient by itself
    "complex": 1.0,   # complexity keyword hit → nudge, not enough alone
    "math": 2.5,      # math-symbol density ∈ [0,1]
    "length": 1.5,    # length bucket ∈ [0,1]
    "code": -1.5,     # a code fence pushes toward CODE, away from reasoning
    "subject": 3.0,   # prompt's domain ∈ reasoning-friendly set
}
_DEFAULT_THRESHOLD = 0.5


@lru_cache(maxsize=2)
def _classify_regexes() -> tuple[re.Pattern, re.Pattern]:
    """Lazily fetch the deep/complex regexes from ``classify`` (single source)."""
    from llm_router.classify import _COMPLEXITY_COMPLEX, _COMPLEXITY_DEEP

    return _COMPLEXITY_DEEP, _COMPLEXITY_COMPLEX


def _math_density(text: str) -> float:
    if not text:
        return 0.0
    hits = sum(1 for ch in text if ch in _MATH_CHARS)
    return min(1.0, hits / len(text) * 4.0)  # ×4 so a ~25%-symbol span saturates


def _features(text: str, subject: str | None) -> dict[str, float]:
    """Extract the generic reason-gate features from a prompt."""
    deep_re, complex_re = _classify_regexes()
    return {
        "bias": 1.0,
        "deep": 1.0 if deep_re.search(text) else 0.0,
        "complex": 1.0 if complex_re.search(text) else 0.0,
        "math": _math_density(text),
        "length": min(1.0, len(text) / 2000.0),
        "code": 1.0 if "```" in text else 0.0,
        "subject": 1.0 if subject in _REASONING_SUBJECTS else 0.0,
    }


# ── artifact-backed weights ───────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_gate_params(path_str: str) -> tuple[dict[str, float], float]:
    """Load ``{weights, threshold}`` from the ``reason_gate`` block of the
    centroid artifact, falling back to the safe defaults when absent/invalid."""
    path = Path(path_str)
    if not path.is_file():
        return dict(_DEFAULT_WEIGHTS), _DEFAULT_THRESHOLD
    try:
        block = json.loads(path.read_text(encoding="utf-8")).get("reason_gate")
        if not block:
            return dict(_DEFAULT_WEIGHTS), _DEFAULT_THRESHOLD
        weights = dict(_DEFAULT_WEIGHTS)
        for k, v in (block.get("weights") or {}).items():
            if k in _DEFAULT_WEIGHTS:
                weights[k] = float(v)
        threshold = float(block.get("threshold", _DEFAULT_THRESHOLD))
        return weights, threshold
    except Exception as exc:  # noqa: BLE001 — never let a bad artifact break routing
        log.warning("reason_gate: bad artifact %s (%s) — using defaults", path, exc)
        return dict(_DEFAULT_WEIGHTS), _DEFAULT_THRESHOLD


def _params() -> tuple[dict[str, float], float]:
    # Shares the centroid artifact path with semantic_classify.
    from llm_router.semantic_classify import _centroids_path

    return _load_gate_params(_centroids_path())


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


# ── public API ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReasonSignal:
    """Result of the reason-gate: whether the prompt warrants a reasoning model,
    the calibrated probability, and the features that produced it (for telemetry
    and for debugging a surprising route)."""

    needs_reasoning: bool
    score: float
    threshold: float
    features: dict[str, float] = field(default_factory=dict)


def gate(text: str, *, subject: str | None = None) -> ReasonSignal:
    """Compute the calibrated reason-gate decision for ``text``.

    Args:
        text: The prompt.
        subject: Optional domain (``types.Subject`` value) — when the async
            embedding classifier has already run, pass its subject to let a
            math/physics/logic domain contribute; omit on the sync path.

    Returns:
        A :class:`ReasonSignal`. ``needs_reasoning`` is ``score ≥ threshold``.
    """
    weights, threshold = _params()
    feats = _features(text, subject)
    logit = sum(weights.get(k, 0.0) * v for k, v in feats.items())
    score = _sigmoid(logit)
    return ReasonSignal(
        needs_reasoning=score >= threshold,
        score=score,
        threshold=threshold,
        features=feats,
    )


def needs_reasoning(text: str, *, subject: str | None = None) -> bool:
    """Boolean convenience wrapper — the drop-in for ``_COMPLEXITY_DEEP.search``."""
    return gate(text, subject=subject).needs_reasoning
