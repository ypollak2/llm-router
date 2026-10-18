"""CrewAI adapter: a ``crewai.LLM`` pointed at the llm-router gateway.

CrewAI (https://github.com/crewAIInc/crewAI) takes an ``LLM(model=..., base_url=...)`` per agent.
``wrap_model`` returns one whose ``base_url`` is the gateway's OpenAI-compatible endpoint, so each
agent call is routed, gated and ledgered by llm-router. Nothing is subclassed.

    from llm_router.frameworks.crewai import CrewAIAdapter
    llm = CrewAIAdapter().wrap_model("auto")           # Agent(role=..., llm=llm)

Install the framework yourself (it is never a llm-router dependency): pip install crewai
Runnable, pinned: examples/crewai/.
"""
from __future__ import annotations

from typing import Any

from llm_router.frameworks import _gateway

CREWAI_AVAILABLE = _gateway.module_present("crewai")


class CrewAIAdapter:
    name: str = "crewai"

    def wrap_model(self, framework_model: Any = None, agent_id: str | None = None):
        """A ``crewai.LLM`` aimed at the gateway. ``framework_model`` is a model name, an
        existing ``LLM`` whose ``model`` is reused, or None for ``auto``. CrewAI picks its
        client from a ``provider/`` prefix, so the name is sent as ``openai/<model>``; the
        gateway sees the bare name.

        ``agent_id`` is accepted for the FrameworkAdapter protocol; the gateway has no
        per-agent header, so it is not sent."""
        llm = _gateway.require("crewai", "crewai").LLM
        name = _gateway.model_name(framework_model)
        return llm(
            model=name if name.startswith("openai/") else f"openai/{name}",
            base_url=_gateway.openai_base_url(),
            api_key=_gateway.gateway_api_key(),
        )

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        # A CrewAI Agent's ``role`` is its identity.
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "role", None)

    @classmethod
    def is_available(cls) -> bool:
        return CREWAI_AVAILABLE
