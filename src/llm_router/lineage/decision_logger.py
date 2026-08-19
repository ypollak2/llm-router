"""Capture routing decisions with full lineage trace.

Each decision logs:
- What operation was performed (get_cap, classify_audit, etc.)
- How the task was classified (simple/moderate/complex)
- Which model was selected (Haiku, Gemini Flash, Sonnet, Opus, etc.)
- Why that model was picked (router output, fallback reason, etc.)
- Token counts (input, output, total)
- Latency and cost
- Fallback chain if applicable
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from llm_router.lineage.lineage_store import LineageStore


@dataclass(frozen=True)
class RoutingDecision:
    """Immutable record of a single routing decision."""

    # Required fields (no defaults)
    decision_id: str
    operation: str  # e.g., "get_cap", "classify_audit", "validate_config"
    classification: str  # e.g., "query/simple", "analyze/moderate", "code/complex"
    selected_model: str  # e.g., "gemini-2.5-flash", "claude-sonnet-4-6", "ollama-llama2"
    selection_reason: str  # e.g., "router_picked", "fallback_after_ollama", "cost_optimal"

    # Optional fields (with defaults)
    timestamp: float = field(default_factory=time.time)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    routing_overhead_ms: float = 0.0
    fallback_chain: list[str] = field(default_factory=list)  # ["ollama", "codex", "gemini-flash"]
    fallback_reason: str | None = None  # e.g., "ollama_timeout"
    request_id: str = ""  # Link to parent request if part of larger flow
    parent_decision_id: str = ""  # Link to parent decision for nested operations
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)


_store: LineageStore | None = None


def get_lineage_store() -> LineageStore:
    """Get or create the global LineageStore singleton."""
    global _store
    if _store is None:
        _store = LineageStore()
    return _store


def log_routing_decision(
    operation: str,
    classification: str,
    selected_model: str,
    selection_reason: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    routing_overhead_ms: float = 0.0,
    fallback_chain: list[str] | None = None,
    fallback_reason: str | None = None,
    request_id: str = "",
    parent_decision_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> RoutingDecision:
    """Log a routing decision to lineage store.

    Args:
        operation: What was performed (e.g., "get_cap", "classify_audit")
        classification: Task classification (e.g., "query/simple", "analyze/moderate")
        selected_model: Which model handled it (e.g., "gemini-2.5-flash")
        selection_reason: Why that model was picked (e.g., "router_picked", "fallback_after_ollama")
        input_tokens: Input token count
        output_tokens: Output token count
        cost_usd: Cost in USD
        latency_ms: Total latency in milliseconds
        routing_overhead_ms: Time spent on routing decision itself
        fallback_chain: Models tried before success (e.g., ["ollama", "codex"])
        fallback_reason: Why fallback occurred (e.g., "ollama_timeout")
        request_id: Link to parent request
        parent_decision_id: Link to parent decision (for nested operations)
        metadata: Additional context

    Returns:
        RoutingDecision record that was logged
    """
    import uuid

    decision = RoutingDecision(
        decision_id=str(uuid.uuid4()),
        operation=operation,
        classification=classification,
        selected_model=selected_model,
        selection_reason=selection_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        routing_overhead_ms=routing_overhead_ms,
        fallback_chain=fallback_chain or [],
        fallback_reason=fallback_reason,
        request_id=request_id,
        parent_decision_id=parent_decision_id,
        metadata=metadata or {},
    )

    store = get_lineage_store()
    store.append(decision)

    return decision
