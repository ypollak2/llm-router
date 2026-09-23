"""S4a — 87% of what trained the bandit was placeholder data.

Measured on the development ledger, `routing_decisions`, the table
`aggregate_stats` reads:

    provenance   rows   distinct latency values
    NULL         1387   1        <- every row exactly 500ms and exactly $0.01
    runtime       214   214      <- real measurements

The NULL rows are from 2026-07 and 2026-08, a period when latency and cost were
not being recorded; each carries the same two placeholder constants. They were
weighted equally with real measurements, and they outnumbered them six to one.

THE COLUMN THAT IDENTIFIES THEM HAS EXISTED ALL ALONG. `provenance` is written
by `_write_provenance()` at insert time and deliberately has no default —
cost.py's own docstring says a default "asserts the very thing it should be
recording", and criticises the older `is_real INTEGER DEFAULT 1` for exactly
that. All 1601 rows read `is_real = 1`, including the placeholders.

Nothing read either column. Built, correct, populated, unused — the CLASS-A
shape R12 was written for, sitting in the bandit's only source of evidence.

WHY THIS IS NOT A TUNING CHANGE. Excluding rows whose origin was never recorded
is the same fail-closed rule the money surfaces already apply, for the same
reason: until the instrument is trustworthy, nothing computed from it is
evidence. The measured delta on that ledger was that the top pick did not
change — the two models dropped were ranked last on placeholder data — and
neither was removed from routing, because `reorder()` explores from
`candidates` rather than from `eligible`.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
import tempfile

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"


def _ledger(rows):
    """A routing_decisions table with the columns the bandit query reads."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(tmp.name)
    con.execute("""
        CREATE TABLE routing_decisions (
            timestamp TEXT, profile TEXT, subject TEXT, final_model TEXT,
            success INTEGER, cost_usd REAL, latency_ms REAL,
            judge_score REAL, provenance TEXT, is_real INTEGER DEFAULT 1
        )""")
    con.executemany(
        "INSERT INTO routing_decisions (timestamp, profile, subject, final_model,"
        " success, cost_usd, latency_ms, judge_score, provenance)"
        " VALUES (datetime('now'), ?, NULL, ?, ?, ?, ?, NULL, ?)", rows)
    con.commit()
    return tmp.name, con


def test_the_query_filters_on_provenance():
    """AST on the SQL string the bandit runs.

    A source-text check would pass with the WHERE clause deleted and the
    explanation left in a comment — the A-10 evasion, in the module whose
    comment is the only record of why this matters.
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _ast_assert import string_constants

    tree = ast.parse((SRC / "telemetry.py").read_text(encoding="utf-8"))
    sql = [s for s in string_constants(tree) if "FROM routing_decisions" in s]
    assert sql, "the bandit's aggregate query is gone"
    assert any("provenance = 'runtime'" in q for q in sql), (
        "the bandit's query no longer filters on provenance, so rows whose "
        "origin was never recorded train it again. 87% of the development "
        "ledger was placeholder data when this filter was added."
    )


def test_placeholder_rows_do_not_reach_the_stats(tmp_path, monkeypatch):
    """Behaviour, end to end, against a real SQLite table.

    `aggregate_stats` takes no db_path, so the ledger is redirected by env.
    Driving it for real matters: the AST test above proves the WHERE clause is
    written, and only this one proves it does anything.
    """
    import asyncio

    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))

    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE routing_decisions (
            timestamp TEXT, profile TEXT, subject TEXT, final_model TEXT,
            success INTEGER, cost_usd REAL, latency_ms REAL,
            judge_score REAL, provenance TEXT, is_real INTEGER DEFAULT 1
        )""")
    con.executemany(
        "INSERT INTO routing_decisions (timestamp, profile, subject, final_model,"
        " success, cost_usd, latency_ms, judge_score, provenance)"
        " VALUES (datetime('now'), ?, NULL, ?, ?, ?, ?, NULL, ?)",
        # 20 placeholder rows: perfect success, identical constants, NO provenance
        [("balanced", "paid/model", 1, 0.01, 500.0, None) for _ in range(20)]
        # 6 measured rows for the same model, genuinely worse
        + [("balanced", "paid/model", 0, 0.02, 9000.0, "runtime") for _ in range(6)],
    )
    con.commit()
    con.close()

    # Reset any cached connection so the env override is picked up.
    from llm_router import cost, telemetry
    cost._db = None

    stats = asyncio.run(telemetry.aggregate_stats(
        profile="balanced", subject="general", candidates=["paid/model"],
    ))
    assert stats, "no stats returned; the ledger redirect did not take"
    row = stats[0]
    assert row.n_samples == 6, (
        f"n_samples={row.n_samples}, expected 6. The 20 placeholder rows are "
        "still counted as evidence — exactly the 87% contamination this "
        "filter removes."
    )
    assert row.success_rate == 0.0, (
        f"success_rate={row.success_rate}; placeholder successes leaked in and "
        "would make a failing model look perfect"
    )
    assert row.avg_latency_ms == pytest.approx(9000.0), (
        "the 500ms placeholder latency is still being averaged in"
    )


def test_a_default_bearing_column_cannot_answer_this():
    """`is_real INTEGER DEFAULT 1` is why a second column had to exist.

    Pinned because the temptation is to 'simplify' by reusing it. A default of
    1 means every legacy row claims to be real, which is precisely the claim
    the column was meant to verify.
    """
    src = (SRC / "cost.py").read_text(encoding="utf-8")
    assert "ALTER TABLE routing_decisions ADD COLUMN provenance TEXT" in src, (
        "the provenance column is gone from the migrations"
    )
    assert "DEFAULT" not in src.split(
        "ALTER TABLE routing_decisions ADD COLUMN provenance TEXT")[1][:40], (
        "a DEFAULT was added to `provenance`. A default here manufactures the "
        "evidence the column exists to record — see the `is_real` precedent."
    )


def test_excluded_models_are_still_routable():
    """The property that makes this safe rather than a policy change.

    `reorder()` explores from `candidates`, not from `eligible`, so a model
    whose only rows were placeholders becomes UNDER-SAMPLED — still offered,
    still explored — rather than removed.
    """
    tree = ast.parse((SRC / "bandit.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "reorder"
    )
    body = ast.unparse(fn)
    assert "explore_pool = [m for m in candidates" in body, (
        "exploration no longer draws from `candidates`. With the provenance "
        "filter in place, a model with no trusted rows would then be dropped "
        "from routing entirely instead of being explored."
    )
    assert "if not eligible:" in body, (
        "the cold-start fallback to the static policy order is gone; with "
        "fewer trusted rows it is now load-bearing"
    )
