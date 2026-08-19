"""CrewAI framework adapter — stub.

CrewAI (https://github.com/joaomdmoura/crewAI) wraps LiteLLM under the
hood, so the integration is straightforward: provide a LiteLLM-compatible
completion function that delegates to llm_router.router.

v0.0.2 ships this stub; concrete impl in v0.0.3+.
"""
from __future__ import annotations

from typing import Any


CREWAI_AVAILABLE = False


class CrewAIAdapter:
    name: str = "crewai"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        raise NotImplementedError("CrewAI adapter lands in v0.0.3+.")

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        # Crew's Agent objects have a role field that maps well to agent_id.
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "role", None)

    @classmethod
    def is_available(cls) -> bool:
        return CREWAI_AVAILABLE
