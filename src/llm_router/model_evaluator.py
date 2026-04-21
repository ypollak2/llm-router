"""Model performance evaluator — benchmarks local + remote LLMs automatically.

Tests each available model (Ollama, Codex, API-based) with a suite of
benchmark prompts, scores on speed + quality, and updates routing priorities
based on real performance data.

Results cached to ~/.llm-router/model_evals.json (7-day TTL).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from llm_router.logging import get_logger
from llm_router.codex_agent import is_codex_available, run_codex
from llm_router.config import get_config

log = get_logger("llm_router.model_evaluator")

EVAL_CACHE_PATH = Path.home() / ".llm-router" / "model_evals.json"
EVAL_TTL_DAYS = 7
EVAL_TTL_SECONDS = EVAL_TTL_DAYS * 86400


@dataclass
class ModelScore:
    """Performance metrics for a single model."""
    model: str
    provider: str  # "ollama", "codex", "openai", "gemini", etc.
    latency_ms: float
    quality: float  # 0.0-1.0 (user feedback or heuristic assessment)
    accuracy: float  # 0.0-1.0
    timestamp: float
    task_type: str  # "code", "reasoning", "simple"


@dataclass
class EvaluationResult:
    """Complete evaluation run."""
    timestamp: float
    models: dict[str, ModelScore]  # model_name -> score
    total_evals: int


class BenchmarkSuite:
    """Predefined benchmark tasks for model evaluation."""
    
    TASKS = {
        "simple": {
            "prompt": "What is 2 + 2?",
            "expected_in": ["4"],
            "max_latency_ms": 500,
        },
        "reasoning": {
            "prompt": "Explain why recursion works in programming. Keep it to 2 sentences.",
            "expected_in": ["function", "call", "base case"],
            "max_latency_ms": 2000,
        },
        "code": {
            "prompt": "Write a Python function that checks if a string is a palindrome.",
            "expected_in": ["def", "return", "=="],
            "max_latency_ms": 3000,
        },
    }

    @classmethod
    def get_prompt(cls, task_type: str) -> str:
        """Get benchmark prompt for task type."""
        return cls.TASKS[task_type]["prompt"]

    @classmethod
    def assess_response(cls, task_type: str, response: str) -> float:
        """Score response quality 0.0-1.0 based on expected keywords."""
        task = cls.TASKS[task_type]
        expected = task["expected_in"]
        
        # Count how many expected keywords appear
        matches = sum(1 for keyword in expected if keyword.lower() in response.lower())
        return min(1.0, matches / len(expected))


async def eval_ollama_model(model: str, task_type: str = "reasoning") -> Optional[ModelScore]:
    """Evaluate a single Ollama model on a benchmark task."""
    import urllib.request
    import json as json_lib
    
    OLLAMA_URL = "http://localhost:11434"
    OLLAMA_TIMEOUT = 10
    
    try:
        prompt = BenchmarkSuite.get_prompt(task_type)
        
        start = time.time()
        body = json_lib.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 100},
        }).encode()
        
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json_lib.loads(resp.read())
            response = result.get("message", {}).get("content", "")
            latency_ms = (time.time() - start) * 1000
            
            quality = BenchmarkSuite.assess_response(task_type, response)
            
            return ModelScore(
                model=model,
                provider="ollama",
                latency_ms=latency_ms,
                quality=quality,
                accuracy=1.0 if quality > 0.5 else 0.0,
                timestamp=time.time(),
                task_type=task_type,
            )
    except Exception as e:
        log.warning(f"Failed to evaluate Ollama model {model}: {e}")
        return None


async def eval_codex_model(task_type: str = "reasoning") -> Optional[ModelScore]:
    """Evaluate Codex CLI on a benchmark task."""
    if not is_codex_available():
        return None
    
    try:
        prompt = BenchmarkSuite.get_prompt(task_type)
        
        start = time.time()
        result = await run_codex(prompt, model="gpt-5.4", timeout=10)
        latency_ms = (time.time() - start) * 1000
        
        if not result.success:
            return None
        
        quality = BenchmarkSuite.assess_response(task_type, result.content)
        
        return ModelScore(
            model="codex/gpt-5.4",
            provider="codex",
            latency_ms=latency_ms,
            quality=quality,
            accuracy=1.0 if quality > 0.7 else 0.0,
            timestamp=time.time(),
            task_type=task_type,
        )
    except Exception as e:
        log.warning(f"Failed to evaluate Codex: {e}")
        return None


def load_eval_cache() -> Optional[EvaluationResult]:
    """Load cached evaluation results if fresh (< 7 days old)."""
    if not EVAL_CACHE_PATH.exists():
        return None
    
    try:
        data = json.loads(EVAL_CACHE_PATH.read_text())
        timestamp = data.get("timestamp", 0)
        age_seconds = time.time() - timestamp
        
        if age_seconds > EVAL_TTL_SECONDS:
            log.info(f"Evaluation cache expired ({age_seconds/86400:.1f} days old)")
            return None
        
        # Reconstruct ModelScore objects
        models = {}
        for model_name, score_dict in data.get("models", {}).items():
            models[model_name] = ModelScore(**score_dict)
        
        return EvaluationResult(
            timestamp=timestamp,
            models=models,
            total_evals=data.get("total_evals", 0),
        )
    except Exception as e:
        log.warning(f"Failed to load eval cache: {e}")
        return None


def save_eval_cache(result: EvaluationResult) -> None:
    """Save evaluation results to cache."""
    EVAL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "timestamp": result.timestamp,
        "total_evals": result.total_evals,
        "models": {
            name: asdict(score)
            for name, score in result.models.items()
        },
    }
    
    EVAL_CACHE_PATH.write_text(json.dumps(data, indent=2))
    log.info(f"Saved model evaluations to {EVAL_CACHE_PATH}")


async def evaluate_available_models(task_types: list[str] = None) -> EvaluationResult:
    """Benchmark all available models on specified task types.
    
    Args:
        task_types: List of task types to evaluate ("simple", "reasoning", "code")
                   Defaults to all task types.
    
    Returns:
        EvaluationResult with scores for all models.
    """
    if task_types is None:
        task_types = ["reasoning", "code"]  # Skip simple, too trivial
    
    # Check cache first
    cached = load_eval_cache()
    if cached:
        log.info(f"Using cached model evaluations ({len(cached.models)} models)")
        return cached
    
    log.info("Starting model evaluation benchmark...")
    
    result = EvaluationResult(
        timestamp=time.time(),
        models={},
        total_evals=0,
    )
    
    cfg = get_config()
    
    # Find available Ollama models
    ollama_models = []
    if cfg.ollama_base_url:
        ollama_models = [
            "qwen3.5:latest",
            "qwen3-coder-next",
            "kimi-k2.6:cloud",
            "qwen2.5:latest",
            "gemma4:latest",
        ]
    
    # Evaluate each model on each task type
    for task_type in task_types:
        log.info(f"Evaluating models on '{task_type}' task...")
        
        # Ollama models
        for model in ollama_models:
            score = await eval_ollama_model(model, task_type)
            if score:
                key = f"ollama/{model}"
                result.models[key] = score
                result.total_evals += 1
                log.debug(f"  {key}: {score.latency_ms:.0f}ms, quality={score.quality:.2f}")
        
        # Codex
        score = await eval_codex_model(task_type)
        if score:
            result.models[score.model] = score
            result.total_evals += 1
            log.debug(f"  {score.model}: {score.latency_ms:.0f}ms, quality={score.quality:.2f}")
    
    save_eval_cache(result)
    return result


def get_best_model_for_task(task_type: str, provider_filter: list[str] = None) -> Optional[str]:
    """Get best performing model for a specific task type.
    
    Args:
        task_type: "code", "reasoning", or "simple"
        provider_filter: Optional list of providers to consider ("ollama", "codex", etc.)
    
    Returns:
        Model name (e.g., "ollama/qwen3.5:latest") or None if no models evaluated.
    """
    cached = load_eval_cache()
    if not cached or not cached.models:
        return None
    
    # Filter by provider if specified
    candidates = {
        name: score
        for name, score in cached.models.items()
        if score.task_type == task_type
        and (not provider_filter or score.provider in provider_filter)
    }
    
    if not candidates:
        return None
    
    # Sort by quality first, then by latency
    best = max(
        candidates.items(),
        key=lambda x: (x[1].quality, -x[1].latency_ms)
    )
    
    return best[0]


def display_evaluation_results(result: EvaluationResult) -> str:
    """Format evaluation results for display."""
    if not result.models:
        return "No model evaluations available."
    
    output = "\n📊 Model Evaluation Results\n"
    output += "─" * 70 + "\n\n"
    
    # Group by task type
    by_task = {}
    for name, score in result.models.items():
        if score.task_type not in by_task:
            by_task[score.task_type] = []
        by_task[score.task_type].append((name, score))
    
    for task_type in sorted(by_task.keys()):
        output += f"**{task_type.upper()}**\n"
        
        # Sort by quality (descending)
        sorted_scores = sorted(
            by_task[task_type],
            key=lambda x: x[1].quality,
            reverse=True
        )
        
        for name, score in sorted_scores:
            quality_bar = "█" * int(score.quality * 10) + "░" * (10 - int(score.quality * 10))
            output += f"  {name:30} {quality_bar} {score.quality:.1%} ({score.latency_ms:.0f}ms)\n"
        
        output += "\n"
    
    age_hours = (time.time() - result.timestamp) / 3600
    output += f"Evaluated at: {age_hours:.1f} hours ago\n"
    
    return output
