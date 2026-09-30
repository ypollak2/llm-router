"""Escalation signals (proxy/escalation.py) and their wiring into
ClaudeTierPolicy.decide() (proxy/tiers.py): the `opus:` explicit pin, the
automatic correction-signal escalation, and the long/multi-part first-prompt
safety floor.

Motivated by ~/.rsi/research/llm-router-cursor-parity/trial-real-prompts.md
(n=6, 2026-09-30): every conversation landed on Sonnet, and the one
unacceptable answer (t7-northstar) was a moderate-classified but
investigation-heavy first prompt. `test_long_first_prompt_never_rewritten`
paraphrases that failure as a regression test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router.proxy import escalation as esc
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
SID = "11111111-2222-3333-4444-555555555555"
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"


def _req(model=OPUS, thinking="adaptive") -> dict:
    body = json.loads(FIXTURE.read_text())
    body["model"] = model
    if thinking is None:
        body.pop("thinking", None)
    else:
        body["thinking"] = {"type": thinking}
    return body


def _first(model=OPUS, thinking="adaptive", text="Fix the bug in pkg/mod01.py.") -> dict:
    body = _req(model, thinking)
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    return body


def _with_human_turn(text: str, model=OPUS) -> dict:
    """The fixture's tool-loop conversation with one more human turn appended
    -- a re-ask/correction after the assistant already answered once."""
    body = _req(model)
    body["messages"] = body["messages"] + [
        {"role": "user", "content": [{"type": "text", "text": text}]}
    ]
    return body


def _conversation(*human_texts: str, model=OPUS) -> dict:
    """A synthetic multi-turn conversation of plain human/assistant text
    turns. `conversation_key` hashes only `messages[0]` (proxy/cache_cost.py),
    so a sequence of decide() calls meant to share ONE conversation's
    stickiness state must all start from the exact same first turn -- this
    builds each call's body from a shared, growing turn list rather than
    independently re-deriving the first turn (which `_with_human_turn` does,
    and which silently starts a NEW conversation each time it's called)."""
    body = _req(model)
    msgs: list[dict] = []
    for i, text in enumerate(human_texts):
        if i > 0:
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": text}]})
    body["messages"] = msgs
    return body


def _with_failed_tool_turns(n: int, model=OPUS) -> dict:
    """The fixture's first two turns (one ask, one tool call) followed by `n`
    consecutive tool_result turns that all report `is_error`."""
    body = _req(model)
    body["messages"] = body["messages"][:3] + [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_err_{i}",
                 "content": "Error: command failed", "is_error": True}
            ],
        }
        for i in range(n)
    ]
    return body


def _classify(cx="moderate", task="code"):
    async def fn(text):
        return {"task_type": task, "complexity": cx, "chain_head": ["ollama/x"], "model": None}
    return fn


def _raw_policy() -> dict:
    import yaml

    return yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())


@pytest.fixture
def conv_policy():
    """Conversation-level mode with the post-first-call handoff on --
    matches production (`--tiers conversation`, LaunchAgent installed by
    `commands/proxy_default.py`) and lets most tests reach the policy tier
    on the first classified call."""
    raw = _raw_policy()
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True)


# ── escalation.py: pure functions ───────────────────────────────────────────

def test_explicit_opus_pin_matches_only_the_literal_prefix():
    assert esc.explicit_opus_pin(_first(text="opus: investigate the outage")) is True
    assert esc.explicit_opus_pin(_first(text="OPUS:  investigate")) is True
    assert esc.explicit_opus_pin(_first(text="opus please look into this")) is False
    assert esc.explicit_opus_pin(_first(text="use opus: as a delimiter here")) is False


def test_opus_pin_never_fires_on_the_claude_prefix_convention():
    """`claude:` is the owner's own convention (task brief) and must never be
    treated as an opus pin -- it is a correction SIGNAL (below), and even that
    only fires on turns after the first."""
    assert esc.explicit_opus_pin(_first(text="claude: what's the status?")) is False


