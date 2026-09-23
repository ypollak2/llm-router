"""S1/S2 — the instrument must not measure itself.

Phase 50, on the published 15.0.0, running a READ-ONLY diagnostic three times
against unchanged state:

    llm-router doctor --audit
    fail_open_events: 4
    fail_open_events: 8
    fail_open_events: 12

`doctor --audit` opened the database; two `ALTER TABLE … ADD COLUMN
is_simulated` statements hit `duplicate column name`; each was recorded as a
swallowed failure. So an operator investigating a high count made it higher,
and the figures published from that counter — `fail_open_events: 713, of which
CHZ-FO-COST-MIGRATE-ALTER = 531` in the R12 commit — were partly measuring the
diagnostic rather than the product.

R12's claim "every counter has a reader" survived that. "The counters mean
something" had never been separately established, and this one did not.

Two properties are asserted here, for EVERY registered counter rather than for
the one that was found:

  S1  reading twice returns the same value
  S2  a fresh database with no faults injected reads zero

S2 is the stronger of the two. A counter whose baseline is normal operation
cannot signal abnormal operation, no matter how faithfully it is read.

TWO INDEPENDENT FIXES, AND THE RED-CHECK PROVED THEY ARE INDEPENDENT.

  S6  `_column_exists` no longer consults a hand-maintained table allowlist
      that had drifted (it was missing `codex_usage`, `gemini_usage` and
      `migrations`). An unknown table used to return False — "the column does
      not exist" — when the truth was "I cannot tell", so the ALTER ran.
  S2  `_safe_migrate` treats `duplicate column name` as SUCCESS.

Removing either one alone leaves the counter at zero, because the other
catches it. Removing BOTH produces 4 migration fail-opens across three
database opens and fails this file. Neither is redundant: S6 stops the
statement being attempted, S2 stops an attempt that slips through any path the
regex cannot parse. Delete one and the protection is single-layered against a
defect that has already shipped once.

The precedent already existed and was never generalised: `hook_liveness.
orphan_count()` deliberately does not reap, pinned by
`test_reading_the_count_does_not_change_it`. One counter had the rule. Now all
of them do.
"""

from __future__ import annotations

import pytest

from llm_router import counter_registry


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A fresh database AND a fresh process, for the counters that need each.

    S2 as first written said "a fresh database reads zero", and that framing is
    wrong for two of the seven counters. `capture_outcomes` and
    `ledger_events_dropped` live in PROCESS-GLOBAL memory, not on disk — a new
    database says nothing about them, and in a long-lived pytest process they
    legitimately carry whatever earlier tests recorded.

    The test found this by failing only when run after the rest of the suite.
    The property S2 actually wants is "zero when nothing has happened yet",
    and what "yet" means depends on where the counter lives. Both are reset
    here so the assertion means the same thing for every counter.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    from llm_router import execution_ledger, failopen, prompt_capture, session_store  # noqa: F401

    failopen.clear()
    # Process-local counters: a fresh database does not clear them.
    monkeypatch.setattr(prompt_capture, "_counters", {}, raising=False)
    monkeypatch.setattr(session_store, "_lock_timeouts", 0, raising=False)
    execution_ledger.reset_dropped_event_count()
    # S8's low-signal counter is the third of this kind: it counts in module
    # globals inside `classify`, so every prompt any earlier test classified is
    # still on the tally. Same reasoning as the two above.
    from llm_router import classify as _classify

    _classify.reset_low_signal_counters()
    yield


def test_the_registry_is_not_empty():
    """Anti-vacuity: every assertion below is trivial over an empty registry."""
    assert len(counter_registry.REGISTRY) >= 7, (
        f"only {len(counter_registry.REGISTRY)} counters registered"
    )


