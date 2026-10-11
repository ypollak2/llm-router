"""STEP-SIDECALL-1: a human typed prompt must not be labelled side_call or continuation.

Two request shapes hid human turns (docs/bugs/STEP-SIDECALL-1.md):

* a Message Threads continue (``thread.type == "continue"``, Claude Code 2.1.294-2.1.296,
  beta ``message-threads-2026-08-12``): ``messages`` is only the tail after the anchor reply
  and ``tools`` is omitted, so ``_has_client_tools`` was false and the call read ``side_call``;
* a prompt typed while a tool runs, appended as a text block to the tool results' turn,
  which read ``continuation``.

Every body below is synthesized from those shapes; no prompt text is reproduced.
"""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import httpx
import pytest

from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy import steps

TOKEN = "sk-ant-oat01-" + "Kd4" * 20  # fake
SCHEMA = {"type": "object", "properties": {}}
CORE = [{"name": n, "description": n, "input_schema": SCHEMA} for n in ("Bash", "Read", "Edit")]
AGENT = {"name": "Agent", "description": "launch a sub-agent", "input_schema": SCHEMA}
HANDBACK = {"name": "SubagentHandback", "description": "report", "input_schema": SCHEMA}
HUMAN = "synthetic typed prompt"
RESULT = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "synthetic output"}
REMINDER = {"type": "text", "text": "<system-reminder>\nsynthetic reminder\n</system-reminder>"}


def _user(*blocks) -> dict:
    return {"role": "user", "content": list(blocks)}


def _text(s: str = HUMAN) -> dict:
    return {"type": "text", "text": s}


def _continue(messages: list, *, parent: str | None = "msg_parent", tools=None, system=None) -> dict:
    """What Claude Code sends on a thread continue: the tail after the anchor, tools left out."""
    body = {"model": "claude-opus-5-5", "max_tokens": 64000, "stream": True, "messages": messages,
            "system": system if system is not None else [{"type": "text", "text": "synthetic ephemeral"}],
            "thread": {"type": "continue", "previous_message_id": parent}}
    if tools is not None:
        body["tools"] = tools
    return body


def _history(last: dict, *, tools=None, agent: bool = True) -> dict:
    """A stateless request: the whole conversation, tools present."""
    body = {"model": "claude-opus-5-5", "max_tokens": 64000, "stream": True,
            "tools": tools if tools is not None else CORE + ([AGENT] if agent else []),
            "system": [{"type": "text", "text": "synthetic system"}],
            "messages": [_user(_text("synthetic first prompt")),
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                             "input": {"command": "true"}}]},
                         _user(RESULT),
                         {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                         last]}
    return body


def _title_call() -> dict:
    return {"model": "claude-haiku-5-5", "max_tokens": 32, "stream": True,
            "system": [{"type": "text", "text": "Generate a short title"}],
            "messages": [_user(_text())]}


# -- the shapes that were mislabelled ---------------------------------------------------------


def test_a_human_prompt_on_a_thread_continue_is_turn_first_not_side_call():
    body = _continue([_user(_text())])
    assert not steps.has_client_tools(body)                   # tools stay on the thread
    assert steps.step_kind(body, parent_main=True) == steps.STEP_TURN_FIRST
    assert steps.is_first_call(body) is False                 # one message, but a reply precedes it
    assert steps.turn_fields(body, True)["turn_origin"] == steps.ORIGIN_TYPED


def test_a_tool_result_on_a_thread_continue_is_a_continuation_with_one_message():
    body = _continue([_user(RESULT, REMINDER)])
    assert steps.step_kind(body) == steps.STEP_CONTINUATION   # parent not needed
    assert steps.step_kind(body, parent_main=False) == steps.STEP_CONTINUATION
    assert steps.turn_origin(body) is None


