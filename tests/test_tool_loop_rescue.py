"""The context-dependent gate sits upstream of the tool branch, and starved it.

Measured in the production debug log before this arm existed: 348 invocations
skipped as context-dependent, of which **0** ever logged a `needs_tools` value —
the gate returns before the tool branch is reached. That is why `needs_tools=True`
appears 3 times in 1961 invocations and the local agent loop had run once ever.

The gate's premise is that a routed model would answer BLIND about local state.
A model holding read_file/search_files is not blind, so for tool-shaped prompts
the premise fails and the gate is in the wrong place.

This is opt-in (`LLM_ROUTER_LOCAL_AGENT_LOOP`) because enabling it lets a local
model reach write_file/edit_file/run_command on prompts that previously always
went to Claude, and the loop has no diff gate yet.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HOOK = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    """auto-route.py is hyphenated, so it cannot be imported by name."""
    spec = importlib.util.spec_from_file_location("auto_route_under_test", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hook():
    return _load()


TOOL_SHAPED = "fix the timeout bug in src/llm_router/hooks/auto-route.py"


def test_on_by_default(hook, monkeypatch):
    # conftest sets this off suite-wide so no test spawns a live loop; this test
    # is about the DEFAULT, so it has to see a clean environment.
    """Default-on became defensible once writes stopped reaching the tree
    (agent_writes defaults to `propose`) and the loop gained a wall-clock budget.
    Before either, this was correctly off."""
    monkeypatch.delenv("LLM_ROUTER_LOCAL_AGENT_LOOP", raising=False)
    assert hook._local_agent_loop_enabled() is True
    assert hook._tool_loop_rescue(TOOL_SHAPED, "code") is True


@pytest.mark.parametrize("val", ["0", "off", "false", "no", "OFF", " off "])
def test_it_can_be_turned_off(hook, monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_LOOP", val)
    assert hook._local_agent_loop_enabled() is False
    assert hook._tool_loop_rescue(TOOL_SHAPED, "code") is False


@pytest.mark.parametrize("val", ["1", "on", "true", "yes", "", "maybe"])
def test_anything_else_leaves_it_on(hook, monkeypatch, val):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_LOOP", val)
    assert hook._local_agent_loop_enabled() is True


def test_the_loop_has_a_wall_clock_budget(hook, monkeypatch):
    """The loop runs inside UserPromptSubmit, before the user sees anything.
    15 iterations at a 60s per-call timeout is a 15-minute worst case, which is
    why a per-call timeout alone is not enough to justify running by default."""
    monkeypatch.delenv("LLM_ROUTER_AGENT_LOOP_BUDGET_S", raising=False)
    assert hook._agent_loop_budget_s() == 90.0
    monkeypatch.setenv("LLM_ROUTER_AGENT_LOOP_BUDGET_S", "30")
    assert hook._agent_loop_budget_s() == 30.0


@pytest.mark.parametrize("bad", ["nonsense", "-5", "0", ""])
def test_a_nonsense_budget_falls_back_rather_than_disabling_the_bound(hook, monkeypatch, bad):
    """A budget of 0 or a typo must not mean "no limit" — that silently restores
    the 15-minute worst case this bound exists to remove."""
    monkeypatch.setenv("LLM_ROUTER_AGENT_LOOP_BUDGET_S", bad)
    assert hook._agent_loop_budget_s() == 90.0


def test_the_prompt_it_exists_for_is_both_context_dependent_and_tool_shaped(hook):
    """The premise of the whole change: these prompts were being dropped, and they
    are exactly the ones the agent loop was built to serve."""
    assert hook._is_context_dependent(TOOL_SHAPED), "not the population in question"
    from llm_router.hooks.chain_builder import needs_claude_tools
    assert needs_claude_tools(TOOL_SHAPED, "code"), "not tool-shaped"


def test_enabled_rescues_a_tool_shaped_prompt(hook, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_LOCAL_AGENT_LOOP", raising=False)
    assert hook._tool_loop_rescue(TOOL_SHAPED, "code") is True


def test_enabled_does_not_rescue_a_prompt_with_no_tool_shape(hook, monkeypatch):
    """A pure Q&A reference to local state stays blocked even with the flag on —
    tools do not help a model that has nothing to go read."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_LOOP", "1")
    assert hook._tool_loop_rescue("what did we decide about that?", "query") is False


def test_the_rescue_and_the_execution_branch_use_one_predicate(hook, monkeypatch):
    """If these two could disagree, a prompt would be rescued past the gate and
    then routed to execute_chain — a blind text call on exactly the prompt the
    gate was protecting. Same function, same arguments, by construction."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_LOOP", "1")
    from llm_router.hooks.chain_builder import needs_claude_tools
    for p in (TOOL_SHAPED, "what did we decide about that?", "run the test suite"):
        assert hook._tool_loop_rescue(p, "code") is bool(needs_claude_tools(p, "code"))


def test_a_broken_chain_builder_fails_closed(hook, monkeypatch):
    """Rescue widens what gets routed, so its failure mode must be the old
    behaviour (skip), never an unguarded route."""
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_LOOP", "1")
    import llm_router.hooks.chain_builder as cb
    monkeypatch.setattr(cb, "needs_claude_tools",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hook._tool_loop_rescue(TOOL_SHAPED, "code") is False
