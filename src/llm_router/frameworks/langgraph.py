"""LangGraph framework adapter — stub.

LangGraph (https://github.com/langchain-ai/langgraph) is LangChain's
graph-based agent runtime. The integration path:
    1. Implement a Runnable subclass that delegates to llm_router.router.
    2. Map LangGraph's checkpointer hooks to LLM Router lineage rows.
    3. Pass session_id via the LangGraph RunnableConfig's metadata.

v0.0.2 ships this stub; concrete impl in v0.0.3+.
"""
from __future__ import annotations

from typing import Any


LANGGRAPH_AVAILABLE = False


class LangGraphAdapter:
    name: str = "langgraph"

    def wrap_model(self, framework_model: Any, agent_id: str | None = None):
        raise NotImplementedError("LangGraph adapter lands in v0.0.3+.")

    def detect_agent_id(self, framework_runtime: Any) -> str | None:
        # LangGraph passes node names through RunnableConfig.metadata —
        # extract from there once concrete impl exists.
        return None

    @classmethod
    def is_available(cls) -> bool:
        return LANGGRAPH_AVAILABLE
