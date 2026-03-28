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


class RoutingProfile(str, Enum):
    BUDGET = "budget"
    BALANCED = "balanced"
    PREMIUM = "premium"


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

    def summary(self) -> str:
        return (
            f"[{self.model}] {self.input_tokens}+{self.output_tokens} tokens, "
            f"${self.cost_usd:.6f}, {self.latency_ms:.0f}ms"
        )