def test_a_prompt_typed_beside_tool_results_is_turn_first_not_continuation():
    last = _user(RESULT, _text(), REMINDER)
    assert steps.step_kind(_history(last)) == steps.STEP_TURN_FIRST
    assert steps.turn_origin(_history(last)) == steps.ORIGIN_TYPED
    assert steps.step_kind(_continue([last]), parent_main=True) == steps.STEP_TURN_FIRST


def test_a_prompt_typed_beside_tool_results_inside_a_sub_agent_is_not_a_human_turn():
    last = _user(RESULT, _text())
    assert steps.step_kind(_history(last, agent=False)) == steps.STEP_SUBAGENT_TURN
    assert steps.step_kind(_history(last, tools=CORE + [AGENT, HANDBACK])) == steps.STEP_SUBAGENT_TURN


def test_unknown_parent_on_a_human_text_continue_is_not_counted_as_a_main_thread_turn():
    body = _continue([_user(_text())], parent="msg_never_seen")
    assert steps.step_kind(body, parent_main=None) == steps.STEP_SUBAGENT_TURN
    assert steps.step_kind(body, parent_main=False) == steps.STEP_SUBAGENT_TURN
    assert steps.is_main_thread(body, None) is None           # the ledger column says unknown


def test_a_sub_agent_marker_beats_a_main_parent():
    system = [{"type": "text", "text": "Messages from the agent that launched you"}]
    body = _continue([_user(_text())], system=system)
    assert steps.is_main_thread(body, True) is False
    assert steps.step_kind(body, parent_main=True) == steps.STEP_SUBAGENT_TURN


def test_a_harness_only_turn_on_a_main_thread_continue_is_a_harness_turn():
    note = _text("<task-notification>\nsynthetic\n</task-notification>")
    assert steps.step_kind(_continue([_user(note)]), parent_main=True) == steps.STEP_HARNESS_TURN


def test_a_continue_that_carries_tools_reads_its_own_launcher():
    body = _continue([_user(_text())], tools=CORE + [AGENT])
    assert steps.is_main_thread(body, None) is True
    assert steps.step_kind(body) == steps.STEP_TURN_FIRST
    sub = _continue([_user(_text())], tools=CORE)
    assert steps.step_kind(sub, parent_main=True) == steps.STEP_SUBAGENT_TURN


# -- what must stay as it was -----------------------------------------------------------------


@pytest.mark.parametrize("body", [
    _title_call(),
    dict(_title_call(), model="claude-sonnet-5-5", max_tokens=1),                      # quota probe
    dict(_title_call(), messages=[_user(_text()), {"role": "assistant", "content": [_text("a")]},
                                  _user(_text("summarize the conversation"))]),       # compaction / summary
    dict(_title_call(), tools=[{"type": "web_search_20250305", "name": "web_search"}]),  # server tool only
    dict(_title_call(), thread={"type": "create"}),
])
def test_genuine_side_calls_stay_side_calls(body):
    assert steps.step_kind(body) == steps.STEP_SIDE_CALL
    assert steps.turn_origin(body) is None


def test_a_thread_continue_with_a_different_thread_type_is_not_a_continue():
    body = dict(_title_call(), thread={"type": "bogus", "previous_message_id": "msg_x"})
    assert steps.step_kind(body) == steps.STEP_SIDE_CALL


@pytest.mark.parametrize("extra", [[], [REMINDER], [_text("[Request interrupted by user for tool use]")]])
def test_tool_result_follow_ups_stay_continuations(extra):
    last = _user(RESULT, *extra)
    assert steps.step_kind(_history(last)) == steps.STEP_CONTINUATION
    assert steps.step_kind(_history(last, agent=False)) == steps.STEP_CONTINUATION
    assert steps.step_kind(_continue([last])) == steps.STEP_CONTINUATION


def test_an_image_beside_the_tool_results_is_still_a_continuation():
    last = _user(RESULT, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}})
    assert steps.step_kind(_history(last)) == steps.STEP_CONTINUATION
    assert steps.step_ineligible(_history(last)) == "media"


