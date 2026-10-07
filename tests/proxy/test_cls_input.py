"""M1.5: the classifier input, assembled from the request body.

Rules under test: the newest human prompt, earlier human prompts and the last
assistant text are what the classifier sees; tool results, tool calls, thinking and
Claude Code's reminder blocks are not; reminders change nothing; the layout is the
one p_eval's judges and the eval harness used; the caps hold.
"""

from __future__ import annotations

from llm_router import local_classifier as lc
from llm_router.proxy import cls_input

SYSTEM = [{"type": "text", "text": "You are Claude Code.\n\nPrimary working directory: /work/demo\nPlatform: darwin"}]


def _user(text):
    return {"role": "user", "content": text}


def _assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def _text(t):
    return {"type": "text", "text": t}


def _fixture() -> dict:
    return {
        "system": SYSTEM,
        "messages": [
            _user("first ask: add a flag"),
            _assistant({"type": "thinking", "thinking": "SECRET-THINKING"}, _text("Added the flag."),
                       {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "SECRET-TOOL-INPUT"}}),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET-TOOL-RESULT"}]},
            _assistant(_text("Done. Tests pass.")),
            _user("now commit it"),
        ],
    }


EXPECTED_CONTEXT = (
    "Working directory: /work/demo\n\n"
    "Earlier user prompt 1/1:\nfirst ask: add a flag\n\n"
    "Assistant's last message before this prompt (tail):\nDone. Tests pass."
)


def test_fixture_body_gives_the_expected_string():
    out = cls_input.assemble(_fixture())
    assert isinstance(out, lc.Assembled)
    assert out.context == EXPECTED_CONTEXT
    assert out.prompt == "now commit it"
    assert str(out) == EXPECTED_CONTEXT + "\n\nNewest prompt:\nnow commit it"


def test_tool_thinking_and_tool_use_blocks_never_reach_the_classifier():
    out = str(cls_input.assemble(_fixture()))
    for secret in ("SECRET-THINKING", "SECRET-TOOL-INPUT", "SECRET-TOOL-RESULT"):
        assert secret not in out


def test_reminder_blocks_change_nothing():
    plain = cls_input.assemble(_fixture())
    noisy = _fixture()
    reminder = "<system-reminder>\nthe date is today\nCLAUDE.md says be brief\n</system-reminder>"
    noisy["messages"][0] = {"role": "user", "content": [_text(reminder), _text("first ask: add a flag"), _text(reminder)]}
    noisy["messages"][-1] = {"role": "user", "content": [_text(reminder), _text("now   commit\nit"), _text(reminder)]}
    noisy["messages"][3] = _assistant(_text("Done. Tests pass."), _text(reminder))
    out = cls_input.assemble(noisy)
    assert str(out) == str(plain) and "system-reminder" not in str(out)


def test_in_conversation_system_messages_are_not_turns():
    body = _fixture()
    body["messages"].insert(3, {"role": "system", "content": "SECRET-MID-SYSTEM"})
    assert str(cls_input.assemble(body)) == str(cls_input.assemble(_fixture()))


def test_first_prompt_says_so():
    out = cls_input.assemble({"system": SYSTEM, "messages": [_user("which git branch am I on?")]})
    assert out.context == "Working directory: /work/demo\n\n" + cls_input._FIRST
    assert out.prompt == "which git branch am I on?"


def test_no_working_directory_means_no_line():
    out = cls_input.assemble({"system": "plain system prompt", "messages": [_user("hi")]})
    assert out.context == cls_input._FIRST
    assert cls_input.assemble({"messages": [_user("hi")]}).context == cls_input._FIRST


def test_string_system_prompt_is_read_too():
    out = cls_input.assemble({"system": "x\nPrimary working directory: /a b/c\ny", "messages": [_user("hi")]})
    assert out.context.startswith("Working directory: /a b/c\n")


def test_caps():
    body = {"system": SYSTEM, "messages": [
        _user("a" * 400), _assistant(_text("one")),
        _user("b" * 400), _assistant(_text("two")),
        _user("c" * 400), _assistant(_text("three")),
        _user("d" * 400), _assistant(_text("x" * 900)),
        _user("p" * 2500),
    ]}
    out = cls_input.assemble(body)
    assert out.prompt == "p" * 2000
    earlier = [part for part in out.context.split("\n\n") if part.startswith("Earlier user prompt")]
    assert len(earlier) == 3                                      # b, c, d: the three newest
    assert earlier[0].startswith("Earlier user prompt 1/3:\nbbb") and len(earlier[0].split("\n", 1)[1]) == 300
    assert earlier[2].startswith("Earlier user prompt 3/3:\nddd")
    tail = out.context.split("Assistant's last message before this prompt (tail):\n", 1)[1]
    assert tail == "[...] " + "x" * 500                           # the last 500 chars, marked


def test_assistant_text_after_the_newest_prompt_is_not_context():
    body = _fixture()
    body["messages"] += [_assistant(_text("ANSWER-TO-THE-NEWEST-PROMPT"))]
    assert "ANSWER-TO-THE-NEWEST-PROMPT" not in str(cls_input.assemble(body))


def test_tool_loop_tail_still_finds_the_newest_human_prompt():
    body = _fixture()
    body["messages"] += [
        _assistant({"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "SECRET-TOOL-RESULT"}]},
    ]
    out = cls_input.assemble(body)
    assert out.prompt == "now commit it" and "SECRET-TOOL-RESULT" not in str(out)


def test_empty_and_odd_bodies_do_not_raise():
    assert cls_input.assemble({}).prompt == ""
    assert cls_input.assemble({"messages": []}).prompt == ""
    assert cls_input.assemble({"messages": [{"role": "user", "content": []}, "junk", None]}).prompt == ""
    assert cls_input.assemble({"messages": [_user(None)], "system": 7}).prompt == ""


def test_feeds_the_p_eval_item_template():
    msg = lc._payload("m", cls_input.assemble(_fixture()))["messages"][1]["content"]
    assert msg.startswith("### ITEM id=turn\n[CONTEXT available when the prompt was sent]\nWorking directory: /work/demo")
    assert "[PROMPT TO LABEL]\nnow commit it\n### END ITEM id=turn" in msg
