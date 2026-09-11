"""`llm-router routing-report` — count what actually happened, once per prompt.

This exists because the ad-hoc greps used to measure routing were wrong twice, in
opposite directions, and both times the wrong number was reported as fact.

  * `grep -E "^(FAILED|ERROR)"` silently matched nothing once pytest colourised its
    output, so a real test failure read as a green suite.
  * counting `OUTPUT COMPLETE` as "prompts" undercounted the denominator, because a
    DIRECT SUCCESS invocation never emits one — it answers and stops. Success and
    prompts were disjoint sets, which is how a report claimed 34 successes out of
    33 prompts.

The fix is structural rather than a better regex: parse the log into one record per
invocation and give each exactly one outcome. A count that cannot exceed its own
denominator is a count that cannot produce that headline again.

Excluded from the denominator: invocations with no session id (the test suite runs
the hook, and 227 such rows once polluted a production log) and `session_id=unknown`
(the hook ran but could not resolve a session, so it is not a user prompt either).
"""
from __future__ import annotations

import pytest

from llm_router.routing_report import Outcome, parse_log, summarise


def _log(*lines: str) -> list[str]:
    return [ln + "\n" for ln in lines]


REAL = _log(
    "[2026-09-10 20:15:00] [INVOCATION START] ID=1.0",
    "[2026-09-10 20:15:00] [INVOCATION 1.0] prompt_len=12 session_id=da8c385a",
    "[2026-09-10 20:15:00] [INVOCATION 1.0] DIRECT: zone=green chain=['ollama/x']",
    "[2026-09-10 20:15:06] [INVOCATION 1.0] DIRECT SUCCESS: model=ollama/x latency=6294ms",
)


def test_a_direct_success_counts_as_one_prompt_and_one_success():
    """The bug that produced 34 successes from 33 prompts.

    A successful DIRECT invocation answers and stops, so it never reaches
    OUTPUT COMPLETE. Counting that line as "a prompt" made success and prompts
    disjoint.
    """
    days = summarise(parse_log(REAL))
    d = days["2026-09-10"]
    assert d["prompts"] == 1
    assert d["success"] == 1


