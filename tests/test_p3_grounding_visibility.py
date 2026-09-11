"""Phase 3 — grounding is the product, so it has to be visible.

Today the check is an internal safety valve. It discards a fabricated draft, writes
one line to a debug log nobody reads, and falls through to the expensive model. From
the outside that is indistinguishable from a cheap model that simply did not answer
— which is exactly how a false-positive guard hides, and why the symbol check ran at
a 3-in-4 false-rejection rate for a while without anyone noticing.

Two changes, both small, both about the same thing: the one capability no competitor
has should be something you can see and count.

  (a) `routing-report` reports grounding beside routing. A rejection is a
      fabrication that did NOT reach you, and that number is the product's entire
      argument.
  (b) The hook says so at the time, rather than only in a log.

Deliberately NOT a quality claim. A rejection means a draft cited something that
does not exist; it says nothing about whether the answers that passed were good.
That is phase 3(c)'s job, and conflating the two would be the third bad instrument
in a week.
"""
from __future__ import annotations

import pytest

from llm_router.routing_report import parse_log, summarise


def _log(*lines: str) -> list[str]:
    return [ln + "\n" for ln in lines]


ROUTED = _log(
    "[2026-09-11 10:00:00] [INVOCATION START] ID=1.0",
    "[2026-09-11 10:00:00] [INVOCATION 1.0] prompt_len=20 session_id=abc",
    "[2026-09-11 10:00:05] [INVOCATION 1.0] DIRECT SUCCESS: model=ollama/x",
)
REJECTED = _log(
    "[2026-09-11 10:01:00] [INVOCATION START] ID=2.0",
    "[2026-09-11 10:01:00] [INVOCATION 2.0] prompt_len=20 session_id=abc",
    "[2026-09-11 10:01:03] [INVOCATION 2.0] DRAFT REJECTED (ungrounded): cites nope.py",
    "[2026-09-11 10:01:03] [INVOCATION 2.0] OUTPUT COMPLETE",
)


def test_rejections_are_counted():
    d = summarise(parse_log(ROUTED + REJECTED))["2026-09-11"]
    assert d["rejected"] == 1


def test_the_grounding_rate_is_rejections_over_drafts_produced():
    """Not over all prompts. A prompt the gate never routed produced no draft, so it
    was never a chance for the check to fire — including it would dilute the rate
    with cases grounding had no part in."""
    d = summarise(parse_log(ROUTED + REJECTED))["2026-09-11"]
    assert d["drafts"] == 2, "one relayed, one rejected"
    assert d["grounding_rate"] == pytest.approx(0.5)


def test_a_day_with_no_drafts_has_no_grounding_rate():
    """Dividing by zero drafts must not read as a perfect record."""
    skipped = _log(
        "[2026-09-11 11:00:00] [INVOCATION START] ID=3.0",
        "[2026-09-11 11:00:00] [INVOCATION 3.0] prompt_len=9 session_id=abc",
        "[2026-09-11 11:00:00] [INVOCATION 3.0] DIRECT SKIP: context-dependent prompt",
        "[2026-09-11 11:00:00] [INVOCATION 3.0] OUTPUT COMPLETE",
    )
    d = summarise(parse_log(skipped))["2026-09-11"]
    assert d["drafts"] == 0
    assert d["grounding_rate"] is None


def test_a_rejected_draft_is_still_not_a_success():
    """The invariant from the counting fix must survive the new columns."""
    d = summarise(parse_log(ROUTED + REJECTED))["2026-09-11"]
    assert d["success"] == 1
    assert d["success"] + d["failed"] + d["skipped"] + d["other"] == d["prompts"]


# ── 3(b): the rejection reaches the user, not just the log ──────────────────

HOOK = __import__("pathlib").Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


def test_a_rejection_produces_a_user_visible_notice():
    src = HOOK.read_text(encoding="utf-8")
    assert "_grounding_notice" in src
    assert "A local draft was discarded" in src, (
        "the rejection is still only written to the debug log"
    )


def test_the_notice_names_what_was_cited_and_how_to_turn_it_off():
    """A guard the user cannot see is a guard they cannot judge, and one they
    cannot disable is one they will work around."""
    src = HOOK.read_text(encoding="utf-8")
    i = src.index("A local draft was discarded")
    window = src[i:i + 600]
    assert "{_cited}" in window
    assert "LLM_ROUTER_GROUNDING_CHECK=off" in window


def test_the_notice_is_initialised_before_it_is_read():
    """It is set deep inside the DIRECT path and read near the end of main().

    If the initialisation ever moves inside a conditional, every prompt that does
    not reject a draft raises UnboundLocalError — which is every prompt.
    """
    src = HOOK.read_text(encoding="utf-8")
    init = src.index('    _grounding_notice = ""')
    read = src.index("if _grounding_notice:")
    assert init < read
    line = src[src.rindex("\n", 0, init) + 1:init + 30]
    assert line.startswith("    _grounding_notice"), (
        f"initialisation is nested, not at function level: {line!r}"
    )
