"""Complexity classifier — scores prompts as simple/moderate/complex.

Uses the cheapest available LLM to classify a user prompt into a complexity
tier (simple/moderate/complex) and infer a task type (query/research/generate/
analyze/code). The classification drives downstream model selection: simple
tasks get cheap models, complex tasks get powerful ones.

Results are cached by prompt hash so repeated/similar prompts skip the LLM
call entirely. On total failure, a safe ``moderate`` fallback ensures routing
always proceeds.
"""

from __future__ import annotations

import json
import logging
import re

from llm_router import providers
from llm_router.cache import get_cache
from llm_router.config import get_config
from llm_router.health import get_tracker
from llm_router.profiles import CLASSIFIER_MODELS, provider_from_model
from llm_router.types import ClassificationResult, Complexity, TaskType

log = logging.getLogger("llm_router.classifier")

# System prompt sent to the classifier LLM. Instructs it to return a
# single JSON object with exactly four fields. The prompt is intentionally
# terse and includes a concrete example to maximize compliance with the
# structured output format, even from small/cheap models.
CLASSIFIER_SYSTEM_PROMPT = """\
Classify task complexity and type. Respond with ONLY a single-line JSON object. No markdown, no explanation.

Complexity: "simple" (facts, math, lookups), "moderate" (multi-step, code gen, writing), "complex" (architecture, research synthesis, novel algorithms), "deep_reasoning" (formal proofs, first-principles derivation, philosophical analysis, theorem proving, research synthesis requiring extended chain-of-thought)
Task type: "query", "research", "generate", "analyze", "code"

Example: {"complexity":"simple","task_type":"query","confidence":0.95,"reasoning":"factual lookup"}"""

# Text-only task types that the classifier is allowed to infer. Media types
# (IMAGE, VIDEO, AUDIO) are excluded because they are determined by the
# caller based on explicit user intent, not inferred from prompt content.
VALID_TASK_TYPES = {t.value for t in TaskType if t not in (TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO)}


def _parse_classification(raw: str) -> dict:
    """Extract a JSON dict from the classifier's response text.

    Uses a 4-tier parsing strategy, from strictest to most lenient:
      1. **Direct parse** — the response is clean JSON (ideal case).
      2. **Fence extraction** — the model wrapped JSON in markdown code fences
         (common with instruction-tuned models).
      3. **Regex extraction** — find the first ``{...}`` substring (handles
         models that add preamble text before the JSON).
      4. **Truncated JSON** — extract individual key-value pairs via regex
         when the JSON is incomplete (see ``_parse_truncated_json``).

    Args:
        raw: The raw text content from the classifier LLM response.

    Returns:
        A dict with at least a ``"complexity"`` key.

    Raises:
        ValueError: None of the 4 strategies could extract valid data.
    """
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
    """Extract individual fields from truncated/malformed JSON via regex.

    Thinking models (e.g. Gemini 2.5 Flash with thinking enabled) sometimes
    use most of their output budget on internal chain-of-thought reasoning,
    leaving the actual JSON response truncated mid-string — for example:
    ``{"complexity":"complex","task_type":"code``  (missing closing braces).

    This function salvages whatever key-value pairs are present by matching
    each expected field individually with regex, rather than requiring valid
    JSON structure.

    Args:
        raw: The raw (possibly truncated) response text.

    Returns:
        A dict with extracted fields if at least ``"complexity"`` was found,
        or None if the response is too garbled to extract anything useful.
    """
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
    """Classify a prompt's complexity using the cheapest available classifier model.

    Flow:
      1. **Cache check** — look up the (prompt, quality_mode, min_model) hash
         in the LRU cache. On hit, return immediately (no LLM call).
      2. **Model chain** — iterate through ``CLASSIFIER_MODELS`` in order
         (cheapest first), skipping any whose provider lacks an API key.
      3. **LLM call** — send the prompt with ``CLASSIFIER_SYSTEM_PROMPT`` at
         temperature 0 for deterministic output.
      4. **Parse** — extract the JSON classification via ``_parse_classification``.
      5. **Cache store** — persist the result for future lookups.
      6. **Fallback** — if all models fail, return a safe moderate/balanced
         result so the routing pipeline never stalls.

    Args:
        prompt: The user's prompt text to classify.
        quality_mode: Routing quality preference (used as part of the cache key
            because the same prompt may be classified differently under
            different quality modes in future versions).
        min_model: Minimum model floor (included in the cache key for the same
            reason as quality_mode).

    Returns:
        A ``ClassificationResult`` with complexity, confidence, task type,
        and metadata about the classifier call (model, cost, latency).
    """
    # Check cache first
    cache = get_cache()
    cached = await cache.get(prompt, quality_mode, min_model)
    if cached is not None:
        log.info("Classification cache hit (%.0f%% confidence)", cached.confidence * 100)
        return cached

    config = get_config()
    available = config.available_providers
    tracker = get_tracker()

    # Sort classifier models: healthy providers first, then by static list order.
    # This avoids repeatedly trying a provider whose circuit breaker is open,
    # which would waste latency on a call that is guaranteed to fail.
    def _classifier_sort_key(model: str) -> tuple[int, int]:
        provider = provider_from_model(model)
        healthy = 0 if tracker.is_healthy(provider) else 1
        position = CLASSIFIER_MODELS.index(model) if model in CLASSIFIER_MODELS else 99
        return (healthy, position)

    models_to_try = sorted(
        [m for m in CLASSIFIER_MODELS if provider_from_model(m) in available],
        key=_classifier_sort_key,
    )

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
                # Map legacy "complex" responses that might say "deep" to DEEP_REASONING
                if "deep" in str(complexity_val).lower():
                    complexity = Complexity.DEEP_REASONING
                else:
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
    """Create a safe fallback classification when all classifier models fail.

    Returns ``moderate`` complexity with zero confidence, which maps to the
    ``balanced`` routing profile. This is the safest middle ground: it avoids
    over-spending (opus) while still providing reasonable quality (sonnet).

    Args:
        reason: Human-readable explanation of why fallback was triggered.

    Returns:
        A ``ClassificationResult`` with moderate complexity and zero confidence.
    """
    return ClassificationResult(
        complexity=Complexity.MODERATE,
        confidence=0.0,
        reasoning=f"fallback: {reason}",
        inferred_task_type=None,
        classifier_model="none",
        classifier_cost_usd=0.0,
        classifier_latency_ms=0.0,
    )
