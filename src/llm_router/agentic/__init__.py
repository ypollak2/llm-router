"""LLM Router Agentic Router — milestone-gated escalating delegation.

See docs/agentic-router.md for the full design. The P1 core here is the
deterministic Milestone-Gated Escalating Execution (MGEE) engine, provable with
fake agents (no real models) so the flow — carry-forward, monotonic escalation,
bounded attempts, never-stuck termination — is verified before real backends.
"""
from llm_router.agentic.engine import (
    Event,
    MGEEEngine,
    Outcome,
    TaskResult,
    validate_event_stream,
)
from llm_router.agentic.ledger import (
    AcceptanceResult,
    Milestone,
    MilestoneStatus,
    TaskLedger,
)
from llm_router.agentic.savings import Savings, compute_savings

__all__ = [
    "AcceptanceResult",
    "Event",
    "MGEEEngine",
    "Milestone",
    "MilestoneStatus",
    "Outcome",
    "Savings",
    "TaskLedger",
    "TaskResult",
    "compute_savings",
    "validate_event_stream",
]
