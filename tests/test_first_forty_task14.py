"""Task 14: local debugging must not classify as research.

Observed repeatedly in a live session, including while this fix was written:
asking why a local surface was broken ("what is this $0.67 in the statusline")
classified as ``research/moderate``. Enforcement then held Bash, because QA task
types deliberately do NOT exempt even read-only commands — that invariant is
correct and is asserted in test_route_enforcement_hooks.py, so the fix belongs
in the classifier, not the gate.

Root cause: the ``research`` intent pattern claimed ``investigate``, ``find
out``, ``look up``, ``discover`` and ``check if`` as bare verbs. Those are the
ordinary vocabulary of local debugging. They now only signal research when
paired with an external-knowledge object.
"""

from __future__ import annotations

import pytest

from llm_router.classify import classify_signals

# Prompts about the user's own machine, repo or running state. None of these can
# be answered by a model that cannot see the host, so routing them to a research
# provider is wasted latency at best and a hold on local tools at worst.
LOCAL_DEBUGGING = [
    "investigate why the statusline is blank",
    "look into why the hook did not fire",
    "find out which python the statusline resolves",
    "check if the backup files are still accumulating",
    "discover what is writing to my home directory",
    "investigate this test failure",
    "look up the definition of _is_readonly_bash in the repo",
]

# Genuine external-knowledge questions that must keep routing to research.
EXTERNAL_RESEARCH = [
    "research the latest LLM routing benchmarks",
    "look up the current pricing for OpenRouter",
    "what is the latest release of the MCP spec",
    "who acquired Cursor",
    "find the best practices for plugin marketplaces",
    "competitive analysis of model routers",
    "how much did Anthropic raise in its last round",
]


@pytest.mark.parametrize("prompt", LOCAL_DEBUGGING)
def test_local_debugging_is_not_research(prompt):
    signal = classify_signals(prompt)
    assert signal.task_type != "research", (
        f"{prompt!r} classified as research. A stateless research model cannot "
        "see this host, and the classification holds local tools behind a route "
        "that can never satisfy the question."
    )


@pytest.mark.parametrize("prompt", EXTERNAL_RESEARCH)
def test_external_questions_still_route_to_research(prompt):
    """The narrowing must not cost real research routing."""
    signal = classify_signals(prompt)
    assert signal.task_type == "research", (
        f"{prompt!r} no longer classifies as research — the intent pattern was "
        "narrowed too far and genuine web-grounded work will go to a local model"
    )


def test_the_prompt_that_started_this():
    """The exact live prompt that blocked repo work, verbatim."""
    signal = classify_signals("what is this 0.67$ in the statusline")
    assert signal.task_type != "research", (
        "the reported prompt still classifies as research"
    )
