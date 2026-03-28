"""Shared types for LLM Router."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskType(str, Enum):
    QUERY = "query"
    RESEARCH = "research"
    GENERATE = "generate"
    ANALYZE = "analyze"
    CODE = "code"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class RoutingProfile(str, Enum):
    BUDGET = "budget"
    BALANCED = "balanced"
    PREMIUM = "premium"


class Tier(str, Enum):
    FREE = "free"
    PRO = "pro"
    TEAM = "team"


# Features gated by tier
PRO_FEATURES = {
    "multi_step",       # Multi-step orchestration
    "budget_optimizer",  # Monthly budget with auto-downshift
    "benchmarks",       # Weekly quality benchmark routing
    "synergy",          # Synergy detection & recommendations
    "analytics",        # Full usage breakdown & projections
    "custom_profiles",  # User-defined routing profiles
    "savings_report",   # Monthly savings report
}


class BudgetExceededError(RuntimeError):
    """Raised when the monthly budget limit has been reached."""


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    provider: str
    citations: list[str] = field(default_factory=list)
    media_url: str | None = None  # For image/video/audio responses

    def summary(self) -> str:
        tokens = f"{self.input_tokens}+{self.output_tokens} tokens" if self.input_tokens else ""
        parts = [f"[{self.model}]"]
        if tokens:
            parts.append(tokens)
        parts.append(f"${self.cost_usd:.6f}")
        parts.append(f"{self.latency_ms:.0f}ms")
        return " ".join(parts)


@dataclass(frozen=True)
class PipelineStep:
    """A single step in a multi-step orchestration pipeline."""
    task_type: TaskType
    prompt_template: str  # Can reference {previous_result}
    system_prompt: str | None = None
    model_override: str | None = None


@dataclass
class PipelineResult:
    """Result of a multi-step orchestration pipeline."""
    steps: list[LLMResponse] = field(default_factory=list)
    final_content: str = ""
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0

    def summary(self) -> str:
        lines = [f"Pipeline: {len(self.steps)} steps, ${self.total_cost_usd:.4f}, {self.total_latency_ms:.0f}ms"]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  Step {i}: {step.summary()}")
        return "\n".join(lines)