def test_correction_signal_detects_claude_reask():
    body = _with_human_turn("claude: that's not right, redo it")
    assert esc.correction_signal(body) == esc.REASON_CLAUDE_REASK


@pytest.mark.parametrize("text", [
    "No, that's not what I asked.",
    "no, try again",
    "That's wrong — the function name is different.",
    "you missed the second file",
    "Not what I meant, please redo this.",
    "Incorrect. Read the whole file first.",
])
def test_correction_signal_detects_contradiction_openers(text):
    assert esc.correction_signal(_with_human_turn(text)) == esc.REASON_CONTRADICTION


def test_correction_signal_ignores_the_word_no_mid_sentence():
    """A prompt that legitimately contains "no"/"wrong" mid-text (a normal
    code-review ask) must not be misread as a correction -- only an OPENING
    contradiction counts."""
    body = _with_human_turn("Please check whether there is no off-by-one error here.")
    assert esc.correction_signal(body) is None


def test_correction_signal_detects_repeated_tool_failures():
    assert esc.correction_signal(_with_failed_tool_turns(1)) is None
    assert esc.correction_signal(_with_failed_tool_turns(2)) == esc.REASON_TOOL_FAILURES
    assert esc.correction_signal(_with_failed_tool_turns(5)) == esc.REASON_TOOL_FAILURES


def test_correction_signal_tool_failure_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_ESCALATION_TOOL_FAIL_N", "3")
    assert esc.correction_signal(_with_failed_tool_turns(2)) is None
    assert esc.correction_signal(_with_failed_tool_turns(3)) == esc.REASON_TOOL_FAILURES


def test_correction_signal_none_on_a_clean_turn():
    assert esc.correction_signal(_with_human_turn("Now add a docstring too.")) is None


def test_is_long_or_multi_part_by_word_count():
    short = "Fix the bug in mod01.py."
    long = " ".join(["word"] * 150)
    assert esc.is_long_or_multi_part(short) is False
    assert esc.is_long_or_multi_part(long) is True


def test_is_long_or_multi_part_by_bullet_count():
    text = "Please do the following:\n- one\n- two\n- three\n"
    assert esc.is_long_or_multi_part(text) is True


