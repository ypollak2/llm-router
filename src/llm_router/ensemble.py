"""LLM-first ensemble classifier — the router's front frontier.

Design (locked with the user, 2026-07):

    classify_ensemble(prompt)
        │  1. LLM-FIRST, ALWAYS. A local Ollama model classifies every uncached
        │     prompt ($0, semantic). No regex short-circuit — regex is a vote,
        │     never a skip.
        │  2. BLEND. The deterministic signal engine (classify.py) folds in as a
        │     WEIGHTED vote: LLM weight = its self-reported confidence; heuristic
        │     weight = its normalized signal score. The two ballots are summed.
        │  3. TIEBREAK. Only when the blended margin is thin does a SECOND local
        │     model vote — so the confident majority pays no extra latency.
        ▼
    ClassificationResult  (same shape as classifier.py → callers unchanged)

Why blend rather than trust the LLM outright: a local 7B model alone mislabels
structural prompts (measured: "Compare Kafka and SQS…" → query/moderate, gold
is analyze/complex). The regex engine nails task_type on exactly those. Neither
wins alone; the weighted sum beats both (see scripts/eval_router_ensemble.py).

Complexity is biased toward the HIGHER of the two estimates when the primary is
unsure — under-classification is the expensive mistake (route complex→cheap→fail
→bounce back to Claude), which is the failure the user is trying to kill.
"""
from __future__ import annotations

import asyncio
import os

from llm_router import providers
from llm_router.classifier import (
    CLASSIFIER_SYSTEM_PROMPT,
    _parse_classification,
    _fallback_result,
)
from llm_router.classify import _COMPLEXITY_RANK, apply_complexity_floor, classify_signals
from llm_router.logging import get_logger
from llm_router.sanitization import sanitize_prompt
from llm_router.types import ClassificationResult, Complexity, Subject, TaskType

log = get_logger("llm_router.ensemble")

# Task types the classifier may infer (media types are caller-driven, not inferred).
_VALID_TASK_TYPES = {t.value for t in TaskType if t not in (TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO)}

# Heuristic score at which the regex signal earns a full 1.0 vote. intent(3)+topic(2)
# = 5 is a strong two-layer hit; 6 keeps a single-layer hit (score 3) at 0.5 weight.
_SCORE_NORM = 6.0

# When the winning task_type's lead over the runner-up is thinner than this
# (as a share of total ballot weight), consult the second local model.
_MARGIN_TIEBREAK = 0.34

# Above this primary confidence we trust the LLM's complexity outright; below it,
# we take the higher of {LLM, heuristic} to avoid under-classification.
_TRUST_PRIMARY_COMPLEXITY = 0.75

# Complexity floor policy lives in llm_router.classify (single source of truth,
# shared with the sync heuristic path). Imported above as apply_complexity_floor.


def _ensemble_enabled() -> bool:
    """LLM-first ensemble is the default routing classifier (user choice, 2026-07).
    Disable with LLM_ROUTER_ENSEMBLE=off to fall back to the cloud classifier."""
    return os.environ.get("LLM_ROUTER_ENSEMBLE", "on").strip().lower() in ("1", "true", "on", "yes")


# Primary local classifier — one env knob shared by warmup and routing so they
# always target the same model.
DEFAULT_PRIMARY = "ollama/qwen2.5:7b"


def _primary_model() -> str:
    return os.environ.get("LLM_ROUTER_ENSEMBLE_PRIMARY", DEFAULT_PRIMARY)


def warm_primary(model: str | None = None) -> None:
    """Fire-and-forget cold-start mitigation: load the primary local classifier
    into memory NOW so the first routed prompt pays warm latency (~2.5s) instead
    of cold start (~56s). Ollama keeps a model resident ~5min after a call, which
    covers the start of a session. Runs in a daemon thread so it never blocks
    startup; all failures are swallowed. No-op when the ensemble is disabled or the
    primary is not a local (ollama) model.
    """
    if not _ensemble_enabled():
        return
    target = model or _primary_model()
    if not target.startswith("ollama/"):
        return

    def _run() -> None:
        try:
            asyncio.run(local_llm_classify("warmup ping", target, timeout_seconds=90.0))
        except Exception:  # noqa: BLE001 — warmup is best-effort
            pass

    import threading

    threading.Thread(target=_run, name="llm_router-ollama-warmup", daemon=True).start()


async def classify_for_routing(prompt: str, **_legacy_kwargs) -> "ClassificationResult":
    """Routing-path classification entry point used by the MCP tools.

    LLM-first: when LLM_ROUTER_ENSEMBLE is on (default), the local ensemble classifies
    every prompt and — on cold start/model failure — degrades INTERNALLY to the
    floor'd heuristic (never stalls, never bounces). When disabled, defers to the
    cloud ``classify_complexity``. Legacy kwargs (timeout_seconds, quality_mode,
    min_model) are accepted for call-site compatibility; the ensemble uses its own
    timeout because a 10s cap would kill it on a cold local model.
    """
    if _ensemble_enabled():
        timeout = float(os.environ.get("LLM_ROUTER_ENSEMBLE_TIMEOUT", "120"))
        return await classify_ensemble(prompt, primary=_primary_model(), timeout_seconds=timeout)
    from llm_router.classifier import classify_complexity

    return await classify_complexity(prompt, **_legacy_kwargs)


