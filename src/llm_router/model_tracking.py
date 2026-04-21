"""Model usage tracking — logs every routing decision with classification & context.

Records:
- Selected model for each task
- Task classification (type + complexity)
- Classification method (heuristic/ollama/api/fallback)
- Timestamp and routing context
- Later: optional quality feedback

Stored to ~/.llm-router/model_tracking.jsonl for persistent analysis.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from llm_router.logging import get_logger

log = get_logger("llm_router.model_tracking")

TRACKING_PATH = Path.home() / ".llm-router" / "model_tracking.jsonl"


@dataclass
class RoutingDecision:
    """Complete record of a single routing decision."""
    timestamp: float  # Unix timestamp
    task_type: str  # "code", "query", "research", "analyze", "generate"
    complexity: str  # "simple", "moderate", "complex", "deep_reasoning"
    classification_method: str  # "heuristic", "ollama", "api", "weak_heuristic", "fallback", "build_task"
    classification_confidence: float  # 0.0-1.0 (from heuristic scores or LLM confidence)
    selected_model: str  # "ollama/qwen3.5:latest", "openai/gpt-4o", etc.
    provider: str  # "ollama", "openai", "gemini", "codex", "claude", "groq", "deepseek"
    chain_position: int  # Position in fallback chain (1 = first choice, 2 = second, etc.)
    chain_length: int  # Total length of fallback chain tried
    quota_pressure: Optional[float] = None  # Claude subscription pressure (0.0-1.0) if available
    cost_usd_estimate: Optional[float] = None  # Estimated cost for this model
    quality_feedback: Optional[float] = None  # 0.0-1.0, set later via llm_rate
    notes: Optional[str] = None  # Additional context


def log_routing_decision(
    task_type: str,
    complexity: str,
    classification_method: str,
    selected_model: str,
    provider: str,
    chain_position: int = 1,
    chain_length: int = 1,
    classification_confidence: float = 0.0,
    quota_pressure: Optional[float] = None,
    cost_usd_estimate: Optional[float] = None,
    notes: Optional[str] = None,
) -> None:
    """Log a routing decision to the tracking file.
    
    Called by router.py and hooks whenever a model is selected.
    
    Args:
        task_type: Task classification ("code", "query", "analyze", etc.)
        complexity: Complexity level ("simple", "moderate", "complex")
        classification_method: How was task_type determined
        selected_model: Model name (e.g., "ollama/qwen3.5:latest")
        provider: Provider name ("ollama", "openai", etc.)
        chain_position: Position in fallback chain (1st choice, 2nd, etc.)
        chain_length: Total models in the fallback chain
        classification_confidence: Confidence 0-1.0
        quota_pressure: Claude subscription pressure if available
        cost_usd_estimate: Estimated cost for this model
        notes: Optional context (e.g., "under budget pressure", "code task detected")
    """
    try:
        decision = RoutingDecision(
            timestamp=time.time(),
            task_type=task_type,
            complexity=complexity,
            classification_method=classification_method,
            classification_confidence=classification_confidence,
            selected_model=selected_model,
            provider=provider,
            chain_position=chain_position,
            chain_length=chain_length,
            quota_pressure=quota_pressure,
            cost_usd_estimate=cost_usd_estimate,
            notes=notes,
        )
        
        TRACKING_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        # Append to JSONL file (atomic, append-only)
        with open(TRACKING_PATH, "a") as f:
            f.write(json.dumps(asdict(decision)) + "\n")
        
        log.debug(f"Tracked: {selected_model} for {task_type}/{complexity} ({classification_method})")
    except Exception as e:
        log.warning(f"Failed to log routing decision: {e}")


def load_tracking_data(limit: int = 1000) -> list[RoutingDecision]:
    """Load recent routing decisions from tracking file.
    
    Args:
        limit: Maximum number of recent records to load
    
    Returns:
        List of RoutingDecision objects, most recent first
    """
    if not TRACKING_PATH.exists():
        return []
    
    decisions = []
    try:
        # Read file in reverse order for efficiency
        with open(TRACKING_PATH, "r") as f:
            lines = f.readlines()
        
        # Take last N lines, then reverse to get newest first
        for line in reversed(lines[-limit:]):
            if line.strip():
                try:
                    data = json.loads(line)
                    decisions.append(RoutingDecision(**data))
                except Exception as e:
                    log.warning(f"Failed to parse tracking record: {e}")
        
        # Reverse back so we return in original order (newest first not needed here)
        decisions.reverse()
    except Exception as e:
        log.warning(f"Failed to load tracking data: {e}")
    
    return decisions


def get_model_usage_stats(hours: int = 24) -> dict[str, int]:
    """Count how many times each model was selected in the last N hours.
    
    Args:
        hours: Look back this many hours
    
    Returns:
        Dict mapping model names to selection counts
    """
    cutoff_time = time.time() - (hours * 3600)
    decisions = load_tracking_data(limit=10000)
    
    stats = {}
    for decision in decisions:
        if decision.timestamp >= cutoff_time:
            stats[decision.selected_model] = stats.get(decision.selected_model, 0) + 1
    
    return dict(sorted(stats.items(), key=lambda x: x[1], reverse=True))


def get_task_type_distribution(hours: int = 24) -> dict[str, int]:
    """Count how many times each task type was routed in the last N hours.
    
    Args:
        hours: Look back this many hours
    
    Returns:
        Dict mapping task types to counts
    """
    cutoff_time = time.time() - (hours * 3600)
    decisions = load_tracking_data(limit=10000)
    
    stats = {}
    for decision in decisions:
        if decision.timestamp >= cutoff_time:
            key = f"{decision.task_type}/{decision.complexity}"
            stats[key] = stats.get(key, 0) + 1
    
    return dict(sorted(stats.items(), key=lambda x: x[1], reverse=True))


def get_classification_method_stats(hours: int = 24) -> dict[str, int]:
    """Count how many times each classification method was used.
    
    Args:
        hours: Look back this many hours
    
    Returns:
        Dict mapping classification method names to counts
    """
    cutoff_time = time.time() - (hours * 3600)
    decisions = load_tracking_data(limit=10000)
    
    stats = {}
    for decision in decisions:
        if decision.timestamp >= cutoff_time:
            stats[decision.classification_method] = stats.get(decision.classification_method, 0) + 1
    
    return dict(sorted(stats.items(), key=lambda x: x[1], reverse=True))


def get_model_success_rate(model: str, hours: int = 24) -> dict:
    """Get success rate and avg quality feedback for a model.
    
    Args:
        model: Model name to analyze
        hours: Look back this many hours
    
    Returns:
        Dict with: count, avg_quality_feedback, min_quality, max_quality
    """
    cutoff_time = time.time() - (hours * 3600)
    decisions = load_tracking_data(limit=10000)
    
    relevant = [
        d for d in decisions
        if d.selected_model == model and d.timestamp >= cutoff_time
    ]
    
    if not relevant:
        return {
            "model": model,
            "count": 0,
            "avg_quality": None,
            "min_quality": None,
            "max_quality": None,
        }
    
    feedbacks = [d.quality_feedback for d in relevant if d.quality_feedback is not None]
    
    return {
        "model": model,
        "count": len(relevant),
        "avg_quality": sum(feedbacks) / len(feedbacks) if feedbacks else None,
        "min_quality": min(feedbacks) if feedbacks else None,
        "max_quality": max(feedbacks) if feedbacks else None,
        "with_feedback": len(feedbacks),
    }


def display_tracking_summary(hours: int = 24) -> str:
    """Format tracking data for display.
    
    Args:
        hours: Look back this many hours
    
    Returns:
        Formatted string with usage statistics
    """
    output = f"\n📊 Model Usage Tracking (Last {hours}h)\n"
    output += "─" * 70 + "\n\n"
    
    # Model usage
    model_stats = get_model_usage_stats(hours)
    if model_stats:
        output += "**Top Models Selected**\n"
        for model, count in list(model_stats.items())[:10]:
            output += f"  {model:40} — {count:3d} times\n"
    
    # Task type distribution
    output += "\n**Task Type Distribution**\n"
    task_stats = get_task_type_distribution(hours)
    for task_key, count in list(task_stats.items())[:10]:
        output += f"  {task_key:30} — {count:3d} times\n"
    
    # Classification method
    output += "\n**Classification Methods Used**\n"
    method_stats = get_classification_method_stats(hours)
    for method, count in method_stats.items():
        pct = (count / sum(method_stats.values()) * 100) if method_stats else 0
        output += f"  {method:20} — {count:3d} ({pct:5.1f}%)\n"
    
    output += f"\n{'─' * 70}\n"
    return output


def export_tracking_csv(filepath: Path) -> int:
    """Export tracking data to CSV for external analysis.
    
    Args:
        filepath: Where to save the CSV
    
    Returns:
        Number of records exported
    """
    import csv
    
    decisions = load_tracking_data(limit=100000)
    
    if not decisions:
        return 0
    
    try:
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=decisions[0].__dataclass_fields__.keys())
            writer.writeheader()
            for decision in decisions:
                writer.writerow(asdict(decision))
        
        log.info(f"Exported {len(decisions)} tracking records to {filepath}")
        return len(decisions)
    except Exception as e:
        log.error(f"Failed to export tracking data: {e}")
        return 0
