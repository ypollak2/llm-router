"""Implicit routing contracts — enforced automatically within route_and_call.

A contract captures the expectations for a delegated model call: what task type
it's handling, what verification gates must pass, and what constraints apply.
Contracts are never exposed to the user — they're generated and evaluated
entirely within the routing pipeline.

v8.8.0: Contract-as-Infrastructure — automatic quality gates on every routed call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from llm_router.types import Complexity, TaskType


class GateType(str, Enum):
    """Verification gates that can be applied to a model's output."""

    SYNTAX = "syntax"          # Code output must be parseable (no SyntaxError)
    LENGTH = "length"          # Output must exceed minimum length threshold
    STRUCTURE = "structure"    # Must contain expected sections/format
    FORMAT = "format"          # Must match expected format (JSON, markdown, etc.)
    CITATION = "citation"      # Must include sources/references


@dataclass(frozen=True)
class ContractConstraints:
    """Constraints on what the delegated model may do."""

    max_output_tokens: int = 8192
    min_output_length: int = 20       # Minimum characters in response
    required_format: str | None = None  # "json", "markdown", "code", None


@dataclass(frozen=True)
class RoutingContract:
    """An implicit contract generated for every delegated model call.

    Not a user-facing construct — generated inside route_and_call and used
    for automatic verification before accepting a response.
    """

    contract_id: str
    task_type: TaskType
    complexity: Complexity
    model: str
    gates: list[GateType]
    constraints: ContractConstraints = field(default_factory=ContractConstraints)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for receipt storage."""
        return {
            "contract_id": self.contract_id,
            "task_type": self.task_type.value,
            "complexity": self.complexity.value,
            "model": self.model,
            "gates": [g.value for g in self.gates],
            "constraints": {
                "max_output_tokens": self.constraints.max_output_tokens,
                "min_output_length": self.constraints.min_output_length,
                "required_format": self.constraints.required_format,
            },
            "created_at": self.created_at,
        }


# Gate assignment rules: which gates apply to which task types.
# These are intentionally lightweight — we verify structure, not correctness.
_TASK_GATES: dict[TaskType, list[GateType]] = {
    TaskType.CODE: [GateType.SYNTAX, GateType.LENGTH],
    TaskType.GENERATE: [GateType.LENGTH],
    TaskType.ANALYZE: [GateType.LENGTH, GateType.STRUCTURE],
    TaskType.RESEARCH: [GateType.LENGTH, GateType.CITATION],
    TaskType.QUERY: [GateType.LENGTH],
    # Media tasks have no text gates
    TaskType.IMAGE: [],
    TaskType.VIDEO: [],
    TaskType.AUDIO: [],
}

# Minimum output length per complexity — simple tasks get shorter thresholds.
# SIMPLE threshold is intentionally low (1 char) because simple queries may
# legitimately return very short answers (e.g. "yes", "42", "True").
_MIN_LENGTH: dict[Complexity, int] = {
    Complexity.SIMPLE: 1,
    Complexity.MODERATE: 20,
    Complexity.COMPLEX: 50,
    Complexity.DEEP_REASONING: 80,
}


def build_contract(
    contract_id: str,
    task_type: TaskType,
    complexity: Complexity,
    model: str,
) -> RoutingContract:
    """Build an implicit contract for a model dispatch.

    Automatically selects gates and constraints based on task type and complexity.
    """
    gates = _TASK_GATES.get(task_type, [])
    min_len = _MIN_LENGTH.get(complexity, 20)

    constraints = ContractConstraints(
        min_output_length=min_len,
        required_format="code" if task_type == TaskType.CODE else None,
    )

    return RoutingContract(
        contract_id=contract_id,
        task_type=task_type,
        complexity=complexity,
        model=model,
        gates=gates,
        constraints=constraints,
    )