def test_serving_eligibility_is_unchanged():
    last = _user(RESULT, REMINDER)
    assert steps.step_class(_history(last), {"continuation"}) == "continuation"
    assert steps.step_class(_continue([last]), {"continuation"}) is None   # a thread cannot be served


def test_stateless_first_and_later_human_turns_are_unchanged():
    first = _history(_user(_text()))
    first["messages"] = first["messages"][:1]
    assert steps.step_kind(first) == steps.STEP_TURN_FIRST
    assert steps.step_kind(dict(first, tools=CORE)) == steps.STEP_SUBAGENT_FIRST
    assert steps.step_kind(_history(_user(_text()))) == steps.STEP_TURN_FIRST
    assert steps.step_kind(_history(_user(_text()), agent=False)) == steps.STEP_SUBAGENT_TURN


# -- through the proxy: the parent reply carries main-thread-ness to the continue ------------


class _Upstream:
    """Answers every call with the next msg_id: msg_1, msg_2, ..."""

    def __init__(self):
        self.ids = (f"msg_{n}" for n in itertools.count(1))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = {"id": next(self.ids), "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1}}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()),
                              headers={"content-type": "application/json"})


async def _session(tmp_path: Path, bodies: list[dict]) -> list[dict]:
    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "proxy_calls.jsonl",
                            steps=frozenset())
    app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(_Upstream())),
                       backend_factory=lambda model: None)
    h = {"authorization": f"Bearer {TOKEN}", "anthropic-version": "2023-06-01", "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        for body in bodies:
            r = await c.post("/v1/messages", content=json.dumps(dict(body, stream=False)), headers=h)
            assert r.status_code == 200
    return ledger.read_rows(tmp_path / "proxy_calls.jsonl")


async def test_a_thread_of_main_thread_calls_is_labelled_end_to_end(tmp_path):
    create = dict(_history(_user(RESULT)), thread={"type": "create"})        # msg_1, main thread
    rows = await _session(tmp_path, [
        create,
        _continue([_user(_text())], parent="msg_1"),                         # msg_2: typed prompt
        _continue([_user(RESULT)], parent="msg_2"),                          # msg_3: tool follow-up
        _continue([_user(RESULT, _text())], parent="msg_3"),                 # msg_4: prompt typed mid-run
        _continue([_user(_text())], parent="msg_999"),                       # msg_5: parent never seen
    ])
    assert [r["step_class"] for r in rows] == ["continuation", "turn_first", "continuation", "turn_first",
                                               "subagent_turn"]
    assert [r["thread"] for r in rows] == ["create", "continue", "continue", "continue", "continue"]
    assert [r["is_main_thread"] for r in rows] == [True, True, True, True, None]
    assert [r["turn_origin"] for r in rows] == [None, "typed", None, "typed", "subagent_brief"]
    assert [r["is_first_call"] for r in rows] == [False] * 5


async def test_a_sub_agent_thread_never_becomes_a_human_turn(tmp_path):
    sub = dict(_history(_user(RESULT), tools=CORE + [AGENT, HANDBACK]), thread={"type": "create"})  # msg_1
    rows = await _session(tmp_path, [sub, _continue([_user(_text())], parent="msg_1")])
    assert [r["step_class"] for r in rows] == ["continuation", "subagent_turn"]
    assert rows[1]["is_main_thread"] is False


async def test_a_side_call_row_stays_a_side_call_and_has_no_thread(tmp_path):
    (row,) = await _session(tmp_path, [_title_call()])
    assert row["step_class"] == "side_call" and row["thread"] is None


def test_the_registry_is_bounded():
    assert 1024 <= ps._THREAD_MAIN_MAX <= 65536


def test_the_classifier_does_not_mutate_its_body():
    body = _continue([_user(RESULT, _text())])
    before = copy.deepcopy(body)
    steps.step_fields(body, parent_main=True)
    steps.turn_fields(body, True)
    assert body == before