def test_every_outcome_sums_to_the_prompt_count():
    """The structural guarantee: one invocation, one outcome."""
    lines = REAL + _log(
        "[2026-09-10 20:16:00] [INVOCATION START] ID=2.0",
        "[2026-09-10 20:16:00] [INVOCATION 2.0] prompt_len=9 session_id=da8c385a",
        "[2026-09-10 20:16:00] [INVOCATION 2.0] DIRECT SKIP: context-dependent prompt",
        "[2026-09-10 20:16:00] [INVOCATION 2.0] OUTPUT COMPLETE",
        "[2026-09-10 20:17:00] [INVOCATION START] ID=3.0",
        "[2026-09-10 20:17:00] [INVOCATION 3.0] prompt_len=9 session_id=da8c385a",
        "[2026-09-10 20:17:04] [INVOCATION 3.0] DIRECT FAILED: falling through to Claude",
        "[2026-09-10 20:17:04] [INVOCATION 3.0] OUTPUT COMPLETE",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["prompts"] == 3
    assert d["success"] + d["failed"] + d["skipped"] + d["other"] == d["prompts"]


def test_test_suite_invocations_are_excluded():
    """227 rows with an empty session id once polluted the production log."""
    lines = REAL + _log(
        "[2026-09-10 20:18:00] [INVOCATION START] ID=4.0",
        "[2026-09-10 20:18:00] [INVOCATION 4.0] prompt_len=5 session_id=",
        "[2026-09-10 20:18:00] [INVOCATION 4.0] DIRECT SUCCESS: model=ollama/fake-model",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["prompts"] == 1, "a test-suite invocation was counted as a user prompt"


def test_unknown_sessions_are_excluded():
    """The hook ran but could not resolve a session — not a user prompt either."""
    lines = REAL + _log(
        "[2026-09-10 20:19:00] [INVOCATION START] ID=5.0",
        "[2026-09-10 20:19:00] [INVOCATION 5.0] prompt_len=7 session_id=unknown",
        "[2026-09-10 20:19:00] [INVOCATION 5.0] DIRECT SUCCESS: model=ollama/x",
    )
    assert summarise(parse_log(lines))["2026-09-10"]["prompts"] == 1


def test_rescues_are_reported_but_are_not_outcomes():
    """A rescue changes WHY something routed, not whether it did. Counting it as an
    outcome would double-count the invocation it belongs to."""
    lines = _log(
        "[2026-09-10 21:00:00] [INVOCATION START] ID=9.0",
        "[2026-09-10 21:00:00] [INVOCATION 9.0] prompt_len=30 session_id=abc",
        "[2026-09-10 21:00:00] [INVOCATION 9.0] SESSION RESCUE: 900 chars of conversation",
        "[2026-09-10 21:00:05] [INVOCATION 9.0] DIRECT SUCCESS: model=ollama/x",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["prompts"] == 1
    assert d["success"] == 1
    assert d["session_rescue"] == 1
    assert d["success"] + d["failed"] + d["skipped"] + d["other"] == d["prompts"]


def test_a_rejected_draft_is_not_a_success():
    """S2-6 discards an ungrounded draft and falls through — that is not routing."""
    lines = _log(
        "[2026-09-10 22:00:00] [INVOCATION START] ID=11.0",
        "[2026-09-10 22:00:00] [INVOCATION 11.0] prompt_len=30 session_id=abc",
        "[2026-09-10 22:00:03] [INVOCATION 11.0] DRAFT REJECTED (ungrounded): cites x.py",
        "[2026-09-10 22:00:03] [INVOCATION 11.0] OUTPUT COMPLETE",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["success"] == 0
    assert d["rejected"] == 1


def test_the_routed_rate_is_successes_over_prompts():
    lines = REAL + _log(
        "[2026-09-10 20:20:00] [INVOCATION START] ID=6.0",
        "[2026-09-10 20:20:00] [INVOCATION 6.0] prompt_len=9 session_id=abc",
        "[2026-09-10 20:20:00] [INVOCATION 6.0] DIRECT SKIP: context-dependent prompt",
        "[2026-09-10 20:20:00] [INVOCATION 6.0] OUTPUT COMPLETE",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["rate"] == pytest.approx(0.5)


def test_days_are_reported_separately():
    lines = REAL + _log(
        "[2026-09-11 09:00:00] [INVOCATION START] ID=7.0",
        "[2026-09-11 09:00:00] [INVOCATION 7.0] prompt_len=9 session_id=abc",
        "[2026-09-11 09:00:00] [INVOCATION 7.0] DIRECT SKIP: context-dependent prompt",
        "[2026-09-11 09:00:00] [INVOCATION 7.0] OUTPUT COMPLETE",
    )
    days = summarise(parse_log(lines))
    assert set(days) == {"2026-09-10", "2026-09-11"}
    assert days["2026-09-11"]["success"] == 0


def test_an_invocation_with_no_terminal_line_is_counted_as_other():
    """A crashed or truncated invocation must still appear in the denominator,
    or the rate flatters itself by dropping the cases that went wrong."""
    lines = _log(
        "[2026-09-10 23:00:00] [INVOCATION START] ID=12.0",
        "[2026-09-10 23:00:00] [INVOCATION 12.0] prompt_len=9 session_id=abc",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["prompts"] == 1
    assert d["other"] == 1


def test_a_truncated_or_garbled_line_does_not_crash():
    assert parse_log(_log("not a log line at all", "", "[bad")) == {}


def test_outcome_is_assigned_once_even_if_lines_repeat():
    """Log lines can repeat on retry; the outcome must not."""
    lines = _log(
        "[2026-09-10 20:30:00] [INVOCATION START] ID=8.0",
        "[2026-09-10 20:30:00] [INVOCATION 8.0] prompt_len=9 session_id=abc",
        "[2026-09-10 20:30:00] [INVOCATION 8.0] DIRECT SUCCESS: model=ollama/x",
        "[2026-09-10 20:30:00] [INVOCATION 8.0] DIRECT SUCCESS: model=ollama/x",
    )
    d = summarise(parse_log(lines))["2026-09-10"]
    assert d["prompts"] == 1
    assert d["success"] == 1


def test_outcome_enum_covers_what_the_hook_emits():
    assert {o.value for o in Outcome} >= {"success", "failed", "skipped", "other"}
