"""Shared types for LLM Router."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ── Provider Indicators ───────────────────────────────────────────────────────
# Each provider gets a distinct colored emoji for visual identification.

PROVIDER_ICONS: dict[str, str] = {
    "openai": "\U0001f7e2",       # green circle
    "anthropic": "\U0001f7e3",    # purple circle
    "gemini": "\U0001f535",       # blue circle
    "perplexity": "\U0001f7e0",   # orange circle
    "mistral": "\U0001f534",      # red circle
    "deepseek": "\U0001f929",     # star-struck (cyan star)
    "groq": "\U0001f7e1",        # yellow circle
    "together": "\U0001faa9",     # mirror (reflective)
    "xai": "\u26aa",             # white circle
    "cohere": "\U0001f7e4",      # brown circle
    "fal": "\U0001f4a0",         # diamond with dot
    "stability": "\U0001f30c",   # milky way
    "elevenlabs": "\U0001f3b5",  # musical note
    "runway": "\U0001f3ac",      # clapper board
    "replicate": "\U0001f504",   # counterclockwise arrows
}


def colorize_model(model: str) -> str:
    """Prefix a model name with its provider's emoji indicator."""
    provider = model.split("/")[0] if "/" in model else model
    icon = PROVIDER_ICONS.get(provider, "\u2b1c")  # default: white square
    return f"{icon} {model}"


def colorize_provider(provider: str) -> str:
    """Prefix a provider name with its emoji indicator."""
    icon = PROVIDER_ICONS.get(provider, "\u2b1c")
    return f"{icon} {provider}"


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


class Complexity(str, Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


COMPLEXITY_ICONS: dict[str, str] = {
    "simple": "\U0001f7e1",    # yellow circle
    "moderate": "\U0001f535",  # blue circle
    "complex": "\U0001f7e3",   # purple circle
}


@dataclass(frozen=True)
class ClassificationResult:
    """Result of a complexity classification."""
    complexity: Complexity
    confidence: float
    reasoning: str
    inferred_task_type: TaskType | None
    classifier_model: str
    classifier_cost_usd: float
    classifier_latency_ms: float

    def header(self) -> str:
        icon = COMPLEXITY_ICONS.get(self.complexity.value, "\u2b1c")
        task = self.inferred_task_type.value if self.inferred_task_type else "auto"
        return (
            f"{icon} **{self.complexity.value}** ({self.confidence:.0%}) "
            f"| task: {task} "
            f"| classifier: {colorize_model(self.classifier_model)} "
            f"(${self.classifier_cost_usd:.6f}, {self.classifier_latency_ms:.0f}ms)"
        )


class QualityMode(str, Enum):
    BEST = "best"           # Always use strongest model
    BALANCED = "balanced"   # Match model to task complexity (default)
    CONSERVE = "conserve"   # Favor cheapest viable model


# Claude Code models ordered by capability (index = rank)
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]

# Model quality scores (0-1) for routing decisions
MODEL_QUALITY: dict[str, float] = {
    "haiku": 0.6,
    "sonnet": 0.85,
    "opus": 1.0,
}

# Baseline model per complexity (before budget pressure)
COMPLEXITY_BASE_MODEL: dict[str, str] = {
    "simple": "haiku",
    "moderate": "sonnet",
    "complex": "opus",
}


@dataclass(frozen=True)
class RoutingRecommendation:
    """Full routing recommendation with budget awareness."""
    classification: ClassificationResult
    recommended_model: str       # haiku / sonnet / opus
    base_model: str              # what complexity alone would pick
    budget_pct_used: float       # 0.0 - 1.0
    was_downshifted: bool        # True if budget pressure lowered the model
    quality_mode: QualityMode
    min_model: str               # floor — never go below this
    reasoning: str

    def header(self) -> str:
        icon = COMPLEXITY_ICONS.get(self.classification.complexity.value, "\u2b1c")
        model_icons = {"haiku": "\U0001f7e1", "sonnet": "\U0001f535", "opus": "\U0001f7e3"}
        model_icon = model_icons.get(self.recommended_model, "\u2b1c")
        budget_bar = _budget_bar(self.budget_pct_used)

        parts = [
            f"{icon} **{self.classification.complexity.value}** ({self.classification.confidence:.0%})",
            f"→ {model_icon} **{self.recommended_model}**",
            f"| budget: {budget_bar} {self.budget_pct_used:.0%}",
        ]
        if self.was_downshifted:
            parts.append(f"| \u26a0\ufe0f downshifted from {self.base_model}")
        return " ".join(parts)


def _budget_bar(pct: float) -> str:
    """Render a 5-char budget bar: [███░░]"""
    filled = round(pct * 5)
    return "[" + "\u2588" * filled + "\u2591" * (5 - filled) + "]"


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
        parts = [f"[{colorize_model(self.model)}]"]
        if tokens:
            parts.append(tokens)
        parts.append(f"${self.cost_usd:.6f}")
        parts.append(f"{self.latency_ms:.0f}ms")
        return " ".join(parts)

    def header(self) -> str:
        return f"> **Routed to {colorize_model(self.model)}** | ${self.cost_usd:.6f} | {self.latency_ms:.0f}ms"


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
