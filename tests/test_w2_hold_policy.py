"""BIND W2: which prompts may hold a tool, and what releases a hold.

Fixtures are synthetic. They have the SHAPE of the prompts and probe calls the audit
found (acknowledgements, continuations, a compaction summary, "Reply OK" probes, a
paraphrased forward), and carry no text from any real session.
"""
from __future__ import annotations

import pytest

from llm_router import hold_policy as hp

OWNER = (
    "Explain the difference between a mutex and a semaphore, with a short example in "
    "Python, and say when each one is the right tool please"
)

ACK_AND_CONTINUATION_PROMPTS = [
    "ok",
    "done",
    "keep going",
    "keep going and do not wait for me",
    "continue the plan",
    "deploy main now",
    "any news on the build?",
    "Yeah, I restarted the service",
    "wait for the first job to finish",
    "update the progress of the plan please, thanks",
    "you can have a couple of agents to start",
]


@pytest.mark.parametrize("prompt", ACK_AND_CONTINUATION_PROMPTS)
def test_short_prompts_never_hold(prompt):
    assert hp.words_in(prompt) <= hp.SHORT_PROMPT_MAX_WORDS
    assert hp.hold_exempt_reason(prompt) == hp.REASON_SHORT_PROMPT


def test_the_boundary_is_ten_words():
    ten = " ".join(["word"] * hp.SHORT_PROMPT_MAX_WORDS)
    eleven = " ".join(["word"] * (hp.SHORT_PROMPT_MAX_WORDS + 1))
    assert hp.SHORT_PROMPT_MAX_WORDS == 10
    assert hp.hold_exempt_reason(ten) == hp.REASON_SHORT_PROMPT
    assert hp.hold_exempt_reason(eleven) is None


def test_a_real_request_still_holds():
    assert hp.words_in(OWNER) > hp.SHORT_PROMPT_MAX_WORDS
    assert hp.hold_exempt_reason(OWNER) is None


@pytest.mark.parametrize("prompt", [
    "This session is being continued from a previous conversation that ran out of context. "
    + "Summary of the earlier work. " * 300,
    "  this session is being continued from a previous conversation. " + "filler " * 50,
])
def test_a_compaction_summary_is_not_a_request(prompt):
    assert hp.words_in(prompt) > hp.SHORT_PROMPT_MAX_WORDS
    assert hp.hold_exempt_reason(prompt) == hp.REASON_SESSION_CONTINUATION


@pytest.mark.parametrize("prompt", [None, "", "   \n\t "])
def test_no_prompt_is_no_evidence_so_not_exempt(prompt):
    """A pending file from an older hook has no original_prompt: keep today's behaviour."""
    assert hp.hold_exempt_reason(prompt) is None


def test_the_continuation_marker_is_only_read_at_the_start():
    mid = "Please explain this line from a log file: This session is being continued from a previous conversation"
    assert hp.hold_exempt_reason(mid) is None


# ── what releases a hold ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", [
    "llm", "mcp__llm_router__llm", "mcp__llm_router__llm_act", "llm_route",
    "mcp__llm_router__llm_edit", "mcp__llm_router__llm_local_task",
    "mcp__llm_router__llm_text_job", "llm_query", "llm_code", "llm_router_agent_route",
])
def test_routing_doors_can_carry_a_request(tool):
    assert hp.is_route_door(tool)


@pytest.mark.parametrize("tool", [
    "llm_router_status", "mcp__llm_router__llm_router_status", "llm_router_admin",
    "llm_router_session", "llm_savings", "llm_usage", "llm_health", "llm_router_agent_start_session",
    "Bash", "Edit", "mcp__other__llm_thing", "", None,
])
def test_status_admin_and_native_tools_never_carry_one(tool):
    assert not hp.is_route_door(tool)


def test_a_verbatim_forward_releases():
    assert hp.forwards_owner_prompt(OWNER, {"prompt": OWNER, "task": "query"}) is True


def test_a_paraphrased_forward_releases():
    paraphrase = {"prompt": "Explain mutex vs semaphore: the difference, when to use each, "
                            "with a short python example", "task": "query", "tier": "fast"}
    assert hp.forwards_owner_prompt(OWNER, paraphrase) is True


