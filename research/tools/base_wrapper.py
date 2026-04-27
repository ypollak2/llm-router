"""
Abstract base wrapper for token-saving tools.

Defines the standard interface that all tool wrappers must implement for fair benchmarking.
"""

import asyncio
import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
import json


@dataclass
class ToolInput:
    """Standardized input for all tool wrappers."""
    prompt: str
    model: str = "gpt-3.5-turbo"  # Default model (tool can override)
    max_tokens: Optional[int] = None
    temperature: float = 0.7
    metadata: Dict[str, Any] = field(default_factory=dict)  # Tool-specific config


@dataclass
class ToolOutput:
    """Standardized output metrics from all tool wrappers."""
    # Result
    response: str

    # Token metrics
    input_tokens: int  # Original prompt tokens
    output_tokens: int  # Response tokens
    compressed_input_tokens: Optional[int] = None  # After compression (if applicable)
    compression_ratio: Optional[float] = None  # Original / Compressed

    # Timing
    latency_ms: float = 0.0  # Total execution time
    preprocessing_ms: float = 0.0  # Time spent on optimization
    inference_ms: float = 0.0  # Time for LLM call

    # Quality
    quality_score: Optional[float] = None  # 0-1 from judge or heuristic

    # Metadata
    tool_name: str = ""
    technique_variant: str = ""  # e.g., "full_caveman", "aggressive_routing"
    timestamp: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "response": self.response,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "compressed_input_tokens": self.compressed_input_tokens,
            "compression_ratio": self.compression_ratio,
            "latency_ms": self.latency_ms,
            "preprocessing_ms": self.preprocessing_ms,
            "inference_ms": self.inference_ms,
            "quality_score": self.quality_score,
            "tool_name": self.tool_name,
            "technique_variant": self.technique_variant,
            "timestamp": self.timestamp.isoformat(),
            "error": self.error,
        }