@pytest.mark.parametrize(
    "counter_id", counter_registry.counter_ids(), ids=lambda c: c
)
def test_reading_a_counter_twice_returns_the_same_value(counter_id):
    """S1. If the second read differs, the instrument is measuring itself."""
    first = counter_registry.read_one(counter_id)
    second = counter_registry.read_one(counter_id)
    third = counter_registry.read_one(counter_id)
    assert first.value == second.value == third.value, (
        f"{counter_id}: three consecutive reads gave "
        f"{first.value}, {second.value}, {third.value}. Reading it changes it, "
        "so any number derived from it includes however many times someone "
        "looked."
    )


def test_rendering_the_whole_report_twice_is_stable():
    """The composed case — a section may touch a path another section counts."""
    first = counter_registry.render_lines()
    second = counter_registry.render_lines()
    assert first == second, (
        "the audit report is not stable across two consecutive renders:\n"
        + "\n".join(
            f"  1: {a}\n  2: {b}" for a, b in zip(first, second) if a != b
        )
    )


@pytest.mark.parametrize(
    "counter_id", counter_registry.counter_ids(), ids=lambda c: c
)
def test_a_fresh_database_reads_zero(counter_id):
    """S2. A counter whose zero is unreachable cannot signal anything.

    `unterminated_invocations` legitimately reads Unknown on a fresh install:
    there is no routing log, and "no measurement" is not "zero", which is the
    rule the rest of this codebase already follows. Unknown is accepted here;
    a positive count is not.
    """
    reading = counter_registry.read_one(counter_id)
    if reading.value is None:
        return  # Unknown is honest on a fresh machine; see the docstring.
    assert reading.value == 0, (
        f"{counter_id} reads {reading.value} on a brand-new database with no "
        "faults injected. Its baseline is normal operation, so a real spike is "
        "indistinguishable from ordinary use.\n"
        f"detail: {reading.detail}"
    )


def test_opening_the_database_records_no_fail_open():
    """The specific regression, asserted directly.

    Measured before the fix: 2 fail-open events per database open, 6 across
    three opens. After: 0.
    """
    import asyncio

    from llm_router import cost, failopen

    async def _open_three_times():
        for _ in range(3):
            db = await cost._get_db()
            await db.close()
            cost._db = None

    asyncio.run(_open_three_times())
    failopen.reset_cache()
    snap = failopen.snapshot()
    migrate = snap.by_code.get("CHZ-FO-COST-MIGRATE-ALTER", 0)
    assert migrate == 0, (
        f"opening the database {3} times recorded {migrate} migration "
        "fail-opens. An idempotent migration that no-ops is the migration "
        "SUCCEEDING; recording it as a swallowed failure is what made the "
        "counter self-inflating."
    )


def test_an_idempotent_migration_is_success_not_degradation():
    """S2 at the unit level: the same ALTER twice records nothing."""
    import asyncio

    import aiosqlite

    from llm_router import cost, failopen

    async def _twice(tmp):
        async with aiosqlite.connect(tmp) as db:
            await db.execute("CREATE TABLE t (a INTEGER)")
            await cost._safe_migrate(db, "ALTER TABLE t ADD COLUMN b INTEGER")
            await cost._safe_migrate(db, "ALTER TABLE t ADD COLUMN b INTEGER")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        asyncio.run(_twice(f.name))

    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-COST-MIGRATE-ALTER", 0) == 0


def test_a_genuine_migration_failure_is_still_recorded():
    """The other half. A rule that records nothing is not a fix.

    S2 makes "already there" a success. It must not make "the table does not
    exist" or "syntax error" a success too — those are the cases the counter
    was built for.
    """
    import asyncio

    import aiosqlite

    from llm_router import cost, failopen

    async def _broken(tmp):
        async with aiosqlite.connect(tmp) as db:
            await cost._safe_migrate(
                db, "ALTER TABLE table_that_does_not_exist ADD COLUMN b INTEGER"
            )

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        asyncio.run(_broken(f.name))

    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-COST-MIGRATE-ALTER", 0) == 1, (
        "a migration against a missing table was swallowed silently; S2 "
        "widened 'success' too far"
    )
