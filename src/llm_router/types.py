"""Shared types for LLM Router.

Defines all enums, dataclasses, and lookup tables used across the router.
All dataclasses are ``frozen=True`` — they are immutable value objects.
This prevents accidental mutation and makes them safe to cache and share
across async tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ── Provider Indicators ───────────────────────────────────────────────────────
# Colored emoji icons used in CLI output and MCP tool responses to make it
# easy to visually identify which provider handled a request at a glance.
# Each provider is assigned a unique color/shape to avoid confusion.

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
    "ollama": "\U0001f999",      # llama emoji
    "huggingface": "\U0001f917", # hugging face emoji
    "lm_studio": "\U0001f4bb",   # laptop (local server)
    "vllm": "\u26a1",            # lightning bolt (fast local)
}


def colorize_model(model: str) -> str:
    """Prefix a model name with its provider's emoji indicator for CLI display.

    Args:
        model: Model identifier in ``provider/model`` format.

    Returns:
        Emoji-prefixed model string (e.g. ``"🟢 openai/gpt-4o"``).
    """
    provider = model.split("/")[0] if "/" in model else model
    icon = PROVIDER_ICONS.get(provider, "\u2b1c")  # default: white square
    return f"{icon} {model}"


def colorize_provider(provider: str) -> str:
    """Prefix a provider name with its emoji indicator for CLI display.

    Args:
        provider: Provider name (e.g. ``"openai"``).

    Returns:
        Emoji-prefixed provider string (e.g. ``"🟢 openai"``).
    """
    icon = PROVIDER_ICONS.get(provider, "\u2b1c")
    return f"{icon} {provider}"


class TaskType(str, Enum):
    """The type of task a user prompt represents.

    Text tasks (QUERY through CODE) are routed through LiteLLM.
    Media tasks (IMAGE, VIDEO, AUDIO) are routed through provider-specific APIs.
    """

    QUERY = "query"         # Factual lookups, simple questions
    RESEARCH = "research"   # Web-augmented research (Perplexity, etc.)
    GENERATE = "generate"   # Content generation, writing, brainstorming
    ANALYZE = "analyze"     # Deep analysis, comparison, evaluation
    CODE = "code"           # Code generation, refactoring, debugging
    IMAGE = "image"         # Image generation (DALL-E, Flux, Imagen, etc.)
    VIDEO = "video"         # Video generation (Runway, Kling, Veo, etc.)
    AUDIO = "audio"         # Audio/TTS generation (ElevenLabs, OpenAI TTS)


class RoutingProfile(str, Enum):
    """Routing quality/cost tier that determines which model chain to use.

    Each profile maps to a different row in the routing table, with models
    ordered from preferred to fallback.
    """

    BUDGET = "budget"       # Cheapest viable models
    BALANCED = "balanced"   # Quality/cost sweet spot (default)
    PREMIUM = "premium"     # Best available, cost secondary
    QUOTA_BALANCED = "quota_balanced"  # Balance usage across Claude/Gemini CLI/Codex


class Tier(str, Enum):
    """User subscription tier, controlling access to advanced features.

    The FREE tier provides basic routing. PRO and TEAM unlock additional
    features listed in ``PRO_FEATURES``.
    """

    FREE = "free"
    PRO = "pro"
    TEAM = "team"


# Features that require PRO or TEAM tier. When a FREE user tries to access
# a gated feature, the MCP tool returns an upgrade prompt instead of
# executing the action. This set is checked at the tool handler level.
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
    """Prompt complexity level as determined by the classifier.

    Maps directly to routing profiles via ``COMPLEXITY_TO_PROFILE`` in
    ``profiles.py``: simple->budget, moderate->balanced, complex->premium,
    deep_reasoning->premium with extended thinking enabled.
    """

    SIMPLE = "simple"               # Facts, math, lookups — fast/cheap models suffice
    MODERATE = "moderate"           # Multi-step reasoning, code gen, writing
    COMPLEX = "complex"             # Architecture, research synthesis, novel algorithms
    DEEP_REASONING = "deep_reasoning"  # Formal proofs, philosophical analysis, first-principles
                                        # derivation — routes to PREMIUM with extended thinking


COMPLEXITY_ICONS: dict[str, str] = {
    "simple": "\U0001f7e1",           # yellow circle
    "moderate": "\U0001f535",         # blue circle
    "complex": "\U0001f7e3",          # purple circle
    "deep_reasoning": "\U0001f9e0",   # brain (extended thinking)
}


@dataclass(frozen=True)
class ClassificationResult:
    """Result of a complexity classification from the classifier module.

    Attributes:
        complexity: The classified complexity tier (simple/moderate/complex).
        confidence: Classifier's self-reported confidence (0.0-1.0).
            Zero indicates a fallback result where no classifier succeeded.
        reasoning: Brief explanation from the classifier (e.g. "factual lookup").
        inferred_task_type: Task type the classifier detected, or None if it
            couldn't determine one (the caller may override this).
        classifier_model: Which model performed the classification (for debugging).
        classifier_cost_usd: Cost of the classification LLM call itself.
        classifier_latency_ms: Latency of the classification LLM call.
    """

    complexity: Complexity
    confidence: float
    reasoning: str
    inferred_task_type: TaskType | None
    classifier_model: str
    classifier_cost_usd: float
    classifier_latency_ms: float

    def header(self) -> str:
        """Format a one-line summary for CLI/MCP display.

        Returns:
            Human-readable string like ``"[S] simple (95%) | task: query | via gemini/... ($0.000001, 42ms)"``.
        """
        tag = {"simple": "[S]", "moderate": "[M]", "complex": "[C]", "deep_reasoning": "[D]"}.get(self.complexity.value, "[?]")
        task = self.inferred_task_type.value if self.inferred_task_type else "auto"
        return (
            f"{tag} {self.complexity.value} ({self.confidence:.0%}) "
            f"| task: {task} "
            f"| via {self.classifier_model} "
            f"(${self.classifier_cost_usd:.6f}, {self.classifier_latency_ms:.0f}ms)"
        )


class QualityMode(str, Enum):
    """User preference for model quality vs. cost trade-off.

    Used by ``model_selector.select_model`` to override the default
    complexity-based selection.
    """

    BEST = "best"           # Always use strongest model (opus)
    BALANCED = "balanced"   # Match model to task complexity (default)
    CONSERVE = "conserve"   # Favor cheapest viable model (haiku when possible)


# Claude Code models ordered by capability (index = rank).
# Used by model_selector to convert between model names and numeric indices
# for the downshift arithmetic (e.g. index 2 - shift 1 = index 1 = sonnet).
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]

# Relative quality scores (0.0-1.0) for each Claude model tier.
# Used by analytics and savings calculations to quantify the quality
# trade-off when downshifting models under budget pressure.
MODEL_QUALITY: dict[str, float] = {
    "haiku": 0.6,
    "sonnet": 0.85,
    "opus": 1.0,
}

# Blended cost per 1K tokens (average of input and output pricing).
# Based on Anthropic's API pricing. Even under a Claude Code subscription
# (where API calls are "free"), these values let the savings calculator
# estimate how much money the routing decisions would save at API rates.
MODEL_COST_PER_1K: dict[str, float] = {
    "haiku": 0.001,    # $1/M input, $5/M output -> ~$0.001/1K blended
    "sonnet": 0.009,   # $3/M input, $15/M output -> ~$0.009/1K blended
    "opus": 0.045,     # $15/M input, $75/M output -> ~$0.045/1K blended
}

# Approximate generation speed (tokens per second) for each model tier.
# Used by the savings calculator to estimate time saved by routing simple
# tasks to faster models instead of always using opus.
MODEL_SPEED_TPS: dict[str, float] = {
    "haiku": 200.0,    # fastest
    "sonnet": 120.0,   # mid
    "opus": 60.0,      # slowest, deepest reasoning
}

# Default model for each complexity tier before any budget adjustment.
# This is the starting point for model_selector's decision tree.
COMPLEXITY_BASE_MODEL: dict[str, str] = {
    "simple": "haiku",
    "moderate": "sonnet",
    "complex": "opus",
    "deep_reasoning": "opus",  # Extended thinking uses Opus or Sonnet 3.7+ with thinking budget
}


@dataclass(frozen=True)
class RoutingRecommendation:
    """Full routing recommendation produced by ``model_selector.select_model``.

    Attributes:
        classification: The upstream complexity classification.
        recommended_model: The selected Claude model tier (haiku/sonnet/opus).
        base_model: What complexity alone would have selected (before budget
            adjustments). Compared with ``recommended_model`` to detect downshifts.
        budget_pct_used: Fraction of daily token budget consumed (0.0-1.0+).
        was_downshifted: True if budget pressure caused a lower model than
            complexity alone would have chosen.
        quality_mode: The quality mode that was active during selection.
        min_model: The minimum model floor that was enforced.
        reasoning: Human-readable explanation of the selection decision.
        external_fallback: Optional external model to try if Claude is unavailable
            (e.g. ``"openai/gpt-4o"`` when Claude subscription limits are tight).
    """

    classification: ClassificationResult
    recommended_model: str
    base_model: str
    budget_pct_used: float
    was_downshifted: bool
    quality_mode: QualityMode
    min_model: str
    reasoning: str
    external_fallback: str | None = None

    def header(self) -> str:
        """Format a one-line summary for CLI/MCP display.

        Returns:
            Human-readable string showing complexity, model choice, budget
            pressure bar, and any downshift/fallback indicators.
        """
        tag = {"simple": "[S]", "moderate": "[M]", "complex": "[C]", "deep_reasoning": "[D]"}.get(
            self.classification.complexity.value, "[?]",
        )
        model_tag = {"haiku": "H", "sonnet": "S", "opus": "O"}.get(self.recommended_model, "?")
        bar = _budget_bar(self.budget_pct_used, 10)

        parts = [
            f"{tag} {self.classification.complexity.value} ({self.classification.confidence:.0%})",
            f"-> [{model_tag}] {self.recommended_model}",
            f"| pressure: {bar} {self.budget_pct_used:.0%}",
        ]
        if self.was_downshifted:
            parts.append(f"| !! downshifted from {self.base_model}")
        if self.external_fallback:
            parts.append(f"| >> fallback: {self.external_fallback}")
        return " ".join(parts)


def _budget_bar(pct: float, width: int = 20) -> str:
    """Render an ASCII progress bar for budget consumption.

    Uses ``=`` for consumed portion and ``.`` for remaining, producing
    output like ``[========..........]``. This fixed-width representation
    works in both terminal and MCP markdown contexts.

    Args:
        pct: Fraction consumed (0.0-1.0+). Values above 1.0 render as full.
        width: Total character width of the bar (excluding brackets).

    Returns:
        ASCII bar string, e.g. ``"[========............]"``.
    """
    filled = round(pct * width)
    return "[" + "=" * filled + "." * (width - filled) + "]"


class BudgetExceededError(RuntimeError):
    """Raised when the monthly spend has reached or exceeded the configured budget.

    Caught at the MCP tool handler level to return a user-friendly message
    instead of a stack trace.
    """


@dataclass(frozen=True)
class LLMResponse:
    """Unified response from any LLM or media generation call.

    Attributes:
        content: The generated text content (empty string for media-only responses).
        model: The model identifier that produced this response.
        input_tokens: Number of input/prompt tokens consumed.
        output_tokens: Number of output/completion tokens generated.
        cost_usd: Estimated cost of this single call in USD.
        latency_ms: Wall-clock time for the API call in milliseconds.
        provider: Provider name (e.g. ``"openai"``, ``"fal"``).
        citations: Source URLs returned by research models (e.g. Perplexity).
        media_url: URL of the generated asset for image/video/audio responses.
    """

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    provider: str
    citations: list[str] = field(default_factory=list)
    media_url: str | None = None

    def summary(self) -> str:
        """Format a compact one-line summary for logging and CLI display.

        Returns:
            String like ``"[🟢 openai/gpt-4o] 150+200 tokens $0.000350 120ms"``.
        """
        tokens = f"{self.input_tokens}+{self.output_tokens} tokens" if self.input_tokens else ""
        parts = [f"[{colorize_model(self.model)}]"]
        if tokens:
            parts.append(tokens)
        parts.append(f"${self.cost_usd:.6f}")
        parts.append(f"{self.latency_ms:.0f}ms")
        return " ".join(parts)

    def header(self) -> str:
        """Format a markdown header line for MCP tool responses.

        Returns:
            Markdown blockquote with model, tokens, cost, and latency.
        """
        tokens = f"{self.input_tokens + self.output_tokens} tokens" if self.input_tokens else ""
        parts = [f"> 🤖 **{colorize_model(self.model)}**"]
        if tokens:
            parts.append(tokens)
        parts.append(f"${self.cost_usd:.6f}")
        parts.append(f"{self.latency_ms:.0f}ms")
        return " · ".join(parts)


@dataclass(frozen=True)
class PipelineStep:
    """A single step in a multi-step orchestration pipeline.

    Attributes:
        task_type: What kind of task this step performs.
        prompt_template: Prompt text, which may contain ``{previous_result}``
            as a placeholder that gets replaced with the prior step's output.
        system_prompt: Optional system prompt for this step.
        model_override: Force a specific model for this step, bypassing
            profile-based routing.
    """

    task_type: TaskType
    prompt_template: str
    system_prompt: str | None = None
    model_override: str | None = None


# ── Adaptive Universal Router types (v5.0) ────────────────────────────────────


class ProviderTier(str, Enum):
    """Cost tier for a provider, used to enforce free-first ordering in chains.

    Ordered ascending by cost: LOCAL is always preferred, EXPENSIVE is last resort.
    Models within 5% quality score are ranked by tier (lower = preferred).
    """

    LOCAL = "local"              # Ollama, LM Studio, vLLM — zero monetary cost
    FREE_CLOUD = "free_cloud"    # HF Inference free tier, Groq free, Codex quota
    CHEAP_PAID = "cheap_paid"    # DeepSeek, Gemini Flash, GPT-4o-mini
    SUBSCRIPTION = "subscription"  # Claude Pro/Max, paid Codex tier
    EXPENSIVE = "expensive"      # o3, Claude Opus API, GPT-4o full


# Providers that are always locally hosted with zero monetary cost.
# These always receive budget_availability = 1.0 regardless of any quota state.
LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "lm_studio", "vllm", "llamacpp"})


@dataclass(frozen=True)
class ModelCapability:
    """Discovered capability record for a single model.

    Produced by the discovery layer (discover.py) and cached to
    ``~/.llm-router/discovery.json``.

    Attributes:
        model_id: Full model identifier (e.g. ``"ollama/qwen3:32b"``).
        provider: Provider name (e.g. ``"ollama"``, ``"openai"``).
        provider_tier: Cost tier for free-first chain ordering.
        task_types: Task types this model can serve.
        cost_per_1k: Blended cost per 1K tokens (0.0 for local/free models).
        latency_p50_ms: Measured P50 latency in ms (0.0 if not yet measured).
        context_window: Max context window in tokens.
        available: Whether this model is currently reachable.
    """

    model_id: str
    provider: str
    provider_tier: ProviderTier
    task_types: frozenset[TaskType]
    cost_per_1k: float = 0.0
    latency_p50_ms: float = 0.0
    context_window: int = 8192
    available: bool = True


@dataclass(frozen=True)
class BudgetState:
    """Real-time budget state for a single provider.

    Produced by the Budget Oracle (budget.py) and used by the scorer to
    compute ``budget_availability`` for each model in the chain.

    Attributes:
        provider: Provider name.
        pressure: Normalized 0.0 (fully available) to 1.0 (exhausted).
        spend_usd: Amount spent this billing period in USD.
        cap_usd: User-configured monthly cap in USD (0.0 = no cap).
        quota_pct: Quota consumption 0.0-1.0 for subscription providers.
        reset_at: ISO timestamp when quota/rate-limit resets (None if unknown).
    """

    provider: str
    pressure: float
    spend_usd: float = 0.0
    cap_usd: float = 0.0
    quota_pct: float = 0.0
    reset_at: str | None = None

    @property
    def availability(self) -> float:
        """Inverse of pressure — 1.0 = fully available, 0.0 = exhausted."""
        return max(0.0, 1.0 - self.pressure)


@dataclass(frozen=True)
class ComplexityWeights:
    """Scoring formula weights for a given complexity tier.

    All four weights must sum to 1.0. Used by scorer.py to compute
    the final model score from quality, latency, budget, and acceptance signals.
    """

    quality: float      # Benchmark score for this task type
    latency: float      # Speed: normalized inverse of measured P50 latency
    budget: float       # Budget availability: 1.0 = free/available, 0.0 = exhausted
    acceptance: float   # User acceptance rate from llm_rate feedback


# Scoring weight profiles per complexity tier.
# Simple tasks weight budget heavily — cheap models are preferred and quota is preserved.
# Complex tasks weight quality heavily — spend budget only when the task demands it.
COMPLEXITY_WEIGHTS: dict[str, ComplexityWeights] = {
    "simple": ComplexityWeights(quality=0.20, latency=0.25, budget=0.45, acceptance=0.10),
    "moderate": ComplexityWeights(quality=0.35, latency=0.20, budget=0.35, acceptance=0.10),
    "complex": ComplexityWeights(quality=0.55, latency=0.10, budget=0.20, acceptance=0.15),
    "deep_reasoning": ComplexityWeights(quality=0.60, latency=0.05, budget=0.20, acceptance=0.15),
}


@dataclass(frozen=True)
class ScoredModel:
    """A model with its computed composite score for a (task, complexity) pair.

    Produced by scorer.py and consumed by chain_builder.py to sort models
    into the optimal fallback chain.
    """

    model_id: str
    capability: ModelCapability
    score: float            # Final composite score (0.0–1.0)
    quality_score: float    # Benchmark score component
    budget_score: float     # Budget availability component
    latency_score: float    # Latency component
    acceptance_score: float  # User feedback component


@dataclass
class PipelineResult:
    """Aggregated result of a multi-step orchestration pipeline.

    Unlike other dataclasses in this module, this is mutable (not frozen)
    because the pipeline executor appends step results incrementally.

    Attributes:
        steps: List of individual step responses, in execution order.
        final_content: The content from the last step (the pipeline's output).
        total_cost_usd: Sum of all step costs.
        total_latency_ms: Sum of all step latencies (wall-clock, not parallel).
    """

    steps: list[LLMResponse] = field(default_factory=list)
    final_content: str = ""
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0

    def summary(self) -> str:
        """Format a multi-line summary of all pipeline steps.

        Returns:
            Overview line plus one indented line per step with its summary.
        """
        lines = [f"Pipeline: {len(self.steps)} steps, ${self.total_cost_usd:.4f}, {self.total_latency_ms:.0f}ms"]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  Step {i}: {step.summary()}")
        return "\n".join(lines)