def test_a_quoted_excerpt_of_a_long_prompt_releases():
    long_owner = OWNER + " " + " ".join(f"detail{i} aspect{i} factor{i}" for i in range(40))
    excerpt = " ".join(OWNER.split()[:14])
    assert hp.forwards_owner_prompt(long_owner, {"prompt": excerpt}) is True


@pytest.mark.parametrize("probe", [
    {"prompt": "Reply OK", "task": "query", "tier": "fast"},
    {"prompt": "Reply with the single word: ready"},
    {"prompt": "Reply with one word: acknowledged", "task": "auto", "tier": "fast"},
    {"prompt": "What is the capital of France?"},
    {"prompt": "Is reading git file versions a content-generation task? yes or no"},
    {"prompt": ""},
    {},
    None,
])
def test_a_toll_probe_does_not_release(probe):
    assert hp.forwards_owner_prompt(OWNER, probe) is False


def test_dragging_the_owners_text_along_as_context_is_not_a_forward():
    """A probe with the request attached as material still says only 'Reply OK'."""
    probe = {"prompt": "Reply OK", "context": OWNER, "system_prompt": OWNER}
    assert hp.forwards_owner_prompt(OWNER, probe) is False


def test_other_doors_state_the_request_under_other_keys():
    assert hp.forwards_owner_prompt(OWNER, {"task": OWNER, "files": ["a.py"]}) is True       # llm_edit / llm_act
    assert hp.forwards_owner_prompt(OWNER, {"objective": OWNER, "workdir": "/tmp/x"}) is True  # llm_local_task
    assert hp.forwards_owner_prompt(OWNER, {"job": "commit_message", "input_text": OWNER}) is True
    assert hp.forwards_owner_prompt(OWNER, {"job": "commit_message", "input_text": "diff --git"}) is False


def test_an_unknown_door_schema_is_read_without_its_material_keys():
    assert hp.forwards_owner_prompt(OWNER, {"payload": OWNER}) is True
    assert hp.forwards_owner_prompt(OWNER, {"context": OWNER}) is False


def test_a_string_input_is_read_as_the_request():
    assert hp.forwards_owner_prompt(OWNER, OWNER) is True
    assert hp.forwards_owner_prompt(OWNER, "Reply OK") is False


@pytest.mark.parametrize("owner", [None, "", "go on", "do it now please", "12345 67890"])
def test_nothing_to_compare_is_unverifiable_not_false(owner):
    """Fewer than three content words: the caller releases, as before, rather than hold forever."""
    assert hp.forwards_owner_prompt(owner, {"prompt": "Reply OK"}) is None


def test_hebrew_prompts_are_compared_too():
    owner = "תסביר לי את ההבדל בין תהליך לתהליכון ותן דוגמה קצרה בפייתון בבקשה"
    assert hp.forwards_owner_prompt(owner, {"prompt": owner}) is True
    assert hp.forwards_owner_prompt(owner, {"prompt": "Reply OK"}) is False


def test_content_words_drop_function_words_numbers_and_short_words():
    assert hp.content_words("Reply with the 42 words: ok!") == frozenset({"reply", "words"})
    assert hp.content_words(None) == frozenset()


def test_the_call_text_is_bounded():
    big = {"prompt": "x" * 1_000_000}
    assert len(hp.call_text(big)) <= 20_000


def test_task_is_a_category_for_llm_and_the_request_for_llm_act():
    """llm(prompt, task="query") states its request in prompt; llm_act(task=...) in task."""
    assert hp.call_text({"prompt": "Reply OK", "task": "query", "tier": "fast"}) == "Reply OK"
    assert hp.call_text({"prompt": "", "task": "query"}) == ""
    assert hp.call_text({"task": "Run the migration", "context": "ignored"}) == "Run the migration"
    assert hp.call_text({"task": "query"}) == "query"
    assert hp.call_text({"objective": "Rename x", "workdir": "/w", "task": "ignored"}) == "Rename x"
