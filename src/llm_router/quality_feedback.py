"""Post-route quality feedback — automatic response scoring and model quality tracking.

Sprint 4 of the context preparation pipeline. This module:
1. Auto-scores every routed response using heuristics (no LLM call needed)
2. Tracks per-model quality scores by task type and complexity
3. Exposes quality data for routing decisions (skip underperforming models)

The key insight: quality scoring doesn't need an LLM — simple content heuristics
(code blocks present? Non-empty? No refusal phrases?) produce a reliable 0–1 signal.
Over many calls, this signal reliably identifies models that fail at specific task types.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from llm_router.logging import get_logger
from llm_router.token_budget import estimate_tokens

log = get_logger("llm_router.quality_feedback")

# ── Refusal detection ────────────────────────────────────────────────────────
_REFUSAL_PHRASES = [
    "i cannot",
    "i can't",
    "i don't have access",
    "i'm unable to",
    "i am unable to",
    "as an ai",
    "i'm not able to",
    "i apologize, but i cannot",
    "sorry, i cannot",
    "i don't have the ability",
]

_CODE_BLOCK_RE = re.compile(r"```[\s\S]{10,}?```")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_HEADING_RE = re.compile(r"^#{1,4}\s+\S", re.MULTILINE)


@dataclass(frozen=True)
class QualityScore:
    """Quality assessment of a single routed response."""

    score: float  # 0.0–1.0
    reasons: tuple[str, ...]  # Human-readable reasons for the score
    task_type: str
    model: str
    tokens: int


@dataclass
class ModelQuality:
    """Tracked quality stats for a model on a specific task pattern."""

    model: str
    task_type: str
    complexity: str
    total_score: float = 0.0
    call_count: int = 0
    last_updated: float = field(default_factory=time.time)

    @property
    def avg_quality(self) -> float:
        if self.call_count == 0:
            return 0.5  # Prior: assume neutral
        return self.total_score / self.call_count

    def record(self, score: float) -> None:
        self.total_score += score
        self.call_count += 1
        self.last_updated = time.time()


# ── In-memory quality store ──────────────────────────────────────────────────
# Key: (model, task_type, complexity)
_quality_store: dict[tuple[str, str, str], ModelQuality] = {}

# Minimum calls before we trust the quality signal
_MIN_CALLS_FOR_SIGNAL = 3

# Quality threshold below which a model is considered underperforming
QUALITY_THRESHOLD = 0.4


def score_response(
    response: str,
    task_type: str,
    model: str = "",
    complexity: str = "moderate",
) -> QualityScore:
    """Score a routed response using content heuristics.

    Scoring rubric (additive, 0.0–1.0):
      - Base: 0.1 (response exists and is non-empty)
      - Length: +0.1 if > 20 tokens
      - No refusal: +0.2 if no refusal phrases detected
      - Code (code tasks): +0.3 if contains code blocks
      - Language match (code): +0.1 if code matches likely language
      - Structure (non-code): +0.2 if has headings/lists
      - Citations (research): +0.2 if contains URLs
      - Completeness: +0.1 if response doesn't end mid-sentence

    Args:
        response: The model's response text.
        task_type: Task type (code, query, analyze, research, generate).
        model: Model identifier (for tracking).
        complexity: Complexity level.

    Returns:
        QualityScore with 0.0–1.0 score and reasons.
    """
    if not response or not response.strip():
        return QualityScore(
            score=0.0,
            reasons=("empty response",),
            task_type=task_type,
            model=model,
            tokens=0,
        )

    score = 0.0
    reasons: list[str] = []
    tokens = estimate_tokens(response)
    lower = response.lower()

    # Base: response exists
    score += 0.1
    reasons.append("non-empty")

    # Length check
    if tokens > 20:
        score += 0.1
        reasons.append("sufficient length")

    # Refusal detection
    has_refusal = any(phrase in lower for phrase in _REFUSAL_PHRASES)
    if not has_refusal:
        score += 0.2
        reasons.append("no refusal")
    else:
        reasons.append("contains refusal")

    # Task-specific scoring
    if task_type in ("code", "edit"):
        # Code tasks: check for code blocks
        if _CODE_BLOCK_RE.search(response):
            score += 0.3
            reasons.append("contains code")
        # Bonus for substantial code
        if tokens > 50 and _CODE_BLOCK_RE.search(response):
            score += 0.1
            reasons.append("substantial code")

    elif task_type == "research":
        # Research: check for citations/URLs
        urls = _URL_RE.findall(response)
        if urls:
            score += 0.2
            reasons.append(f"citations ({len(urls)} URLs)")
        # Structure
        if _HEADING_RE.search(response):
            score += 0.1
            reasons.append("structured")

    elif task_type in ("analyze", "query"):
        # Analysis: check for structure and depth
        if _HEADING_RE.search(response) or "\n- " in response or "\n* " in response:
            score += 0.2
            reasons.append("structured")
        if tokens > 100:
            score += 0.1
            reasons.append("detailed")

    elif task_type == "generate":
        # Generation: length and structure
        if tokens > 50:
            score += 0.2
            reasons.append("substantial output")
        if _HEADING_RE.search(response) or "\n\n" in response:
            score += 0.1
            reasons.append("well-formatted")

    # Completeness: doesn't end mid-word/sentence
    stripped = response.rstrip()
    if stripped and stripped[-1] in ".!?`\n\"')]}":
        score += 0.1
        reasons.append("complete")

    # Cap at 1.0
    final_score = min(1.0, score)

    return QualityScore(
        score=final_score,
        reasons=tuple(reasons),
        task_type=task_type,
        model=model,
        tokens=tokens,
    )


def record_quality(
    model: str,
    task_type: str,
    complexity: str,
    score: float,
) -> None:
    """Record a quality score for a model/task/complexity pattern.

    Accumulates scores in memory. Used by the router to avoid
    repeatedly routing to models that fail for specific patterns.
    """
    key = (model, task_type, complexity)
    if key not in _quality_store:
        _quality_store[key] = ModelQuality(
            model=model, task_type=task_type, complexity=complexity
        )
    _quality_store[key].record(score)
    log.debug(
        "quality_recorded",
        model=model,
        task_type=task_type,
        complexity=complexity,
        score=score,
        avg=_quality_store[key].avg_quality,
        count=_quality_store[key].call_count,
    )


def get_model_quality(model: str, task_type: str, complexity: str) -> float | None:
    """Get average quality for a model on a specific task pattern.

    Returns None if insufficient data (fewer than _MIN_CALLS_FOR_SIGNAL calls).
    Returns 0.0–1.0 quality score otherwise.
    """
    key = (model, task_type, complexity)
    entry = _quality_store.get(key)
    if entry is None or entry.call_count < _MIN_CALLS_FOR_SIGNAL:
        return None
    return entry.avg_quality


def should_skip_model(model: str, task_type: str, complexity: str) -> bool:
    """Check if a model should be skipped due to poor quality history.

    Returns True if:
    - Model has >= _MIN_CALLS_FOR_SIGNAL calls for this pattern
    - Average quality < QUALITY_THRESHOLD

    This is the primary feedback mechanism: the router calls this before
    dispatching to each model in the fallback chain.
    """
    quality = get_model_quality(model, task_type, complexity)
    if quality is None:
        return False  # Not enough data — give it a chance
    return quality < QUALITY_THRESHOLD


def get_quality_summary() -> dict[str, dict]:
    """Get a summary of all tracked model quality scores.

    Returns dict of {model: {task_type/complexity: {avg, count}}}
    Useful for the quality report tool.
    """
    summary: dict[str, dict] = {}
    for (model, task_type, complexity), entry in _quality_store.items():
        if model not in summary:
            summary[model] = {}
        key = f"{task_type}/{complexity}"
        summary[model][key] = {
            "avg_quality": round(entry.avg_quality, 3),
            "call_count": entry.call_count,
            "last_updated": entry.last_updated,
        }
    return summary


def reset_quality_store() -> None:
    """Reset all quality tracking data. Useful for testing."""
    _quality_store.clear()


# ── LoopHole verdicts: ground-truth quality ──────────────────────────────────
# The heuristic scorer above guesses quality from response shape. LoopHole gives
# us the real thing: it routes a coding task through LLM Router, then a falsifiable
# verifier (tests pass / build succeeds / curl 200) decides whether the work was
# actually done. We fold that verdict straight into the same store, so
# ``should_skip_model`` orders chains on ground truth, not shape heuristics.
#
# A verdict is a hard signal → map to the score extremes. A "paused" run is
# "not proven", a soft negative rather than a hard failure.
_VERDICT_SCORE = {"done": 1.0, "failed": 0.0, "paused": 0.25}

# LoopHole tasks are code; task complexity travels in a ``llm_router:<tier>`` label.
_TIER_TO_COMPLEXITY = {
    "simple": "simple", "moderate": "moderate", "complex": "complex",
    "auto": "moderate", "reasoning": "complex",
}


def _normalize_loophole_model(label: str) -> str | None:
    """LoopHole labels look like ``ollama:qwen3-coder:30b`` or ``llm_router:complex``.

    Return a concrete ``provider/model`` id, or None for a router alias
    (``llm_router:<tier>``) — LoopHole didn't observe which model the alias resolved
    to, so there's no concrete model to credit or penalize.
    """
    if not label or label.startswith("llm_router:") or label == "unknown":
        return None
    provider, sep, rest = label.partition(":")
    return f"{provider}/{rest}" if sep else None


def _loophole_complexity(record: dict) -> str:
    for key in ("executor_model", "planner_model"):
        label = record.get(key, "") or ""
        if label.startswith("llm_router:"):
            return _TIER_TO_COMPLEXITY.get(label.split(":", 1)[1], "moderate")
    return "moderate"


def record_loophole_verdict(record: dict) -> bool:
    """Fold one LoopHole feedback record into the quality store.

    Returns True if a concrete model's quality was updated; False if the record
    carried only a router alias (nothing concrete to score) or was malformed.
    """
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    if status not in _VERDICT_SCORE:
        status = "done" if record.get("verified_done") else "failed"
    model = _normalize_loophole_model(record.get("executor_model", ""))
    if model is None:
        return False
    record_quality(
        model=model,
        task_type="code",
        complexity=_loophole_complexity(record),
        score=_VERDICT_SCORE[status],
    )
    log.info(
        "loophole_verdict_recorded",
        model=model,
        status=status,
        goal_id=record.get("goal_id"),
    )
    return True


def _loophole_jsonl_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".llm-router", "quality_feedback.jsonl")


def ingest_loophole_jsonl(path: str | None = None, since_offset: int = 0) -> tuple[int, int]:
    """Drain LoopHole's verdict JSONL (its offline fallback) into the store.

    Reads from ``since_offset`` bytes so a caller can poll incrementally.
    Returns ``(records_applied, new_offset)``. A missing file is not an error.
    """
    path = path or _loophole_jsonl_path()
    if not os.path.exists(path):
        return (0, since_offset)
    applied = 0
    with open(path, encoding="utf-8") as f:
        f.seek(since_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                if record_loophole_verdict(json.loads(line)):
                    applied += 1
            except (ValueError, json.JSONDecodeError):
                continue
        new_offset = f.tell()
    return (applied, new_offset)
