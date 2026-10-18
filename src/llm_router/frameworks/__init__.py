"""Framework adapters — wrap agent-framework model calls through LLM Router.

Each adapter sits between an agent framework (Agno, LangGraph,
CrewAI, OpenAI Agents SDK, Claude Agent SDK, Pydantic AI) and LLM Router's
routing brain. The adapter:

    1. Accepts the framework's model handle / message format.
    2. Routes the call via llm_router.router.
    3. Optionally tags the lineage row with framework + agent_id.
    4. Returns a response in the framework's expected shape.

This means the same LLM Router signal/decision config applies regardless of
which framework owns the agent loop. Pick the framework you like; routing
quality stays consistent.

Status (AGT A.6): every adapter is a thin ``base_url`` wrapper around the framework's own
OpenAI- or Anthropic-compatible client, pointed at ``llm-router gateway``. The framework package
is the user's to install; none is a llm-router dependency.
    - agno             — RouteredModel, RouteredTeam (llm_router.integrations.agno)
    - langgraph        — langchain_openai.ChatOpenAI
    - crewai           — crewai.LLM
    - openai_agents    — agents.OpenAIChatCompletionsModel over AsyncOpenAI
    - claude_agent_sdk — ClaudeAgentOptions with ANTHROPIC_BASE_URL
    - pydantic_ai      — OpenAIChatModel over OpenAIProvider
There is no Hermes adapter: "Hermes" names several tool-call formats, not one framework to wrap,
and a Hermes-format model is served by any OpenAI-compatible client against the gateway.
Runnable examples: examples/. Guide: docs/integration.md.
"""
from llm_router.frameworks.base import FrameworkAdapter

__all__ = ["FrameworkAdapter"]


# Concrete adapters are lazy-imported below so users without the framework
# installed don't get ImportError on `from llm_router.frameworks import ...`.

def get_agno_adapter():
    """Return the Agno adapter (RouteredModel + RouteredTeam re-exported)."""
    from llm_router.frameworks import agno

    return agno

