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


PROMPT_TEXT = "".join(f"{i:03d}|" for i in range(625))        # 2500 chars, every 4-char block differs
ASSISTANT_TEXT = "".join(f"{i:03d}|" for i in range(225))   # 900 chars, every 4-char block differs


def test_caps():
    body = {"system": SYSTEM, "messages": [
        _user("a" * 400), _assistant(_text("one")),
        _user("b" * 400), _assistant(_text("two")),
        _user("c" * 400), _assistant(_text("three")),
        _user("d" * 400), _assistant(_text(ASSISTANT_TEXT)),
        _user(PROMPT_TEXT),
    ]}
    out = cls_input.assemble(body)
    assert out.prompt == PROMPT_TEXT[-2000:]                      # the LAST 2,000 chars (PLAN.md:915)
    assert out.prompt != PROMPT_TEXT[:2000]
    earlier = [part for part in out.context.split("\n\n") if part.startswith("Earlier user prompt")]
    assert len(earlier) == 3                                      # b, c, d: the three newest
    assert earlier[0].startswith("Earlier user prompt 1/3:\nbbb") and len(earlier[0].split("\n", 1)[1]) == 300
    assert earlier[2].startswith("Earlier user prompt 3/3:\nddd")
    tail = out.context.split("Assistant's last message before this prompt (tail):\n", 1)[1]
    assert len(ASSISTANT_TEXT) == 900
    assert tail == "[...] " + ASSISTANT_TEXT[-500:]               # the last 500 chars, marked
    assert tail != "[...] " + ASSISTANT_TEXT[:500]                # not the head


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


# --- P1.7-c: a bounded backward scan -------------------------------------------------------


def _forward_reference(body: dict) -> lc.Assembled:
    """``assemble`` as merged in #301 (a full forward walk), kept verbatim as the oracle: on a history the scan
    window covers, the backward scan must give the same string and the same prompt."""
    from llm_router.proxy.steps import non_system

    msgs = non_system(body.get("messages") or [])
    newest = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user" and cls_input._human(msgs[i].get("content")):
            newest = i
            break
    prompt = cls_input._human(msgs[newest].get("content"))[-cls_input.PROMPT_CHARS:] if newest >= 0 else ""
    earlier, assistant = [], ""
    for m in msgs[:max(newest, 0)]:
        text = cls_input._human(m.get("content"))
        if not text:
            continue
        if m.get("role") == "user":
            earlier.append(text[:cls_input.EARLIER_CHARS])
        elif m.get("role") == "assistant":
            assistant = text
    earlier = earlier[-cls_input.EARLIER_PROMPTS:]
    parts = []
    cwd = cls_input._cwd(body)
    if cwd:
        parts.append(f"Working directory: {cwd}")
    if not earlier and not assistant:
        parts.append(cls_input._FIRST)
    parts += [f"Earlier user prompt {j + 1}/{len(earlier)}:\n{u}" for j, u in enumerate(earlier)]
    if assistant:
        tail = assistant if len(assistant) <= cls_input.ASSISTANT_CHARS else \
            "[...] " + assistant[-cls_input.ASSISTANT_CHARS:]
        parts.append(f"Assistant's last message before this prompt (tail):\n{tail}")
    return lc.Assembled("\n\n".join(parts), prompt)


def _random_message(rng, k):
    """Every shape the scan must tell apart: human text, reminder-only text, tool results with and without a
    reminder, assistant text, tool_use only, thinking only, in-conversation system messages, empty content."""
    reminder = "<system-reminder>r" + "x" * rng.randint(0, 40) + "</system-reminder>"
    return rng.choice([
        _user(f"ask {k} " + "w " * rng.randint(0, 200)),
        _user(reminder),
        _user("   "),
        {"role": "user", "content": [_text(reminder), _text(f"typed {k}")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "R"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "R"}, _text(reminder)]},
        _assistant(_text(f"answer {k} " + "a" * rng.randint(0, 700))),
        _assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {}}),
        _assistant({"type": "thinking", "thinking": "T"}),
        _assistant(_text("")),
        {"role": "system", "content": f"note {k}"},
    ])


def test_backward_scan_equals_the_forward_walk_on_600_random_histories():
    import random

    rng = random.Random(20261008)
    checked = with_context = 0
    for case in range(600):
        n = rng.randint(0, 60) if case % 3 else rng.randint(0, cls_input.MAX_SCAN_MESSAGES)
        body = {"system": SYSTEM, "messages": [_random_message(rng, k) for k in range(n)]}
        if case % 5 == 0:
            body["messages"].append(_user(f"the newest prompt {case}"))
        new, old = cls_input.assemble(body), _forward_reference(body)
        assert (str(new), new.context, new.prompt) == (str(old), old.context, old.prompt), case
        assert new.capped is False, case
        checked += 1
        with_context += "Earlier user prompt 3/3" in new.context and "Assistant's last message" in new.context
    assert checked == 600 and with_context >= 50   # not vacuous: many cases carry full context


def _counting(monkeypatch):
    calls = []
    real = cls_input._human

    def counted(content):
        calls.append(1)
        return real(content)
    monkeypatch.setattr(cls_input, "_human", counted)
    return calls


def _tool_pair(j):
    return [_assistant({"type": "tool_use", "id": f"t{j}", "name": "Read", "input": {}}),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{j}", "content": "R"},
                                         _text("<system-reminder>r</system-reminder>")]}]


def test_the_scan_stops_once_it_has_its_context(monkeypatch):
    msgs = []
    for k in range(200):                       # 1,800 messages: a prompt, an answer, 3 tool pairs, an answer
        msgs += [_user(f"ask {k}"), _assistant(_text(f"answer {k}"))]
        for j in range(3):
            msgs += _tool_pair(j)
        msgs.append(_assistant(_text(f"done {k}")))
    msgs.append(_user("the newest prompt"))
    calls = _counting(monkeypatch)
    out = cls_input.assemble({"system": SYSTEM, "messages": msgs})
    assert out.prompt == "the newest prompt" and out.context == (
        "Working directory: /work/demo\n\nEarlier user prompt 1/3:\nask 197\n\nEarlier user prompt 2/3:\nask 198"
        "\n\nEarlier user prompt 3/3:\nask 199\n\nAssistant's last message before this prompt (tail):\ndone 199")
    assert len(msgs) == 1801 and len(calls) <= 30     # 1 + (3 turns x 9 messages), not 1,801


def test_a_long_tool_loop_is_read_at_most_max_scan_messages_back(monkeypatch):
    msgs = [_user("the only prompt"), _assistant(_text("on it"))]
    for j in range(2500):                      # 5,002 messages after the only prompt
        msgs += _tool_pair(j)
    msgs.append(_user("and now this"))
    calls = _counting(monkeypatch)
    out = cls_input.assemble({"system": SYSTEM, "messages": msgs})
    assert out.prompt == "and now this"
    assert len(calls) <= cls_input.MAX_SCAN_MESSAGES + 1
    # the prompt's context lies beyond the window: say nothing rather than claim a first prompt
    assert cls_input._FIRST not in out.context and "the only prompt" not in out.context
    assert out.context == "Working directory: /work/demo" and out.capped is True


def test_capped_is_false_when_the_window_holds_the_whole_context():
    assert cls_input.assemble(_fixture()).capped is False
    msgs = [_user("an old prompt")] + [m for j in range(1000) for m in _tool_pair(j)]
    msgs += [_user(f"p{k}") if k % 2 == 0 else _assistant(_text(f"a{k}")) for k in range(8)] + [_user("newest")]
    assert cls_input.assemble({"system": SYSTEM, "messages": msgs}).capped is False   # found all within the window
