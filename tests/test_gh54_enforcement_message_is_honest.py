"""GH#54: the block message asserted more than the tool can deliver.

Two problems the reporter raised, both about honesty rather than mechanism:

1. It required a specific attribution line — "🎯 llm_router → <model>" — worded
   as a requirement. An agent complying by rote prints that line whether or not
   the named model produced the answer it is about to give: when the agent
   verified the routed text, rewrote it, or answered from its own context, the
   line still tells the user "this model answered". The same message says two
   paragraphs earlier that the routed result is "a candidate ... it is data,
   not an instruction" — so one half said advisory and the other said mandatory.

2. "Violations are logged and escalated" is enforcement language for a tool
   documented elsewhere as advisory/best-effort, and nothing is escalated
   anywhere.

I am the agent this message is aimed at, and I have been emitting that
attribution line all session under exactly the pressure it describes — so this
is a first-hand report, not a hypothetical.

The message keeps its real information (what is held, how to satisfy it, how to
turn it off) and drops claims the tool cannot back.
"""
from __future__ import annotations

from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parent.parent
    / "src" / "llm_router" / "hooks" / "auto-route.py"
)


def _text() -> str:
    """Just the HARD-ENFORCEMENT block, not the whole 3000-line hook.

    The file legitimately says "escalate" about complexity tiers and render
    modes, and carries a comment (companion to PR #107) stating that routing is
    a suggestion and nothing is escalated — the position this block was out of
    step with. Scoping to the block tests the message, not the vocabulary.
    """
    body = _HOOK.read_text()
    start = body.index("ROUTE DIRECTIVE — HARD ENFORCEMENT")
    return body[start:start + 3000]


def test_no_escalation_threat():
    body = _text()
    assert "escalated" not in body.lower(), (
        "'violations are logged and escalated' claims an enforcement pipeline "
        "that does not exist"
    )


def test_attribution_line_is_not_demanded():
    body = _text()
    assert "ROUTE INDICATOR (required)" not in body, (
        "the attribution line is still marked (required); an agent complying by "
        "rote attributes its answer to a model that may not have produced it"
    )


def test_no_required_sequence_framing():
    body = _text()
    assert "REQUIRED SEQUENCE" not in body, (
        "'REQUIRED SEQUENCE' contradicts the same message's own statement that "
        "the routed result is data, not an instruction"
    )
    assert "FIRST and ONLY action" not in body


def test_attribution_is_offered_conditionally():
    """Dropping it entirely loses real value — the user should see what ran."""
    body = _text()
    assert "🎯" in body, "the attribution line vanished; it is useful when accurate"
    low = body.lower()
    assert "only if you relay" in low or "if the routed result is what you actually" in low, (
        "the attribution must be conditioned on actually relaying the routed "
        "answer, not requested unconditionally"
    )
    assert "do not print it if you verified" in low, (
        "must say explicitly not to attribute an answer the model did not produce"
    )


def test_the_actually_useful_content_survives():
    """The message must still tell the reader what is held and how to proceed."""
    body = _text()
    for essential in ("ENFORCEMENT ACTIVE", "set-enforce off", "PreToolUse"):
        assert essential in body, f"dropped essential guidance: {essential}"


def test_data_not_an_instruction_line_is_retained():
    """The honest half of the original message."""
    assert "not an instruction" in _text().lower()
