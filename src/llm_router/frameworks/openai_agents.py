"""OpenAI Agents SDK adapter — stub.

OpenAI's Agents SDK (https://github.com/openai/openai-agents-python,
formerly Swarm) provides Agent + Runner abstractions. The adapter:
    1. Wraps the openai.AsyncOpenAI client used by the SDK.
    2. Intercepts chat.completions.create and routes via llm_router.
    3. Reads the current Agent's name from the Runner context for agent_id.

v0.0.2 ships this stub; concrete impl in v0.0.3+.
"""
from __future__ import annotations

from typing import Any


OPENAI_AGENTS_AVAILABLE = False


class OpenAIAgentsAdapter:
    name: str = "openai-agents"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        raise NotImplementedError("OpenAI Agents SDK adapter lands in v0.0.3+.")

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "name", None)

    @classmethod
    def is_available(cls) -> bool:
        return OPENAI_AGENTS_AVAILABLE
