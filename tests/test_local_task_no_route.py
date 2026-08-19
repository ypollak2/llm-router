"""Local machine / filesystem / credential / run-app requests must NOT route.

Regression guard for routes misfiring on tasks no routed model can do — e.g.
"search my machine for the token" was being sent to llm_research (a web model).
needs_claude_tools() must flag these so they stay native, while genuine general
questions still route to the cheap models.
"""
import pytest

from llm_router.hooks.chain_builder import needs_claude_tools

# (prompt, task_type) — must be detected as needing native tools (no route)
NO_ROUTE = [
    ("Search wider for the token", "research"),
    ("where should I store the PyPI token", "query"),
    ("run the app and screenshot it", "code"),
    ("launch the dashboard locally", "query"),
    ("what's in my .env file", "query"),
    ("find the api key on my machine", "research"),
    ("publish the release", "code"),
    ("check ~/.llm-router for the config", "query"),
]

# (prompt, task_type) — genuine general questions that SHOULD still route
ROUTABLE = [
    ("what is the capital of France", "query"),
    ("summarize the latest news about fusion energy", "research"),
    ("write a haiku about autumn leaves", "generate"),
    ("find a good recipe for sourdough bread", "research"),
    ("explain how the TCP handshake works", "query"),
    ("what are the tradeoffs of microservices", "analyze"),
]


@pytest.mark.parametrize("prompt,task_type", NO_ROUTE)
def test_local_tasks_need_native_tools(prompt, task_type):
    assert needs_claude_tools(prompt, task_type) is True, prompt


@pytest.mark.parametrize("prompt,task_type", ROUTABLE)
def test_general_questions_still_route(prompt, task_type):
    assert needs_claude_tools(prompt, task_type) is False, prompt