async def local_llm_classify(
    prompt: str,
    model: str,
    *,
    timeout_seconds: float = 120.0,
) -> ClassificationResult:
    """Classify ``prompt`` with a single local model, bypassing the availability
    filter (local Ollama models have no API key, so ``config.available_providers``
    omits them unless ``ollama_base_url`` is set — but ``providers.call_llm`` can
    still reach them directly).

    Never raises: any failure returns the safe moderate fallback so routing
    proceeds.
    """
    try:
        sanitized = sanitize_prompt(prompt)
    except ValueError as e:
        log.warning("ensemble: sanitization failed: %s", e)
        return _fallback_result(f"sanitization failed: {e}")

    messages = [
        {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": sanitized},
    ]
    try:
        async with asyncio.timeout(timeout_seconds):
            resp = await providers.call_llm(
                model=model, messages=messages, temperature=0.0, max_tokens=256
            )
        parsed = _parse_classification(resp.content)
    except Exception as e:  # noqa: BLE001 — classification must never stall routing
        log.warning("ensemble: local classify via %s failed: %s", model, e)
        return _fallback_result(f"local classify failed: {e}")

    return _result_from_parsed(parsed, model, resp.cost_usd, resp.latency_ms)


def _result_from_parsed(
    parsed: dict, model: str, cost_usd: float, latency_ms: float
) -> ClassificationResult:
    try:
        complexity = Complexity(parsed.get("complexity", "moderate"))
    except ValueError:
        complexity = (
            Complexity.DEEP_REASONING
            if "deep" in str(parsed.get("complexity", "")).lower()
            else Complexity.MODERATE
        )
    task_val = parsed.get("task_type", "query")
    inferred = TaskType(task_val) if task_val in _VALID_TASK_TYPES else None
    confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.5) or 0.0)))
    try:
        subject = Subject(parsed.get("subject", "general"))
    except (ValueError, TypeError):
        subject = Subject.GENERAL
    return ClassificationResult(
        complexity=complexity,
        confidence=confidence,
        reasoning=str(parsed.get("reasoning", "")),
        inferred_task_type=inferred,
        classifier_model=model,
        classifier_cost_usd=cost_usd,
        classifier_latency_ms=latency_ms,
        subject=subject,
    )


def _add_vote(votes: dict[TaskType, float], task: TaskType | None, weight: float) -> None:
    if task is None or weight <= 0:
        return
    votes[task] = votes.get(task, 0.0) + weight


def _winner(votes: dict[TaskType, float]) -> tuple[TaskType | None, float]:
    """Return (winning_task, margin) where margin is the winner's lead as a share
    of total ballot weight (0.0-1.0). Empty ballot → (None, 0.0)."""
    if not votes:
        return None, 0.0
    total = sum(votes.values())
    ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
    top_task, top_w = ranked[0]
    second_w = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = (top_w - second_w) / total if total else 0.0
    return top_task, margin


def _blend_complexity(
    primary: ClassificationResult, heur_complexity: Complexity, task_type: TaskType | None
) -> Complexity:
    """Blend the LLM's and heuristic's complexity, then clamp UP to the task-type
    floor. Trust the LLM outright when confident; otherwise take the higher of
    {LLM, heuristic}. Either way the task-type floor prevents under-routing."""
    if primary.confidence >= _TRUST_PRIMARY_COMPLEXITY:
        blended = primary.complexity
    else:
        blended = max(primary.complexity, heur_complexity, key=lambda c: _COMPLEXITY_RANK.get(c, 0))
    return apply_complexity_floor(blended, task_type) if task_type else blended


async def classify_ensemble(
    prompt: str,
    *,
    primary: str = "ollama/qwen2.5:7b",
    secondary: str | None = "ollama/qwen2.5-coder:32b",
    allow_secondary: bool = True,
    timeout_seconds: float = 120.0,
) -> ClassificationResult:
    """LLM-first ensemble: local primary + weighted heuristic, second-model
    tiebreak on the low-margin tail. Returns a ``ClassificationResult``."""
    heur = classify_signals(prompt)
    primary_res = await local_llm_classify(prompt, primary, timeout_seconds=timeout_seconds)

    votes: dict[TaskType, float] = {}
    # LLM ballot: weight = self-reported confidence (floor 0.1 so a 0-confidence
    # but non-null label still counts a little).
    _add_vote(votes, primary_res.inferred_task_type, max(primary_res.confidence, 0.1))
    # Heuristic ballot: weight = normalized signal score (0 when it found nothing).
    _add_vote(votes, heur.task_type, min(1.0, heur.score / _SCORE_NORM))

    winner, margin = _winner(votes)
    models_used = [primary_res.classifier_model]
    latency = primary_res.classifier_latency_ms
    cost = primary_res.classifier_cost_usd

    if allow_secondary and secondary and margin < _MARGIN_TIEBREAK:
        sec = await local_llm_classify(prompt, secondary, timeout_seconds=timeout_seconds)
        _add_vote(votes, sec.inferred_task_type, max(sec.confidence, 0.1))
        winner, margin = _winner(votes)
        models_used.append(sec.classifier_model)
        latency += sec.classifier_latency_ms
        cost += sec.classifier_cost_usd

    task_type = winner if winner is not None else (heur.task_type or TaskType.QUERY)
    complexity = _blend_complexity(primary_res, heur.complexity, task_type)
    total = sum(votes.values())
    confidence = (votes.get(task_type, 0.0) / total) if total else primary_res.confidence

    return ClassificationResult(
        complexity=complexity,
        confidence=confidence,
        reasoning=f"ensemble({'+'.join(models_used)}); margin={margin:.2f}",
        inferred_task_type=task_type,
        classifier_model="ensemble:" + "+".join(models_used),
        classifier_cost_usd=cost,
        classifier_latency_ms=latency,
        subject=primary_res.subject,
    )
