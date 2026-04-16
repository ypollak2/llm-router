"""Background LLM-as-Judge quality evaluation with fire-and-forget pattern.

Evaluates LLM responses using a cheap model (Haiku/Gemini Flash) to score
relevance, completeness, and correctness. Runs asynchronously in background
without blocking the primary task.

Sample rate: LLM_ROUTER_JUDGE_SAMPLE_RATE (default 0.1 = 10% of calls).
Scores stored in routing_decisions table for aggregation and quality penalties.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

from llm_router.cost import _get_db
from llm_router.providers import call_llm


async def evaluate_response_async(
    prompt: str,
    response: str,
    task_type: str,
    routing_decision_id: int | None = None,
) -> None:
    """Fire-and-forget background evaluation of LLM response.

    Runs asynchronously without blocking the primary call. Scores the response
    on relevance, completeness, and correctness using a cheap model, then
    stores the composite score in routing_decisions.

    Args:
        prompt: Original user prompt that generated the response
        response: The LLM response to evaluate
        task_type: Type of task (query, code, generate, analyze, research)
        routing_decision_id: ID of the routing_decisions row to update with score
    """
    # Sample rate check: only evaluate sample_rate% of calls
    sample_rate = float(__import__("os").environ.get("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.1"))
    if random.random() > sample_rate:
        return

    # Fire background task without awaiting
    asyncio.create_task(_evaluate_background(prompt, response, task_type, routing_decision_id))


async def _evaluate_background(
    prompt: str,
    response: str,
    task_type: str,
    routing_decision_id: int | None = None,
) -> None:
    """Background evaluation task — runs independently without blocking caller."""
    try:
        judge_prompt = _build_judge_prompt(prompt, response, task_type)

        # Use cheapest model: Haiku (cheap API) or Gemini Flash Lite
        judge_response = await call_llm(
            model="claude-haiku-4-5-20251001",  # Fallback: gemini/gemini-2.5-flash-lite
            messages=[
                {
                    "role": "user",
                    "content": judge_prompt,
                }
            ],
            temperature=0.0,  # Deterministic scoring
            max_tokens=50,  # Score is short JSON
        )

        # Parse score from response
        score = _parse_judge_score(judge_response.content)

        # Store in database
        if routing_decision_id and score is not None:
            await _store_judge_score(routing_decision_id, score)

    except Exception:
        # Silent failure — judge evaluation is best-effort, never blocks primary task
        pass


def _build_judge_prompt(prompt: str, response: str, task_type: str) -> str:
    """Build prompt for LLM judge evaluation.

    Returns JSON with relevance, completeness, correctness scores (0–1).
    """
    return f"""You are an expert quality evaluator. Rate this response on three dimensions:

USER PROMPT:
{prompt}

RESPONSE:
{response}

TASK TYPE: {task_type}

Evaluate on:
1. Relevance (0–1): Does response address the prompt?
2. Completeness (0–1): Is response sufficiently thorough?
3. Correctness (0–1): Is factual content accurate?

Respond ONLY with valid JSON (no markdown, no explanation):
{{"relevance": 0.X, "completeness": 0.X, "correctness": 0.X}}"""


def _parse_judge_score(response_text: str) -> float | None:
    """Parse composite score from judge response.

    Expects JSON with relevance, completeness, correctness (0–1 each).
    Returns average of three scores, or None if parsing fails.
    """
    import json

    try:
        # Extract JSON from response (may contain extra text)
        response_text = response_text.strip()
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start == -1 or end == 0:
            return None

        json_str = response_text[start:end]
        data = json.loads(json_str)

        # Average the three scores
        relevance = float(data.get("relevance", 0.5))
        completeness = float(data.get("completeness", 0.5))
        correctness = float(data.get("correctness", 0.5))

        composite = (relevance + completeness + correctness) / 3.0
        # Clamp to [0, 1]
        return max(0.0, min(1.0, composite))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


async def _store_judge_score(routing_decision_id: int, score: float) -> None:
    """Store judge score in routing_decisions table."""
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE routing_decisions SET judge_score = ? WHERE id = ?",
            (score, routing_decision_id),
        )
    except Exception:
        pass
    finally:
        await db.close()


async def get_judge_scores_for_model(
    model: str,
    days: int = 30,
) -> dict:
    """Get average judge scores for a model over the past N days.

    Args:
        model: Model name (e.g., 'gpt-4o', 'claude-opus-4-6')
        days: Number of days to aggregate (default 30)

    Returns:
        dict with avg_score, sample_count, min_score, max_score
    """
    db = await _get_db()
    try:
        cutoff = datetime.now() - timedelta(days=days)
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) as sample_count,
                AVG(judge_score) as avg_score,
                MIN(judge_score) as min_score,
                MAX(judge_score) as max_score
            FROM routing_decisions
            WHERE final_model = ? AND judge_score IS NOT NULL AND timestamp >= ?
            """,
            (model, cutoff),
        )
        row = await cursor.fetchone()

        if not row or row[1] is None:
            return {
                "model": model,
                "avg_score": 0.0,
                "sample_count": 0,
                "min_score": 0.0,
                "max_score": 0.0,
                "days": days,
            }

        return {
            "model": model,
            "avg_score": float(row[1]),
            "sample_count": int(row[0]),
            "min_score": float(row[2]) if row[2] is not None else 0.0,
            "max_score": float(row[3]) if row[3] is not None else 0.0,
            "days": days,
        }
    finally:
        await db.close()


async def reorder_by_quality(models: list[str], days: int = 7) -> list[str]:
    """Reorder a model chain by average judge scores, deprioritizing low-quality models.

    Models with average judge score < 0.7 over the past N days are moved to the
    end of the chain. Models with insufficient history (< 3 samples) are unaffected.

    This allows the router to automatically learn from quality feedback and avoid
    repeatedly routing to models that produce poor outputs.

    Args:
        models: Ordered list of model identifiers (provider/model format).
        days: Number of days of history to consider (default 7).

    Returns:
        Reordered model list with low-quality models deprioritized.
        Returns original list unchanged if database is unavailable or has no judge data.
    """
    if not models:
        return models

    try:
        # Get quality scores for each model
        model_quality: dict[str, float] = {}
        model_samples: dict[str, int] = {}

        for model in models:
            try:
                scores = await get_judge_scores_for_model(model, days=days)
                model_quality[model] = scores.get("avg_score", 0.0)
                model_samples[model] = scores.get("sample_count", 0)
            except Exception:
                # If a single model fails, just skip its quality data
                model_quality[model] = 0.0
                model_samples[model] = 0

        # Partition: high quality (≥0.7 or insufficient data) vs low quality (<0.7 with ≥3 samples)
        high_quality = []
        low_quality = []

        for model in models:
            samples = model_samples.get(model, 0)
            quality = model_quality.get(model, 0.0)

            # Keep in original position if: no samples, or quality is acceptable
            if samples < 3 or quality >= 0.7:
                high_quality.append(model)
            else:
                low_quality.append(model)

        # Return reordered: high quality first, then low quality (no removal, just demotion)
        if low_quality:
            from llm_router.logging import get_logger
            log = get_logger("llm_router.judge")
            log.info(
                "Quality-based reordering: demoted %d model(s) due to low avg scores ≥%dd",
                len(low_quality), days
            )
            return high_quality + low_quality

        return models
    except Exception:
        # If anything goes wrong, return original chain unchanged
        return models
