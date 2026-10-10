"""TURNFIRST-2: a sub-agent that holds the ``Agent`` launcher is not the main thread.

TURNFIRST-1 read "main thread" off the ``Agent``/``Task`` launcher alone. Claude Code now lets
sub-agents spawn sub-agents, so a general-purpose sub-agent carries ``Agent`` too: in the live
ledger after the TURNFIRST-1 deploy (2026-10-10 00:15:07Z to 08:17:31Z, n=100 rows) 4 rows were
``turn_first`` with ``is_main_thread`` true and ``turn_origin`` typed, and 3 of them were the
first calls of 3 general-purpose sub-agents (docs/bugs/TURNFIRST-2.md). The system prompts below
are shaped like Claude Code 2.1.296's (the marker lines are verbatim; the rest is synthetic).
"""
from __future__ import annotations

import json

import pytest

from llm_router.proxy import backends as pb
from llm_router.proxy import haiku_arm, steps
from tests.test_proxy_tiers import Upstream, _app, _post, _rows

READ = {"name": "Read", "description": "read", "input_schema": {"type": "object"}}
AGENT = {"name": "Agent", "description": "Launch a new agent", "input_schema": {"type": "object"}}
HANDBACK = {"name": "SubagentHandback", "description": "Deliver your final report to your caller",
            "input_schema": {"type": "object"}}

IDENTITY = {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
MAIN_SYSTEM = [IDENTITY, {"type": "text", "text": (
    "You are an agent working with the user toward their goals, using your own judgment along the way. "
    "Use the instructions below and the tools available to you to assist the user.\n\n# Doing tasks\n...")}]
GENERAL_PURPOSE = ("You are an agent for Claude Code, Anthropic's official CLI for Claude. Given the user's "
                   "message, you should use the tools available to complete the task.")
LAUNCHED_BY = ("Messages from the agent that launched you — your task and any mid-task course "
               "corrections — direct your work.")
NOTES = ("Notes:\n- Agent threads always have their cwd reset between bash calls, as a result please only "
         "use absolute file paths.")
SUB_SYSTEM = [IDENTITY] + [{"type": "text", "text": t} for t in (GENERAL_PURPOSE, LAUNCHED_BY, NOTES)]
#: A custom agent (.claude/agents/*.md): its own prompt, then the lines the agent runner appends.
CUSTOM_SYSTEM = [IDENTITY] + [{"type": "text", "text": t} for t in
                              ("You review diffs for correctness.", LAUNCHED_BY, NOTES)]


def _body(*user_turns: str, system, tools=(READ, AGENT)) -> dict:
    msgs: list[dict] = []
    for i, turn in enumerate(user_turns):
        if i:
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": turn}]})
    return {"model": "claude-sonnet-5-5", "max_tokens": 64, "system": system, "messages": msgs,
            "tools": list(tools)}


def _labels(body: dict) -> tuple:
    f = steps.turn_fields(body)
    return steps.step_kind(body), f["is_main_thread"], f["is_first_call"], f["turn_origin"]


# ── the regression: a general-purpose sub-agent with the launcher ────────────

def test_general_purpose_subagent_first_call_with_the_agent_tool_is_not_turn_first():
    body = _body("Recompute the turn-first metrics and report.", system=SUB_SYSTEM)
    assert _labels(body) == (steps.STEP_SUBAGENT_FIRST, False, True, steps.ORIGIN_SUBAGENT_BRIEF)
    assert haiku_arm.is_main_thread(body) is False


def test_general_purpose_subagent_resume_with_the_agent_tool_is_a_subagent_turn():
    body = _body("Recompute the metrics.", "Also split by session.", system=SUB_SYSTEM)
    assert _labels(body) == (steps.STEP_SUBAGENT_TURN, False, False, steps.ORIGIN_SUBAGENT_BRIEF)


def test_custom_subagent_with_the_agent_tool_is_not_main_thread():
    body = _body("Review the diff.", system=CUSTOM_SYSTEM)
    assert _labels(body) == (steps.STEP_SUBAGENT_FIRST, False, True, steps.ORIGIN_SUBAGENT_BRIEF)


@pytest.mark.parametrize("marker", [LAUNCHED_BY, NOTES, GENERAL_PURPOSE], ids=["launched_by", "notes", "general_purpose"])
@pytest.mark.parametrize("as_string", [False, True], ids=["blocks", "string"])
def test_any_one_marker_vetoes_the_launcher(marker, as_string):
    """Fail toward NOT main: one sub-agent marker is enough, in a list or a string system field."""
    text = f"You are Claude Code.\n\nSome agent prompt.\n{marker} ..."
    body = _body("Do the task.", system=text if as_string else [{"type": "text", "text": text}])
    assert steps.is_main_thread(body) is False
    assert steps.step_kind(body) == steps.STEP_SUBAGENT_FIRST


def test_the_subagent_only_handback_tool_vetoes_the_launcher():
    body = _body("Do the task.", system=MAIN_SYSTEM, tools=(READ, AGENT, HANDBACK))
    assert steps.is_main_thread(body) is False


# ── the owner's main loop must stay main ─────────────────────────────────────

def test_main_thread_typed_turn_with_the_agent_tool_is_turn_first():
    body = _body("what is a mutex?", "and a semaphore?", system=MAIN_SYSTEM)
    body["model"] = "claude-opus-5-5"
    assert _labels(body) == (steps.STEP_TURN_FIRST, True, False, steps.ORIGIN_TYPED)
    assert haiku_arm.is_main_thread(body) is True


def test_main_thread_first_call_with_the_agent_tool_is_turn_first():
    body = _body("what is a mutex?", system=MAIN_SYSTEM)
    assert _labels(body) == (steps.STEP_TURN_FIRST, True, True, steps.ORIGIN_TYPED)


def test_marker_text_typed_by_the_user_does_not_demote_the_main_thread():
    """Only the ``system`` field is read: pasting a sub-agent transcript into a prompt is still a typed turn."""
    body = _body("hi", f"why does my agent say '{NOTES}' and '{GENERAL_PURPOSE}'?", system=MAIN_SYSTEM)
    assert _labels(body) == (steps.STEP_TURN_FIRST, True, False, steps.ORIGIN_TYPED)


def test_a_call_without_the_launcher_is_still_a_subagent():
    body = _body("Do the task.", system=MAIN_SYSTEM, tools=(READ,))
    assert steps.is_main_thread(body) is False


# ── the ledger row ───────────────────────────────────────────────────────────

async def test_ledger_rows_label_the_subagent_with_the_launcher_apart_from_the_main_turn(tmp_path, monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "query", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)
    app = _app(tmp_path, Upstream())
    typed = _body("what is a mutex?", system=MAIN_SYSTEM)
    sub = _body("Recompute the turn-first metrics and report.", system=SUB_SYSTEM)
    for b in (typed, sub):
        b["metadata"] = {"user_id": json.dumps({"session_id": "s-tf2"})}   # a sub-agent shares the session id
        assert (await _post(app, b)).status_code == 200
    got = [(r["step_class"], r["is_main_thread"], r["is_first_call"], r["turn_origin"]) for r in _rows(tmp_path)]
    assert got == [("turn_first", True, True, "typed"), ("subagent_first", False, True, "subagent_brief")]
