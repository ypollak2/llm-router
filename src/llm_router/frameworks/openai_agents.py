"""OpenAI Agents SDK adapter: an ``OpenAIChatCompletionsModel`` over a gateway-pointed client.

The OpenAI Agents SDK (https://github.com/openai/openai-agents-python) runs ``Agent`` objects
through a ``Runner``. ``wrap_model`` returns ``agents.OpenAIChatCompletionsModel`` built on an
``openai.AsyncOpenAI`` whose ``base_url`` is the gateway, so each turn (``Runner.run`` or
``Runner.run_streamed``, with or without function tools) is routed, gated and ledgered by
llm-router. Nothing is subclassed. Pass it as ``Agent(model=...)``.

    from llm_router.frameworks.openai_agents import OpenAIAgentsAdapter
    agent = Agent(name="x", model=OpenAIAgentsAdapter().wrap_model("auto"))

Install the framework yourself (it is never a llm-router dependency): pip install openai-agents
Runnable, pinned: examples/openai_agents/.
"""
from __future__ import annotations

from typing import Any

from llm_router.frameworks import _gateway

OPENAI_AGENTS_AVAILABLE = _gateway.module_present("agents") and _gateway.module_present("openai")


class OpenAIAgentsAdapter:
    name: str = "openai-agents"

    def wrap_model(self, framework_model: Any = None, agent_id: str | None = None):
        """An ``OpenAIChatCompletionsModel`` aimed at the gateway. ``framework_model`` is a
        model name, an existing model object whose ``model`` is reused, or None for ``auto``.

        ``agent_id`` is accepted for the FrameworkAdapter protocol; the gateway has no
        per-agent header, so it is not sent."""
        agents = _gateway.require("agents", "openai-agents")
        client = _gateway.require("openai", "openai").AsyncOpenAI(
            base_url=_gateway.openai_base_url(), api_key=_gateway.gateway_api_key())
        return agents.OpenAIChatCompletionsModel(
            model=_gateway.model_name(framework_model), openai_client=client)

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "name", None)

    @classmethod
    def is_available(cls) -> bool:
        return OPENAI_AGENTS_AVAILABLE
