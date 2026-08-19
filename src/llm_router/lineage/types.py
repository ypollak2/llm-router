"""Routing-lineage value types and tier/inversion helpers.

These were originally defined in the flat ``llm_router/lineage.py`` module.
When the lineage layer was split into a package (``llm_router/lineage/``)
in the v0.1.x rewrite, the old ``.py`` file became dead code — Python's
import machinery picks the package over the same-named module file, so
``from llm_router.lineage import Tier`` was an ImportError even though the
class still existed on disk.

This module restores the canonical home for those symbols inside the
package, so consumers like ``llm_router.observability.summary``, ``llm_router.model_registry``,
and the QA test suite can import them via the documented public path:

    from llm_router.lineage import Tier, Inversion, LineageRecord, make_record

The ``LineageStore`` class itself lives in ``llm_router.lineage.lineage_store``
and is re-exported from the package ``__init__.py``; it does NOT belong
here because it owns mutable I/O state, not a value type.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Tier(str, Enum):
    """Coarse model tier — used for inversion detection."""

    LOCAL = "local"     # Ollama, on-device
    CHEAP = "cheap"     # Haiku, Gemini Flash, GPT-4o-mini, Groq
    MID = "mid"         # GPT-4o, Gemini Pro, Sonnet
    PREMIUM = "premium" # Opus, o3, GPT-5
    UNKNOWN = "unknown"


class Inversion(str, Enum):
    """Mismatch classification between prompt complexity and chosen tier.

    UP-inversion: classified=complex but model is in the cheap tier —
        likely a misroute that under-served the user.
    DOWN-inversion: classified=simple but model is premium — fallback
        chain exhausted, unnecessary over-spend.
    """

    NONE = "none"
    UP = "up_inversion"
    DOWN = "down_inversion"


# Coarse map: complexity bucket -> expected tier upper bound.
# Crossing this boundary flags an inversion.
_COMPLEXITY_EXPECTED_TIER: dict[str, Tier] = {
    "simple": Tier.CHEAP,
    "moderate": Tier.MID,
    "complex": Tier.PREMIUM,
}

_TIER_ORDER: dict[Tier, int] = {
    Tier.LOCAL: 0,
    Tier.CHEAP: 1,
    Tier.MID: 2,
    Tier.PREMIUM: 3,
    Tier.UNKNOWN: -1,
}


@dataclass(frozen=True)
class LineageRecord:
    """One routing decision, fully audited.

    Agent-session fields (agent_id, session_id, step_index,
    parent_session_id, framework) are optional — when the call isn't
    part of an agent run, they're left None and the row behaves as a
    standalone routing decision.
    """

    id: str
    timestamp: float
    host: str
    prompt_fingerprint: str
    task_type: str
    complexity: str
    classifier_method: str
    signal_scores: dict[str, float]
    fired_decisions: tuple[str, ...]
    chain_attempted: tuple[str, ...]
    model_chosen: str
    model_tier: Tier
    inversion: Inversion
    outcome: str
    latency_ms: int
    cost_usd: float
    notes: str = ""
    agent_id: str | None = None
    session_id: str | None = None
    step_index: int | None = None
    parent_session_id: str | None = None
    framework: str | None = None

    def to_row(self) -> tuple:
        return (
            self.id,
            self.timestamp,
            self.host,
            self.prompt_fingerprint,
            self.task_type,
            self.complexity,
            self.classifier_method,
            json.dumps(self.signal_scores),
            json.dumps(list(self.fired_decisions)),
            json.dumps(list(self.chain_attempted)),
            self.model_chosen,
            self.model_tier.value,
            self.inversion.value,
            self.outcome,
            self.latency_ms,
            self.cost_usd,
            self.notes,
            self.agent_id,
            self.session_id,
            self.step_index,
            self.parent_session_id,
            self.framework,
        )


def detect_inversion(complexity: str, model_tier: Tier) -> Inversion:
    """Classify a (complexity, tier) pair as up-/down-/no-inversion.

    Up-inversion: complex query -> cheap or local. Most actionable.
    Down-inversion: simple query -> premium. Wasted spend.
    """
    expected = _COMPLEXITY_EXPECTED_TIER.get(complexity)
    if expected is None or model_tier == Tier.UNKNOWN:
        return Inversion.NONE
    actual_rank = _TIER_ORDER[model_tier]
    expected_rank = _TIER_ORDER[expected]
    if complexity == "complex" and actual_rank < expected_rank:
        return Inversion.UP
    if complexity == "simple" and actual_rank > expected_rank:
        return Inversion.DOWN
    return Inversion.NONE


def tier_for_model(model_id: str) -> Tier:
    """Best-effort tier lookup based on model_id substrings.

    Used by lineage when the router doesn't pass an explicit tier. The
    canonical mapping lives in llm_router.model_selector; this is a fallback
    for ad-hoc records built via ``make_record``.
    """
    m = model_id.lower()
    if any(k in m for k in ("ollama", "qwen3.5", "gemma", "llama3", "phi-3")):
        return Tier.LOCAL
    # Codex and Gemini CLI run via free subscriptions — treat as CHEAP to avoid
    # false DOWN-inversions when they handle simple tasks after Ollama fails.
    if m.startswith("codex/") or m.startswith("gemini_cli/"):
        return Tier.CHEAP
    if any(k in m for k in (
        "haiku", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-3.1-flash",
        "gpt-4o-mini", "gpt-5-nano", "groq",
    )):
        return Tier.CHEAP
    if any(k in m for k in (
        "sonnet", "gpt-4o", "gemini-1.5-pro", "gemini-2.5-pro", "gemini-3.1-pro",
    )):
        return Tier.MID
    if any(k in m for k in ("opus", "o3", "gpt-5", "claude-4")):
        return Tier.PREMIUM
    return Tier.UNKNOWN


def make_record(
    *,
    host: str,
    prompt_fingerprint: str,
    task_type: str,
    complexity: str,
    classifier_method: str,
    signal_scores: dict[str, float],
    fired_decisions: Iterable[str],
    chain_attempted: Iterable[str],
    model_chosen: str,
    outcome: str,
    latency_ms: int,
    cost_usd: float,
    model_tier: Tier | None = None,
    notes: str = "",
    agent_id: str | None = None,
    session_id: str | None = None,
    step_index: int | None = None,
    parent_session_id: str | None = None,
    framework: str | None = None,
) -> LineageRecord:
    """Convenience builder — derives tier + inversion automatically."""
    tier = model_tier if model_tier is not None else tier_for_model(model_chosen)
    inversion = detect_inversion(complexity, tier)
    return LineageRecord(
        id=str(uuid.uuid4()),
        timestamp=time.time(),
        host=host,
        prompt_fingerprint=prompt_fingerprint,
        task_type=task_type,
        complexity=complexity,
        classifier_method=classifier_method,
        signal_scores=dict(signal_scores),
        fired_decisions=tuple(fired_decisions),
        chain_attempted=tuple(chain_attempted),
        model_chosen=model_chosen,
        model_tier=tier,
        inversion=inversion,
        outcome=outcome,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        notes=notes,
        agent_id=agent_id,
        session_id=session_id,
        step_index=step_index,
        parent_session_id=parent_session_id,
        framework=framework,
    )