class BaseToolWrapper(ABC):
    """
    Abstract base class for all token-saving tool wrappers.

    Each tool (RouteLLM, LiteLLM, etc.) must subclass this and implement:
    - async initialize() — set up tool, validate config, load models
    - async execute() — run the tool on input, collect metrics
    - async cleanup() — tear down resources

    The benchmark harness will:
    1. Call initialize() once at startup
    2. Call execute() for each task (with metrics collection)
    3. Call cleanup() once at shutdown
    """

    def __init__(self, tool_name: str, config: Dict[str, Any]):
        """
        Initialize wrapper.

        Args:
            tool_name: Name of the tool (e.g., "routellm", "litellm")
            config: Tool-specific configuration dict
        """
        self.tool_name = tool_name
        self.config = config
        self.initialized = False
        self._startup_time = None
        self._call_count = 0

    async def initialize(self) -> None:
        """
        Set up the tool. Called once before any execute() calls.

        Responsibilities:
        - Load models if needed
        - Validate API keys/configs
        - Initialize external services
        - Test connectivity/permissions

        Should raise Exception if initialization fails.
        """
        self._startup_time = time.time()
        self.initialized = True

    @abstractmethod
    async def execute(
        self,
        task_input: ToolInput,
        technique_variant: str = "default",
    ) -> ToolOutput:
        """
        Execute the tool on the given input.

        Args:
            task_input: Standardized input (prompt, model, etc.)
            technique_variant: Variant of the technique to apply
                Examples: "aggressive", "balanced", "conservative"
                          "full_caveman", "lite_caveman"
                          Tool-specific variants

        Returns:
            ToolOutput: Standardized output with all metrics

        Must set in ToolOutput:
        - response: The generated text
        - input_tokens: Token count of original prompt
        - output_tokens: Token count of response
        - compressed_input_tokens: If compression is applied
        - compression_ratio: Original / compressed (if applicable)
        - latency_ms: Total time
        - preprocessing_ms: Time spent on optimization
        - inference_ms: Time for LLM call
        - tool_name, technique_variant: For tracking
        - error: If execution failed
        """
        raise NotImplementedError

    async def cleanup(self) -> None:
        """
        Clean up resources. Called once at shutdown.

        Responsibilities:
        - Close connections
        - Release memory-intensive resources
        - Flush pending operations
        """
        self.initialized = False

    def _get_startup_time_sec(self) -> float:
        """Get seconds since initialize() was called."""
        if self._startup_time is None:
            return 0.0
        return time.time() - self._startup_time

    def _increment_call_count(self) -> int:
        """Increment and return execution count."""
        self._call_count += 1
        return self._call_count

    # Helper methods for common operations

    def _estimate_tokens(self, text: str) -> int:
        """
        Rough token count estimate (1 token ≈ 4 chars).
        Tools should override with actual tokenization if needed.
        """
        return len(text) // 4

    async def _with_timeout(
        self,
        coro,
        timeout_sec: float,
        error_msg: str = "Execution timeout",
    ):
        """Run coroutine with timeout."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout_sec)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{error_msg} ({timeout_sec}s exceeded)")

    def _record_timing(
        self,
        start_time: float,
    ) -> Dict[str, float]:
        """Calculate elapsed time in milliseconds."""
        elapsed_sec = time.time() - start_time
        return {"elapsed_ms": elapsed_sec * 1000}


class ToolWrapperRegistry:
    """Registry for all available tool wrappers."""

    _registry: Dict[str, type] = {}

    @classmethod
    def register(cls, tool_name: str, wrapper_class: type):
        """Register a tool wrapper class."""
        cls._registry[tool_name] = wrapper_class

    @classmethod
    def get(cls, tool_name: str) -> type:
        """Get a tool wrapper class by name."""
        if tool_name not in cls._registry:
            raise ValueError(f"Unknown tool: {tool_name}. Available: {list(cls._registry.keys())}")
        return cls._registry[tool_name]

    @classmethod
    def list_tools(cls) -> List[str]:
        """List all registered tool names."""
        return sorted(cls._registry.keys())

    @classmethod
    def instantiate(cls, tool_name: str, config: Dict[str, Any]) -> BaseToolWrapper:
        """Create an instance of a tool wrapper."""
        wrapper_class = cls.get(tool_name)
        return wrapper_class(tool_name, config)


# Decorator for easy registration
def register_tool(tool_name: str):
    """Decorator to register a tool wrapper."""
    def decorator(wrapper_class: type):
        ToolWrapperRegistry.register(tool_name, wrapper_class)
        return wrapper_class
    return decorator


# ============================================================================
# SIMULATION MODE — For benchmarking without external dependencies
# ============================================================================

class SimulationMode:
    """Realistic simulation profiles for tools when actual dependencies unavailable."""

    # Tool-specific metric profiles (compression_ratio, latency_ms, quality_score)
    PROFILES = {
        "llm-router": {
            "variants": ["aggressive", "balanced", "conservative", "caveman_off", "caveman_full"],
            "compression_ratio": (0.45, 0.65),  # 35-55% reduction
            "latency_ms": (80, 150),
            "quality_baseline": 0.85,
        },
        "llmlingua": {
            "variants": ["llmlingua_20x", "llmlingua2_6x", "longllmlingua_rag"],
            "compression_ratio": (0.05, 0.15),  # 85-95% reduction
            "latency_ms": (120, 200),
            "quality_baseline": 0.78,
        },
        "routellm": {
            "variants": ["threshold_0.5", "threshold_0.7", "threshold_0.9"],
            "compression_ratio": (0.60, 0.85),  # 15-40% reduction (routing, not compression)
            "latency_ms": (40, 80),
            "quality_baseline": 0.88,
        },
        "litellm": {
            "variants": ["cost_optimized", "latency_optimized", "quality_optimized"],
            "compression_ratio": (0.70, 0.95),  # 5-30% reduction
            "latency_ms": (30, 70),
            "quality_baseline": 0.82,
        },
        "gptcache": {
            "variants": ["strict_similarity", "loose_similarity", "rag_optimized"],
            "compression_ratio": (0.80, 0.99),  # Cache hit, minimal compression
            "latency_ms": (10, 50),
            "quality_baseline": 0.90,
        },
        "claw": {
            "variants": ["code_only", "json_only", "text_only", "balanced", "aggressive"],
            "compression_ratio": (0.30, 0.60),  # 40-70% reduction
            "latency_ms": (100, 180),
            "quality_baseline": 0.81,
        },
        "dspy": {
            "variants": ["bootstrap_few_shot", "miprov2", "auto_generated", "minimal"],
            "compression_ratio": (0.55, 0.75),  # 25-45% reduction
            "latency_ms": (150, 250),
            "quality_baseline": 0.84,
        },
        "vllm_semantic_router": {
            "variants": ["task_specific", "speed_optimized", "quality_optimized"],
            "compression_ratio": (0.65, 0.85),  # 15-35% reduction
            "latency_ms": (50, 120),
            "quality_baseline": 0.86,
        },
        "headroom": {
            "variants": ["priority_aware", "summarize_on_overflow", "adaptive", "aggressive_truncate"],
            "compression_ratio": (0.40, 0.70),  # 30-60% reduction
            "latency_ms": (80, 140),
            "quality_baseline": 0.79,
        },
        "tensorzero": {
            "variants": ["ab_tested", "feedback_optimized", "multi_armed_bandit", "experimental"],
            "compression_ratio": (0.50, 0.80),  # 20-50% reduction
            "latency_ms": (120, 200),
            "quality_baseline": 0.83,
        },
    }

    @classmethod
    def generate_output(
        cls,
        tool_name: str,
        variant: str,
        task_input: ToolInput,
    ) -> ToolOutput:
        """Generate realistic simulated output for a tool/variant."""
        profile = cls.PROFILES.get(tool_name, cls.PROFILES["llm-router"])

        # Estimate input tokens
        input_tokens = max(50, len(task_input.prompt) // 4)

        # Generate compression ratio
        comp_min, comp_max = profile["compression_ratio"]
        compression_ratio = random.uniform(comp_min, comp_max)
        compressed_input_tokens = int(input_tokens * compression_ratio)

        # Generate latency
        latency_min, latency_max = profile["latency_ms"]
        latency_ms = random.uniform(latency_min, latency_max)
        preprocessing_ms = latency_ms * random.uniform(0.3, 0.7)
        inference_ms = latency_ms - preprocessing_ms

        # Quality score (variant-dependent)
        quality_baseline = profile["quality_baseline"]
        # Variant penalty/bonus
        variant_quality_variance = random.uniform(-0.05, 0.08)
        quality_score = max(0.5, min(0.99, quality_baseline + variant_quality_variance))

        # Simulate output
        output_tokens = max(20, len(task_input.prompt) // 8)

        return ToolOutput(
            response=f"[Simulated output from {tool_name}/{variant}]",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            compressed_input_tokens=compressed_input_tokens,
            compression_ratio=compression_ratio,
            latency_ms=latency_ms,
            preprocessing_ms=preprocessing_ms,
            inference_ms=inference_ms,
            quality_score=quality_score,
            tool_name=tool_name,
            technique_variant=variant,
            error=None,
        )
