"""Pydantic AI framework adapter — stub.

Pydantic AI (https://github.com/pydantic/pydantic-ai) is a type-safe
agent framework. Each Agent has a model parameter that accepts either a
model string or a custom Model implementation. The adapter ships a
LLMRouterModel that implements Pydantic AI's Model protocol.

v0.0.2 ships this stub; concrete impl in v0.0.3+.
"""
from __future__ import annotations

from typing import Any


PYDANTIC_AI_AVAILABLE = False


class PydanticAiAdapter:
    name: str = "pydantic-ai"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        raise NotImplementedError("Pydantic AI adapter lands in v0.0.3+.")

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "name", None)

    @classmethod
    def is_available(cls) -> bool:
        return PYDANTIC_AI_AVAILABLE
