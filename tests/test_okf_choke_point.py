"""Every execution path must attach repo knowledge through the same function.

OKF reached 2 of 7 execution paths — `router.py` and
`direct_executor.execute_chain`. The tool loop, the Codex agent, the Claude
agent, the Gemini agent and `llm_local_task` all ran blind. Nobody decided that;
it is what happens when injection is something each new path has to REMEMBER.

Measured cost on one audited day: 101 drafts injected into a session, zero used,
a third of them asserting statuses the model had no way to observe — because it
was answering questions about a repository it could not see.

This test is the thing that makes forgetting loud. A new execution path that
does not cross `llm_router.context_injection` fails here, at the commit that
adds it, rather than weeks later in a log audit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "llm_router"

# Every module that sends a prompt to a model on the user's behalf.
EXECUTION_PATHS = [
    "hooks/agent_loop.py",
    "hooks/direct_executor.py",
    "codex_agent.py",
    "claude_agent.py",
    "gemini_cli_agent.py",
    "tools/local_task.py",
]


@pytest.mark.parametrize("rel", EXECUTION_PATHS)
def test_execution_path_crosses_the_choke_point(rel):
    src = (SRC / rel).read_text()
    assert "context_injection" in src, (
        f"{rel} sends prompts to a model but never imports "
        f"llm_router.context_injection, so it runs with no repo knowledge. "
        f"Call inject() or inject_system_prompt() — do not add a third private "
        f"copy of the retrieval logic."
    )


def test_only_the_choke_point_implements_injection():
    """No module may call `okf.inject_context` itself.

    The rule is about INJECTION, not retrieval. `auto-route` legitimately calls
    `find_relevant` to ask "is there knowledge for this prompt?" as a gating
    decision — it needs the concept list, not an enriched prompt, and that is a
    different question. What must not be duplicated is the attach step: two
    copies is how one of them lost its scope argument and how five execution
    paths ended up with neither.
    """
    skip_prefixes = ("#", '"""', "'''", "*")
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in ("context_injection.py", "okf.py", "router.py"):
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith(skip_prefixes):
                continue
            if ".inject_context(" in line:
                offenders.append(f"{path.relative_to(SRC)}:{i}")
    assert not offenders, (
        "these attach OKF context themselves instead of calling "
        "context_injection.inject(): " + ", ".join(offenders)
    )


def test_injection_is_fail_open():
    """A broken knowledge store must never break a routed call."""
    from llm_router.context_injection import inject
    assert inject("hello", root="/nonexistent/path/xyz") == "hello"
    assert inject("") == ""


def test_a_single_switch_disables_it_everywhere(monkeypatch):
    from llm_router.context_injection import enabled, inject
    monkeypatch.setenv("LLM_ROUTER_CONTEXT_INJECTION", "off")
    assert not enabled()
    assert inject("anything", root=str(SRC.parent.parent)) == "anything"
