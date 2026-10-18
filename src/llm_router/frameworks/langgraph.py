"""LangGraph adapter: a LangChain chat model pointed at the llm-router gateway.

LangGraph (https://github.com/langchain-ai/langgraph) runs graphs of LangChain runnables, and
its agents call a LangChain chat model. ``wrap_model`` returns ``langchain_openai.ChatOpenAI``
with ``base_url`` set to the gateway, so every node's model call (streamed or not, with or
without tools) is routed, gated and ledgered by llm-router. Nothing is subclassed.

    from llm_router.frameworks.langgraph import LangGraphAdapter
    model = LangGraphAdapter().wrap_model("auto")      # then create_react_agent(model, tools)

Install the framework yourself (it is never a llm-router dependency):
    pip install langgraph langchain-openai
Runnable, pinned: examples/langgraph/.
"""
from __future__ import annotations

from typing import Any

from llm_router.frameworks import _gateway

LANGGRAPH_AVAILABLE = _gateway.module_present("langgraph") and _gateway.module_present("langchain_openai")


class LangGraphAdapter:
    name: str = "langgraph"

    def wrap_model(self, framework_model: Any = None, agent_id: str | None = None):
        """A ``ChatOpenAI`` aimed at the gateway. ``framework_model`` is a model name, an
        existing chat model whose ``model_name`` is reused, or None for ``auto``.

        ``agent_id`` is accepted for the FrameworkAdapter protocol; the gateway has no
        per-agent header, so it is not sent."""
        chat_openai = _gateway.require("langchain_openai", "langchain-openai").ChatOpenAI
        return chat_openai(
            model=_gateway.model_name(framework_model),
            base_url=_gateway.openai_base_url(),
            api_key=_gateway.gateway_api_key(),
            streaming=True,
        )

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        """LangGraph carries the node name in ``RunnableConfig['metadata']['langgraph_node']``."""
        config = framework_runtime if isinstance(framework_runtime, dict) else {}
        metadata = config.get("metadata")
        node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
        return node if isinstance(node, str) and node else None

    @classmethod
    def is_available(cls) -> bool:
        return LANGGRAPH_AVAILABLE
