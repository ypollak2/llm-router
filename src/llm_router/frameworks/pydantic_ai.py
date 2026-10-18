"""Pydantic AI adapter: an ``OpenAIChatModel`` whose provider points at the llm-router gateway.

Pydantic AI (https://github.com/pydantic/pydantic-ai) takes a model object per ``Agent``.
``wrap_model`` returns ``OpenAIChatModel`` over an ``OpenAIProvider`` whose ``base_url`` is the
gateway, so every ``run`` / ``run_stream`` (with or without tools) is routed, gated and ledgered by
llm-router. Nothing is subclassed.

    from llm_router.frameworks.pydantic_ai import PydanticAiAdapter
    agent = Agent(PydanticAiAdapter().wrap_model("auto"))

Install the framework yourself (it is never a llm-router dependency): pip install "pydantic-ai-slim[openai]"
Runnable, pinned: examples/pydantic_ai/.
"""
from __future__ import annotations

from typing import Any

from llm_router.frameworks import _gateway

PYDANTIC_AI_AVAILABLE = _gateway.module_present("pydantic_ai")


class PydanticAiAdapter:
    name: str = "pydantic-ai"

    def wrap_model(self, framework_model: Any = None, agent_id: str | None = None):
        """An ``OpenAIChatModel`` aimed at the gateway. ``framework_model`` is a model name,
        an existing model object whose ``model_name`` is reused, or None for ``auto``.

        ``agent_id`` is accepted for the FrameworkAdapter protocol; the gateway has no
        per-agent header, so it is not sent."""
        extra = "pydantic-ai-slim[openai]"
        model_cls = _gateway.require("pydantic_ai.models.openai", extra).OpenAIChatModel
        provider_cls = _gateway.require("pydantic_ai.providers.openai", extra).OpenAIProvider
        provider = provider_cls(
            base_url=_gateway.openai_base_url(), api_key=_gateway.gateway_api_key())
        return model_cls(_gateway.model_name(framework_model), provider=provider)

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        agent_obj = getattr(framework_runtime, "agent", framework_runtime)
        return getattr(agent_obj, "name", None)

    @classmethod
    def is_available(cls) -> bool:
        return PYDANTIC_AI_AVAILABLE
