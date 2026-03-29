"""Complexity classifier — scores prompts as simple/moderate/complex."""

from __future__ import annotations

import json
import logging
import re

from llm_router import providers
from llm_router.cache import get_cache
from llm_router.config import get_config
from llm_router.profiles import CLASSIFIER_MODELS, provider_from_model
from llm_router.types import ClassificationResult, Complexity, TaskType

log = logging.getLogger("llm_router.classifier")

CLASSIFIER_SYSTEM_PROMPT = """\
Classify task complexity and type. Respond with ONLY a single-line JSON object. No markdown, no explanation.

Complexity: "simple" (facts, math, lookups), "moderate" (multi-step, code gen, writing), "complex" (architecture, research synthesis, novel algorithms)
Task type: "query", "research", "generate", "analyze", "code"

Example: {"complexity":"simple","task_type":"query","confidence":0.95,"reasoning":"factual lookup"}"""

VALID_TASK_TYPES = {t.value for t in TaskType if t not in (TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO)}


def _parse_classification(raw: str) -> dict:
    """Extract JSON from classifier response, handling markdown fences and truncation."""
    # Try direct parse
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass
    # Try extracting from code fences
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding any JSON object
    match = re.search(r"\{[^{}]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Handle truncated JSON — extract key-value pairs with regex
    result = _parse_truncated_json(raw)
    if result:
        return result
    raise ValueError(f"Could not parse JSON from classifier response: {raw[:200]}")


def _parse_truncated_json(raw: str) -> dict | None:
    """Extract fields from truncated JSON like {"complexity":"complex","task_type":"code."""
    result = {}
    for key in ("complexity", "task_type", "confidence", "reasoning"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]*)"', raw)
        if match:
            result[key] = match.group(1)
        else:
            # Try numeric values (for confidence)
            match = re.search(rf'"{key}"\s*:\s*([\d.]+)', raw)
            if match:
                result[key] = float(match.group(1))
    if "complexity" in result:
        return result
    return None


async def classify_complexity(
    prompt: str,
    quality_mode: str = "balanced",
    min_model: str = "haiku",
) -> ClassificationResult:
    """Classify a prompt's complexity using the cheapest available model.

    Results are cached by (prompt, quality_mode, min_model) hash for O(1) repeat lookups.
    On failure, returns a moderate/balanced fallback so routing always proceeds.
    """
    # Check cache first
    cache = get_cache()
    cached = await cache.get(prompt, quality_mode, min_model)
    if cached is not None:
        log.info("Classification cache hit (%.0f%% confidence)", cached.confidence * 100)
        return cached

    config = get_config()
    available = config.available_providers

    models_to_try = [
        m for m in CLASSIFIER_MODELS if provider_from_model(m) in available
    ]

    if not models_to_try:
        log.warning("No classifier models available, defaulting to moderate")
        return _fallback_result("no classifier models available")

    messages = [
        {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    last_error: Exception | None = None
    for model in models_to_try:
        try:
            resp = await providers.call_llm(
                model=model,
                messages=messages,
                temperature=0.0,
                max_tokens=256,
            )
            parsed = _parse_classification(resp.content)

            complexity_val = parsed.get("complexity", "moderate")
            try:
                complexity = Complexity(complexity_val)
            except ValueError:
                complexity = Complexity.MODERATE

            task_type_val = parsed.get("task_type", "query")
            inferred_task = TaskType(task_type_val) if task_type_val in VALID_TASK_TYPES else None

            confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.5))))
            reasoning = parsed.get("reasoning", "")

            result = ClassificationResult(
                complexity=complexity,
                confidence=confidence,
                reasoning=reasoning,
                inferred_task_type=inferred_task,
                classifier_model=model,
                classifier_cost_usd=resp.cost_usd,
                classifier_latency_ms=resp.latency_ms,
            )
            # Cache successful classification
            await cache.put(prompt, result, quality_mode, min_model)
            return result

        except Exception as e:
            log.warning("Classifier model %s failed: %s", model, e)
            last_error = e
            continue

    log.warning("All classifier models failed, defaulting to moderate. Last error: %s", last_error)
    return _fallback_result(f"all models failed: {last_error}")


def _fallback_result(reason: str) -> ClassificationResult:
    """Safe fallback — moderate complexity, balanced profile."""
    return ClassificationResult(
        complexity=Complexity.MODERATE,
        confidence=0.0,
        reasoning=f"fallback: {reason}",
        inferred_task_type=None,
        classifier_model="none",
        classifier_cost_usd=0.0,
        classifier_latency_ms=0.0,
    )