def test_long_prompt_thresholds_are_configurable(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LONG_PROMPT_WORDS", "5")
    assert esc.is_long_or_multi_part("one two three four five six") is True


# ── wired into ClaudeTierPolicy.decide() ────────────────────────────────────

async def test_opus_prefix_pins_the_first_call_to_opus(conv_policy):
    sticky = Stickiness()
    body = _first(model=SONNET, text="opus: do a full audit of the routing pipeline")
    d = await conv_policy.decide(body, SID, sticky, classify=_classify("simple"))
    assert (d.reason, d.served_model, d.tier) == (pt.REASON_EXPLICIT_OPUS_PIN, OPUS, "opus")


async def test_opus_prefix_pin_survives_into_the_next_turn(conv_policy):
    sticky = Stickiness()
    first = _conversation("opus: audit this", model=OPUS)
    await conv_policy.decide(first, SID, sticky, classify=_classify("simple"))
    # A later turn the classifier would send to haiku must still stay on opus.
    later = _conversation("opus: audit this", "what about the other file?", model=OPUS)
    d = await conv_policy.decide(later, SID, sticky, classify=_classify("simple"))
    assert d.served_model == OPUS


async def test_correction_signal_escalates_over_a_committed_sticky_tier(conv_policy):
    sticky = Stickiness()
    ask = "Refactor this module."
    first = _conversation(ask, model=OPUS)
    d1 = await conv_policy.decide(first, SID, sticky, classify=_classify("moderate"))
    assert d1.served_model == SONNET  # committed to sonnet via the policy

    # Without a correction signal, the same class stays on sonnet.
    quiet = _conversation(ask, "Also update the docstring.", model=OPUS)
    d_quiet = await conv_policy.decide(quiet, SID, sticky, classify=_classify("moderate"))
    assert d_quiet.served_model == SONNET

    # A contradiction on the same classified complexity must still escalate --
    # this is the case the class_changed override in decide() exists for.
    correction = _conversation(
        ask, "Also update the docstring.", "No, that's wrong. You changed the wrong function.", model=OPUS,
    )
    d2 = await conv_policy.decide(correction, SID, sticky, classify=_classify("moderate"))
    assert (d2.reason, d2.served_model, d2.detail) == (pt.REASON_ESCALATION, OPUS, esc.REASON_CONTRADICTION)

    # And it holds for the turn after that (stickiness).
    after = _conversation(
        ask, "Also update the docstring.", "No, that's wrong. You changed the wrong function.",
        "ok now add tests.", model=OPUS,
    )
    d3 = await conv_policy.decide(after, SID, sticky, classify=_classify("moderate"))
    assert d3.served_model == OPUS


async def test_long_first_prompt_never_rewritten(conv_policy):
    """Paraphrase of the trial's t7-northstar failure: a long, multi-part
    investigation brief that the classifier would call "moderate" (-> sonnet)
    must instead keep whatever model Claude Code itself requested."""
    brief = (
        "Inspect this repository end to end and write a North Star document. "
        "Cover the following:\n"
        "1. What problem the project actually solves today, versus what it claims to.\n"
        "2. Which metrics are instrumented and which are aspirational.\n"
        "3. The three biggest gaps between the roadmap and the current state.\n"
        "4. A recommended sequencing for the next two quarters.\n"
        "Read the docs, the changelog and the test suite before answering; "
        "do not guess at anything you have not verified in the repo."
    )
    body = _first(model=OPUS, text=brief)
    sticky = Stickiness()
    d = await conv_policy.decide(body, SID, sticky, classify=_classify("moderate"))
    assert (d.reason, d.served_model) == (pt.REASON_LONG_FIRST_PROMPT, OPUS)


async def test_short_first_prompt_is_still_classified_normally(conv_policy):
    """The long-prompt floor must not swallow ordinary first calls. (Served
    model floors at Sonnet, not Haiku, because the fixture's own thinking
    config is adaptive+effort, which Haiku 4.5 accepts neither of -- see
    tiers.py's REASON_THINKING_FLOOR; that floor is unrelated to this test.)"""
    body = _first(model=OPUS, text="What does os.path.join do?")
    sticky = Stickiness()
    d = await conv_policy.decide(body, SID, sticky, classify=_classify("simple"))
    assert d.reason != pt.REASON_LONG_FIRST_PROMPT
    assert d.served_model == SONNET and d.reason == pt.REASON_THINKING_FLOOR


async def test_explicit_model_pin_still_wins_over_everything_including_opus_text(conv_policy):
    """Safety default: a request naming a model that is not a configured tier
    (e.g. a raw `--model` pin the policy doesn't recognise) is never touched,
    even if the newest human text also happens to start with `opus:`."""
    body = _first(model="some-custom-fine-tune", text="opus: please look into this")
    sticky = Stickiness()
    d = await conv_policy.decide(body, SID, sticky, classify=_classify("moderate"))
    assert d.reason == pt.REASON_UNKNOWN_MODEL and d.served_model == "some-custom-fine-tune"


async def test_user_pinned_slash_model_beats_correction_signal_escalation(conv_policy):
    """/model is the user's own explicit, in-band choice; the automatic
    correction-signal path must not override it (only the explicit `opus:`
    text pin -- a stronger, later signal -- is allowed to)."""
    body = _with_human_turn("no, that's wrong, redo it", model=OPUS)
    for m in body["messages"]:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            m["content"] = [{"type": "text", "text": "<command-name>/model</command-name>"}]
            break
    sticky = Stickiness()
    d = await conv_policy.decide(body, SID, sticky, classify=_classify("moderate"))
    assert d.reason == pt.REASON_USER_PINNED
