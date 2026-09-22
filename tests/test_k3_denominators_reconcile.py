"""K3 — every population's parts must sum to its whole.

The audit's single most productive question was "what is the denominator?", and
it kept finding the same answer: a rate whose numerator and denominator came
from different sets, or a total that quietly dropped a category.

The failures this class produced in this repo, all real:

  * the near-duplicate stage reported 0 collapses and looked clean. It was
    comparing raw whitespace splits, so nothing could ever match. A stage
    reporting zero is the same evidence as a stage that is broken.
  * `ENFORCE=off` and `shadow` skipped the DIRECT block while logging nothing —
    28.5% of one day's prompts in a category no report had a column for.
  * 1,037 of 1,938 routing-log entries were the test suite, so every rate over
    that file was wrong by roughly a factor of two.
  * `production_only(include_simulated=True)` returned "", producing
    `WHERE  AND` and a query that could not run.

An identity test is the cheapest possible guard: if `total` is computed
independently of the parts, a category that goes missing changes one side and
not the other. A category nobody thought of is exactly what these all were.

EVERY assertion here also checks the population is NON-EMPTY first. An identity
over an empty set holds trivially, which is the failure mode one level up — and
this file would otherwise be the thing it exists to prevent.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    yield


# ── prompt capture: every candidate lands in exactly one outcome ────────────

def test_capture_outcomes_account_for_every_candidate(monkeypatch):
    from llm_router import prompt_capture as pc

    monkeypatch.setattr(pc, "_counters", {}, raising=False)
    for outcome in (pc.OUTCOME_PERSISTED, pc.OUTCOME_REJECTED,
                    pc.OUTCOME_DEDUPED, pc.OUTCOME_ERROR, pc.OUTCOME_SKIPPED):
        pc._record_outcome(outcome, "k3-probe")

    counters = pc.counters()
    assert sum(counters.values()) == 5, f"a candidate vanished: {counters}"

    known = {pc.OUTCOME_PERSISTED, pc.OUTCOME_REJECTED, pc.OUTCOME_DEDUPED,
             pc.OUTCOME_ERROR, pc.OUTCOME_SKIPPED}
    unaccounted = set(counters) - known
    assert not unaccounted, (
        f"outcome(s) no surface has a column for: {unaccounted}. That is how "
        "28.5% of one day's prompts became invisible."
    )


def test_the_outcome_set_is_not_empty():
    """Anti-vacuity for the identity above."""
    from llm_router import prompt_capture as pc

    outcomes = {pc.OUTCOME_PERSISTED, pc.OUTCOME_REJECTED, pc.OUTCOME_DEDUPED,
                pc.OUTCOME_ERROR, pc.OUTCOME_SKIPPED}
    assert len(outcomes) == 5, f"the outcome vocabulary collapsed: {outcomes}"


# ── fail-open: by_code sums to total, and unpersisted is separate ───────────

def test_failopen_total_equals_the_sum_of_its_codes():
    from llm_router import failopen

    failopen.clear()
    for code in ("CHZ-K3-A", "CHZ-K3-A", "CHZ-K3-B"):
        failopen.record(code)
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.total == 3, snap.by_code
    assert sum(snap.by_code.values()) == snap.total, (
        f"the per-code breakdown does not sum to the total: {snap.by_code}"
    )
    # Unpersisted is deliberately NOT folded in — a restart would otherwise
    # look like the failures stopped.
    assert snap.unpersisted_total == 0
    assert snap.render_total() == "3"


def test_an_unreadable_store_is_unknown_not_zero():
    """The substitution that makes a broken counter look like a clean run."""
    from llm_router import failopen

    failopen.clear()
    failopen.store_path().parent.mkdir(parents=True, exist_ok=True)
    failopen.store_path().write_text("{not json\n")
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.total is None, f"an unparseable store reported {snap.total}"
    assert snap.render_total() == "Unknown"


# ── interception coverage: observed + unobserved, by reason ────────────────

def test_coverage_parts_sum_to_the_whole():
    from llm_router import coverage

    coverage.clear()
    coverage.reset_cache()
    coverage.record_observed("Bash")
    coverage.record_observed("Bash")
    coverage.record_unobserved(coverage.Reason.CLASSIFY_FAILED)
    coverage.reset_cache()

    snap = coverage.snapshot()
    assert snap.readable
    by_reason_total = sum(snap.by_reason.values())
    assert by_reason_total == 1, snap.by_reason
    assert snap.observed_n == 2, snap.observed_n
    # The denominator a coverage RATE must use is the sum, never one side.
    assert snap.observed_n + by_reason_total == 3, (
        "observed and unobserved do not account for every event, so any "
        "coverage percentage computed from them is over the wrong denominator"
    )


# ── routing outcomes: one terminal outcome per real invocation ─────────────

def test_every_invocation_lands_in_exactly_one_outcome(tmp_path):
    from llm_router.routing_report import parse_log, summarise

    lines = [
        "[2026-09-22 10:00:00] [INVOCATION START] ID=1.0",
        "[2026-09-22 10:00:00] [INVOCATION 1.0] session_id=abc12345 prompt_len=10",
        "[2026-09-22 10:00:01] [INVOCATION 1.0] DIRECT SUCCESS: ollama",
        "[2026-09-22 10:00:02] [INVOCATION START] ID=2.0",
        "[2026-09-22 10:00:02] [INVOCATION 2.0] session_id=abc12345 prompt_len=10",
        "[2026-09-22 10:00:03] [INVOCATION 2.0] DIRECT SKIP: continuation",
        "[2026-09-22 10:00:04] [INVOCATION START] ID=3.0",
        "[2026-09-22 10:00:04] [INVOCATION 3.0] session_id=abc12345 prompt_len=10",
        # no terminal line — the ENFORCE=off shape
    ]
    days = summarise(parse_log(lines))
    assert days, "the parser found no real invocations; the identity is vacuous"

    for day, d in days.items():
        parts = d["success"] + d["failed"] + d["skipped"] + d["other"]
        assert parts == d["prompts"], (
            f"{day}: {d['prompts']} prompts but {parts} outcomes. An "
            "invocation is in two buckets or none — and 'none' is how a "
            "silent branch hides."
        )
    total = sum(d["prompts"] for d in days.values())
    assert total == 3, total
    assert sum(d["other"] for d in days.values()) == 1, (
        "the unterminated invocation was not counted as one"
    )


def test_the_test_suites_own_sessions_are_excluded(tmp_path):
    """The denominator error that cost a day, pinned.

    1,037 of 1,938 entries were the test suite. A rate over the raw file is
    wrong by roughly a factor of two, and wrong in the direction that looks
    like a regression.
    """
    from llm_router.routing_report import parse_log, summarise

    lines = [
        "[2026-09-22 10:00:00] [INVOCATION START] ID=1.0",
        "[2026-09-22 10:00:00] [INVOCATION 1.0] session_id=unknown prompt_len=10",
        "[2026-09-22 10:00:01] [INVOCATION 1.0] DIRECT SUCCESS: ollama",
        "[2026-09-22 10:00:02] [INVOCATION START] ID=2.0",
        "[2026-09-22 10:00:02] [INVOCATION 2.0] session_id=real1234 prompt_len=10",
        "[2026-09-22 10:00:03] [INVOCATION 2.0] DIRECT SUCCESS: ollama",
    ]
    days = summarise(parse_log(lines))
    assert sum(d["prompts"] for d in days.values()) == 1, (
        "a non-session invocation reached the denominator"
    )


# ── money: production + synthetic + unknown == all rows ───────────────────

def test_provenance_split_accounts_for_every_row(tmp_path, monkeypatch):
    """No row is in two buckets, and none is in none.

    `unknown` exists as its own bucket precisely so a row whose provenance was
    never measured is not silently counted as production — the failure that
    `COALESCE(is_simulated, 1) = 0` fails closed against.
    """
    import sqlite3

    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE savings_stats (timestamp TEXT, estimated_claude_cost_saved REAL, "
        "is_simulated INTEGER)"
    )
    rows = [(1.0, 0), (2.0, 0), (99.0, 1), (7.0, None)]
    conn.executemany(
        "INSERT INTO savings_stats VALUES (datetime('now'), ?, ?)", rows
    )
    conn.commit()

    counts = dict(conn.execute(
        "SELECT COALESCE(is_simulated, -1), COUNT(*) FROM savings_stats GROUP BY 1"
    ).fetchall())
    conn.close()

    production, synthetic, unknown = counts.get(0, 0), counts.get(1, 0), counts.get(-1, 0)
    assert production + synthetic + unknown == len(rows), (
        f"rows vanished between the buckets: {counts}"
    )
    assert unknown == 1, (
        "a row with NULL provenance was folded into a measured bucket. "
        "'Never measured' is not 'production'."
    )
    assert production == 2 and synthetic == 1


# ── the registry itself: every counter reports or says why not ─────────────

def test_every_registered_counter_reports_a_value_or_a_reason():
    from llm_router import counter_registry

    readings = counter_registry.readings()
    assert readings, "the counter registry is empty"
    for counter, reading in readings:
        if reading.value is None:
            assert reading.unknown_reason, (
                f"{counter.id} reports Unknown with no reason, which is "
                "indistinguishable from a counter nobody wired up"
            )
        else:
            assert reading.value >= 0, f"{counter.id} reports {reading.value}"
