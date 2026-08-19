"""Type-safe streaming event contract for LLM Router v0.3.2.

Defines all router-level streaming events with typed payloads.
These events flow from route_and_stream() through the routing pipeline.

Safety invariants:
  - attempt.committed marks the commit barrier (no fallback after)
  - output.delta preserves message order (never time-throttled)
  - visited_models tracking prevents duplicate attempts
  - All usage/audit settlement happens exactly once
"""

from typing import Literal, TypedDict, Union


EventType = Literal[
    "route.started",
    "route.cached_hit",
    "attempt.started",
    "attempt.buffering",
    "attempt.committed",
    "output.delta",
    "quality.verdict",
    "attempt.failed",
    "fallback.scheduled",
    "usage.final",
    "route.completed",
    "route.aborted",
]


class BaseEvent(TypedDict):
    """Base fields on all events."""
    seq: int
    type: EventType
    correlation_id: str
    ts_monotonic_ms: float


class RouteStarted(TypedDict):
    """Route initiated with chain."""
    task_type: str
    profile: str
    complexity: str
    candidate_count: int
    chain_preview: list[str]
    buffered_mode: bool


class RouteCachedHit(TypedDict):
    """Route satisfied from semantic cache."""
    model: str
    provider: str
    cached_at: float


class AttemptStarted(TypedDict):
    """Attempting a model from the chain."""
    attempt_index: int
    model: str
    provider: str
    emergency_fallback: bool


class AttemptBuffering(TypedDict):
    """Buffering tokens before commit (gates/judge active)."""
    attempt_index: int
    model: str
    buffered_chars: int
    buffered_approx_tokens: int


class AttemptCommitted(TypedDict):
    """First visible output; fallback now disabled."""
    attempt_index: int
    model: str
    visible_output_started: bool


class OutputDelta(TypedDict):
    """Chunk of response text."""
    attempt_index: int
    model: str
    text: str
    chars: int
    approx_tokens: int


class QualityVerdict(TypedDict):
    """Streaming judge verdict (only before commit)."""
    attempt_index: int
    model: str
    score: float | None
    threshold: float
    streak: int
    intercept: bool
    reason: str


class AttemptFailed(TypedDict):
    """Model attempt failed; will fallback or abort."""
    attempt_index: int
    model: str
    provider: str
    reason_kind: Literal[
        "provider_error",
        "rate_limit",
        "auth_error",
        "content_filter",
        "gate_failed",
        "budget_exhausted",
        "timeout",
        "cancelled",
    ]
    detail: str
    retry_after_s: int | None
    will_fallback: bool


class FallbackScheduled(TypedDict):
    """Falling back to next model in chain."""
    from_attempt: int
    to_attempt: int
    from_model: str
    to_model: str
    reason_kind: str
    emergency_fallback: bool


class UsageFinal(TypedDict):
    """Final token usage and cost."""
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float


class RouteCompleted(TypedDict):
    """Route succeeded with final model."""
    final_model: str
    final_provider: str
    chain_attempts: list[str]
    used_emergency_fallback: bool
    cached: bool


class RouteAborted(TypedDict):
    """Route failed; no model succeeded."""
    outcome: Literal[
        "budget_exceeded",
        "quota_denied",
        "deadline_exceeded",
        "wall_clock_exceeded",
        "all_models_failed",
        "cancelled",
    ]
    detail: str


# Union of all payload types (matches EventType literals)
RouterPayloadEvent = Union[
    RouteStarted,
    RouteCachedHit,
    AttemptStarted,
    AttemptBuffering,
    AttemptCommitted,
    OutputDelta,
    QualityVerdict,
    AttemptFailed,
    FallbackScheduled,
    UsageFinal,
    RouteCompleted,
    RouteAborted,
]

# Full event: base fields + payload
RouterStreamEvent = Union[BaseEvent, tuple[BaseEvent, RouterPayloadEvent]]


# Validation helpers
_EVENT_PAYLOAD_TYPES: dict[EventType, type] = {
    "route.started": RouteStarted,
    "route.cached_hit": RouteCachedHit,
    "attempt.started": AttemptStarted,
    "attempt.buffering": AttemptBuffering,
    "attempt.committed": AttemptCommitted,
    "output.delta": OutputDelta,
    "quality.verdict": QualityVerdict,
    "attempt.failed": AttemptFailed,
    "fallback.scheduled": FallbackScheduled,
    "usage.final": UsageFinal,
    "route.completed": RouteCompleted,
    "route.aborted": RouteAborted,
}


def is_valid_event_type(t: str) -> bool:
    """Check if string is a valid EventType."""
    return t in _EVENT_PAYLOAD_TYPES


def payload_type_for(event_type: EventType) -> type:
    """Get the TypedDict class for an event type."""
    return _EVENT_PAYLOAD_TYPES[event_type]


def required_keys_for(event_type: EventType) -> set[str]:
    """Get required keys for event type payload."""
    payload_cls = payload_type_for(event_type)
    # TypedDict annotations stored in __annotations__
    return set(payload_cls.__annotations__.keys())


__all__ = [
    "EventType",
    "BaseEvent",
    "RouteStarted",
    "RouteCachedHit",
    "AttemptStarted",
    "AttemptBuffering",
    "AttemptCommitted",
    "OutputDelta",
    "QualityVerdict",
    "AttemptFailed",
    "FallbackScheduled",
    "UsageFinal",
    "RouteCompleted",
    "RouteAborted",
    "RouterPayloadEvent",
    "RouterStreamEvent",
    "is_valid_event_type",
    "payload_type_for",
    "required_keys_for",
]
