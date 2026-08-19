"""FrameworkAdapter protocol — contract every framework integration follows.

An adapter is a thin shim that maps a framework's "call the LLM" surface
to LLM Router's routing call. It's NOT an agent runtime. The framework owns
the loop; LLM Router owns the model pick + cost tracking + lineage.

Adapters are intentionally minimal (3 methods). The complexity of each
framework (Agno, Hermes, LangGraph, ...) is hidden behind the adapter so
the LLM Router core stays framework-agnostic.
"""
from __future__ import annotations

from typing import Any, Protocol


class FrameworkAdapter(Protocol):
    """Every concrete adapter implements these three.

    Attributes:
        name: short framework identifier (matches lineage `framework`
              column, e.g. "agno", "hermes", "langgraph"). Use lowercase
              hyphenated form.

    Methods:
        wrap_model: takes the framework's model object/config and returns
            a drop-in replacement that routes through LLM Router.
        detect_agent_id: best-effort extraction of the calling agent's
            identifier from the framework's runtime context. Returns None
            when the framework doesn't expose the info or no agent is
            active.
        is_available: cheap check that the framework's Python package is
            importable. Used by host installers to suggest "you have Agno,
            install the adapter".
    """

    name: str

    def wrap_model(self, framework_model: Any, agent_id: str | None = None) -> Any:
        ...

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        ...

    @classmethod
    def is_available(cls) -> bool:
        ...
