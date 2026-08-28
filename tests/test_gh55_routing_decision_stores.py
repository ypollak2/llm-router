"""GH#55/#53: "no routing decisions" was reported for three different states.

The reporter made 4 real llm() calls and saw usage=4 rows, claude_usage=4,
savings_stats=3, routing_decisions=0 — while doctor/verify/last/replay all
said "no routing decisions yet". On THIS machine the inverse holds
(routing_decisions=1387, usage=0), which is the tell: there is no single
writer. Four different functions share the name `log_routing_decision`:

    cost.log_routing_decision              -> the routing_decisions TABLE
    routing_hints.log_routing_decision     -> structured audit log only
    model_tracking.log_routing_decision    -> model_tracking.jsonl
    lineage.decision_logger.log_routing_decision -> the lineage store

Whichever path a session takes, the commands query one table and report its
emptiness as "nothing happened".

Worse, doctor collapsed a THIRD state into the same message: `except
sqlite3.Error: simple_n, total_n = 0, 0` meant a missing table or a schema
mismatch printed the same "no routing decisions today yet — trigger a few
llm_* calls" as a genuinely idle machine. A user who had just made four calls
is told to make some calls.

Per the #53 decision, "routed" is reserved for real executions; counters that
measure hint emission say "classified".
"""
from __future__ import annotations

import sqlite3


from llm_router.commands import doctor


def _db(path, *, table=True, rows=0, other_rows=0):
    conn = sqlite3.connect(str(path))
    if table:
        conn.execute(
            "CREATE TABLE routing_decisions "
            "(timestamp TEXT, complexity TEXT, reason_code TEXT)"
        )
        for _ in range(rows):
            conn.execute(
                "INSERT INTO routing_decisions VALUES (datetime('now','localtime'),'simple','')"
            )
    conn.execute("CREATE TABLE usage (timestamp TEXT, model TEXT)")
    for _ in range(other_rows):
        conn.execute("INSERT INTO usage VALUES (datetime('now','localtime'),'ollama/x')")
    conn.commit()
    conn.close()
    return path


def test_missing_table_is_not_reported_as_no_activity(tmp_path):
    """The state that made this unreproducible: unreadable != idle."""
    db = _db(tmp_path / "usage.db", table=False, other_rows=4)
    state = doctor._routing_decision_state(db)
    assert state.readable is False
    assert "no activity" not in (state.summary or "").lower()
    assert state.summary and "routing_decisions" in state.summary


def test_empty_table_with_activity_elsewhere_says_so(tmp_path):
    """The reporter's exact shape: 0 here, 4 in usage."""
    db = _db(tmp_path / "usage.db", rows=0, other_rows=4)
    state = doctor._routing_decision_state(db)
    assert state.readable is True
    assert state.rows == 0
    assert state.other_activity == 4
    assert "usage" in state.summary, (
        f"must point at the store that DID record activity: {state.summary!r}"
    )
    assert "trigger" not in state.summary.lower(), (
        "must not tell a user who just made four calls to make some calls"
    )


def test_genuinely_idle_machine_still_says_idle(tmp_path):
    db = _db(tmp_path / "usage.db", rows=0, other_rows=0)
    state = doctor._routing_decision_state(db)
    assert state.readable is True and state.rows == 0 and state.other_activity == 0
    assert "no routing" in state.summary.lower()


def test_populated_table_reports_rows(tmp_path):
    db = _db(tmp_path / "usage.db", rows=7, other_rows=0)
    state = doctor._routing_decision_state(db)
    assert state.readable is True and state.rows == 7


def test_absent_database_is_distinguished_from_empty(tmp_path):
    state = doctor._routing_decision_state(tmp_path / "does-not-exist.db")
    assert state.readable is False
    assert state.summary


def test_the_four_writers_are_documented_and_distinct():
    """#55's root cause: four same-named functions, four different destinations.

    If a fifth appears, or one is renamed, this fails and the next reader is
    told where to look instead of rediscovering it from an empty table.
    """
    import importlib
    import inspect

    seen = {}
    for mod_name in (
        "llm_router.cost",
        "llm_router.routing_hints",
        "llm_router.model_tracking",
        "llm_router.lineage.decision_logger",
    ):
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, "log_routing_decision", None)
        assert fn is not None, f"{mod_name}.log_routing_decision vanished — update GH#55 notes"
        seen[mod_name] = inspect.signature(fn)

    assert len(seen) == 4
    # They are genuinely different functions, not re-exports of one.
    assert len({str(s) for s in seen.values()}) == 4, (
        f"signatures collapsed — are these still four distinct writers? {seen}"
    )
