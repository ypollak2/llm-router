"""Agno framework adapter — concrete, re-exports from llm_router.integrations.agno.

The concrete implementation lives in llm_router.integrations.agno (ported
from llm-router; provides RouteredModel + RouteredTeam classes that Agno
treats as a drop-in agno.models.base.Model).

This module exposes the same classes under the unified frameworks
namespace + adds the FrameworkAdapter shim for future consistency with
other framework integrations.

Install:
    pip install "llm-routing[agno]"

Usage:
    from llm_router.frameworks.agno import RouteredModel, RouteredTeam
    from agno.agent import Agent

    agent = Agent(
        model=RouteredModel(task_type="code"),
        instructions="You are a coding assistant.",
    )
    agent.print_response("Write a Python quicksort.")
"""
from __future__ import annotations

from typing import Any


# Re-export the concrete classes only when Agno is installed. Otherwise
# expose None so callers can `from llm_router.frameworks.agno import RouteredModel`
# without crashing.
try:
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    AGNO_AVAILABLE = True
except ImportError:
    RouteredModel = None  # type: ignore[assignment]
    RouteredTeam = None  # type: ignore[assignment]
    AGNO_AVAILABLE = False


class AgnoAdapter:
    """FrameworkAdapter implementation for Agno."""

    name: str = "agno"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        """Returns a RouteredModel preconfigured for the given Agno model.

        v0.0.2: ignores framework_model — RouteredModel uses task_type
        internally. v0.0.3 will respect a passed-in cost_filter / model_pool
        from the original Agno model config.
        """
        if not AGNO_AVAILABLE:
            raise ImportError(
                "Agno not installed. pip install 'llm-routing[agno]'"
            )
        return RouteredModel(task_type="code")  # type: ignore[call-arg]

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        """Best-effort: try to read agent.name from an Agno Agent instance."""
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "name", None)

    @classmethod
    def is_available(cls) -> bool:
        return AGNO_AVAILABLE


__all__ = ["AgnoAdapter", "RouteredModel", "RouteredTeam", "AGNO_AVAILABLE"]
