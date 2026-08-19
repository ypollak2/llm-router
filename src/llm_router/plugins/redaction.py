"""Redaction plugin interface and registry.

Core defines a Redactor Protocol; enterprise registers a concrete implementation.
Routers call get_redactor() and fail open if no redactor is registered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class RedactionResult:
    """Output of redact_prompt — the scrubbed text + a summary of what was found."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)
    any_redactions: bool = False


class Redactor(Protocol):
    """Plugin interface for prompt redaction."""

    def redact_prompt(self, prompt: str) -> RedactionResult:
        """Redact sensitive patterns from prompt, return scrubbed text + counts."""
        ...


# Runtime registry for redactor implementations
_REDACTORS: dict[str, Redactor] = {}
_DEFAULT = "default"


def register_redactor(redactor: Redactor, name: str = _DEFAULT) -> None:
    """Register a redactor implementation."""
    _REDACTORS[name] = redactor


def get_redactor(name: str = _DEFAULT) -> Redactor | None:
    """Get the registered redactor, or None if not registered."""
    return _REDACTORS.get(name)


__all__ = [
    "RedactionResult",
    "Redactor",
    "register_redactor",
    "get_redactor",
]
