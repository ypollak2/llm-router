"""Framework adapters — wrap agent-framework model calls through LLM Router.

Each adapter sits between an agent framework (Agno, Hermes, LangGraph,
CrewAI, OpenAI Agents SDK, Claude Agent SDK, Pydantic AI) and LLM Router's
routing brain. The adapter:

    1. Accepts the framework's model handle / message format.
    2. Routes the call via llm_router.router.
    3. Optionally tags the lineage row with framework + agent_id.
    4. Returns a response in the framework's expected shape.

This means the same LLM Router signal/decision config applies regardless of
which framework owns the agent loop. Pick the framework you like; routing
quality stays consistent.

Status as of v0.0.2:
    - agno         — concrete, ported from llm_router.integrations.agno
                     (RouteredModel, RouteredTeam)
    - hermes       — skeleton; concrete impl in v0.0.3
    - langgraph    — stub; PR welcome
    - crewai       — stub; PR welcome
    - openai_agents — stub (OpenAI Agents SDK, formerly Swarm)
    - claude_agent_sdk — stub (Anthropic's official agent SDK)
    - pydantic_ai  — stub
"""
from llm_router.frameworks.base import FrameworkAdapter

__all__ = ["FrameworkAdapter"]


# Concrete adapters are lazy-imported below so users without the framework
# installed don't get ImportError on `from llm_router.frameworks import ...`.

def get_agno_adapter():
    """Return the Agno adapter (RouteredModel + RouteredTeam re-exported)."""
    from llm_router.frameworks import agno

    return agno


def get_hermes_adapter():
    """Return the Hermes adapter."""
    from llm_router.frameworks import hermes

    return hermes
