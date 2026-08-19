"""Integrity pillar — persisted state must always be consistent.

Tests cover: schema migration safety, concurrent writes, crash recovery,
atomic transitions, audit-trail completeness, and PII evidence
guarantees. A failure here means user data loss or compliance breach;
none of the integrity tests are allowed to xfail or skip in CI.
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
from pathlib import Path

import pytest

from llm_router.agents import (
    BudgetExceeded,
    SessionState,
    SessionStore,
)
from llm_router.lineage import (
    Inversion,
    LineageStore,
    Tier,
    detect_inversion,
    make_record,
)
from llm_router.lineage.lineage_store import _connect
from llm_router.signals.pii import PiiSignal


# ────────────────────────────────────────────────────────────────────────
# Schema migration safety
# ────────────────────────────────────────────────────────────────────────

def test_lineage_migration_is_idempotent(tmp_path: Path):
    """Opening the store twice must not corrupt the schema or duplicate columns."""
    db = tmp_path / "lineage.db"
    LineageStore(db_path=db).close()
    LineageStore(db_path=db).close()
    # Inspect schema directly
    conn = sqlite3.connect(str(db))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lineage)").fetchall()]
    conn.close()
    # Each v0.0.2 column appears exactly once
    for col in ("agent_id", "session_id", "step_index",
                "parent_session_id", "framework"):
        assert cols.count(col) == 1, f"column {col} appears {cols.count(col)} times"


def test_lineage_migration_adds_v002_columns_to_pre_v002_db(tmp_path: Path):
    """Simulate a pre-v0.0.2 DB (no v0.0.2 columns) — store must upgrade it."""
    db = tmp_path / "legacy.db"
    # Create a minimal v0.0.1-shaped lineage table by hand
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE lineage (
            id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            host TEXT NOT NULL,
            prompt_fingerprint TEXT NOT NULL,
            task_type TEXT NOT NULL,
            complexity TEXT NOT NULL,
            classifier_method TEXT NOT NULL,
            signal_scores TEXT NOT NULL,
            fired_decisions TEXT NOT NULL,
            chain_attempted TEXT NOT NULL,
            model_chosen TEXT NOT NULL,
            model_tier TEXT NOT NULL,
            inversion TEXT NOT NULL,
            outcome TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

    # Open via LineageStore → migration must run
    store = LineageStore(db_path=db)
    store.close()

    conn = sqlite3.connect(str(db))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lineage)").fetchall()]
    conn.close()
    assert "agent_id" in cols
    assert "session_id" in cols
    assert "step_index" in cols
    assert "parent_session_id" in cols
    assert "framework" in cols


# ────────────────────────────────────────────────────────────────────────
# Crash recovery: rows persist across process kill
# ────────────────────────────────────────────────────────────────────────

def test_lineage_rows_persist_after_store_close(tmp_path: Path):
    db = tmp_path / "lineage.db"
    store = LineageStore(db_path=db)
    for i in range(50):
        store.record(make_record(
            host="x", prompt_fingerprint=f"fp{i}", task_type="query",
            complexity="simple", classifier_method="heuristic",
            signal_scores={}, fired_decisions=(), chain_attempted=("m",),
            model_chosen="ollama/qwen3.5:latest", outcome="success",
            latency_ms=10, cost_usd=0.0,
        ))
    store.close()

    # Re-open — every row must be there
    store2 = LineageStore(db_path=db)
    assert len(store2.recent(limit=100)) == 50
    store2.close()


def test_session_state_persists_after_store_close(tmp_path: Path):
    db = tmp_path / "s.db"
    store = SessionStore(db_path=db)
    s = store.create(agent_id="reviewer", budget_usd=1.0)
    store.record_step(s.session_id, cost_usd=0.30)
    sid = s.session_id
    store.close()

    store2 = SessionStore(db_path=db)
    s2 = store2.get(sid)
    assert s2.consumed_usd == pytest.approx(0.30)
    assert s2.step_count == 1
    assert s2.state == SessionState.ACTIVE
    store2.close()


# ────────────────────────────────────────────────────────────────────────
# Concurrent writes
# ────────────────────────────────────────────────────────────────────────

def test_lineage_concurrent_inserts_lose_no_rows(tmp_path: Path):
    """20 threads × 25 inserts each → 500 rows persisted, none lost."""
    db = tmp_path / "lineage.db"
    # Initialize once before threads start (avoids race on schema creation)
    LineageStore(db_path=db).close()

    def worker(thread_id: int) -> int:
        store = LineageStore(db_path=db)
        for i in range(25):
            store.record(make_record(
                host="x",
                prompt_fingerprint=f"t{thread_id}_i{i}",
                task_type="query",
                complexity="simple",
                classifier_method="heuristic",
                signal_scores={},
                fired_decisions=(),
                chain_attempted=("m",),
                model_chosen="ollama/qwen3.5:latest",
                outcome="success",
                latency_ms=10,
                cost_usd=0.0,
            ))
        store.close()
        return 25

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(worker, i) for i in range(20)]
        total = sum(f.result() for f in futures)
    assert total == 500  # each thread succeeded

    # All rows present in DB
    store = LineageStore(db_path=db)
    assert len(store.recent(limit=1000)) == 500


def test_lineage_connections_are_configured_for_concurrency(tmp_path: Path):
    """Guard the fix for 'database is locked' under concurrent writers.

    The test above is timing-dependent — it passed on 3 of 4 CI Python
    versions while the bug was live — so it cannot by itself protect the fix.
    This asserts the mechanism directly.

    SQLite serialises writers under every journal mode, so the busy timeout is
    what decides whether a contending writer queues or raises. Python's default
    is 5s, and 60 threads x 40 writes measured a 4.41s worst case on a rollback
    journal: thin enough that a slower CI runner tipped past it. Both pragmas
    are asserted because they fix different halves — the timeout is what
    prevents the error, WAL is what keeps the wait short (1.50s for the same
    workload) and lets readers run alongside the writer.
    """
    db = tmp_path / "lineage.db"
    LineageStore(db_path=db).close()

    # journal_mode persists in the database header, so any connection sees it.
    plain = sqlite3.connect(db)
    try:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal", (
            "lineage DB must be in WAL mode so readers don't block the writer"
        )
    finally:
        plain.close()

    # busy_timeout is per-connection — it must be set on every connect the
    # store makes, which is why they all go through the one helper.
    conn = _connect(db)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000, (
            "every lineage connection needs a busy timeout well above Python's "
            "5s default, or concurrent writers raise instead of queueing"
        )
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────────
# Atomic budget enforcement
# ────────────────────────────────────────────────────────────────────────

def test_budget_breach_atomically_terminates_session(tmp_path: Path):
    """The breach + terminal-state transition must be one atomic write —
    a partial state where consumed > cap but state == ACTIVE is forbidden."""
    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=0.10)
    try:
        store.record_step(s.session_id, cost_usd=0.50)
    except BudgetExceeded:
        pass

    final = store.get(s.session_id)
    # Either: state went to BUDGET_EXCEEDED with consumed > cap,
    # or: the step was rejected entirely and consumed stayed at 0.
    # The forbidden state: consumed > cap AND state == ACTIVE.
    if final.consumed_usd > final.budget_cap_usd:
        assert final.state == SessionState.BUDGET_EXCEEDED, (
            "Session over budget but still ACTIVE — atomicity broken"
        )


def test_budget_envelope_monotonic_consumed(tmp_path: Path):
    """Consumed must never decrease across record_step calls."""
    store = SessionStore(db_path=tmp_path / "s.db")
    s = store.create(agent_id="x", budget_usd=10.0)
    prev = 0.0
    for i in range(20):
        cur = store.record_step(s.session_id, cost_usd=0.01).consumed_usd
        assert cur >= prev, (
            f"consumed decreased: prev={prev} → cur={cur} at step {i}"
        )
        prev = cur


# ────────────────────────────────────────────────────────────────────────
# Inversion detection invariants
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("complexity,tier,expected", [
    ("simple", Tier.LOCAL, Inversion.NONE),
    ("simple", Tier.CHEAP, Inversion.NONE),
    ("simple", Tier.MID, Inversion.DOWN),
    ("simple", Tier.PREMIUM, Inversion.DOWN),
    ("moderate", Tier.LOCAL, Inversion.NONE),
    ("moderate", Tier.CHEAP, Inversion.NONE),
    ("moderate", Tier.MID, Inversion.NONE),
    ("moderate", Tier.PREMIUM, Inversion.NONE),
    ("complex", Tier.LOCAL, Inversion.UP),
    ("complex", Tier.CHEAP, Inversion.UP),
    ("complex", Tier.MID, Inversion.UP),
    ("complex", Tier.PREMIUM, Inversion.NONE),
])
def test_inversion_table_invariant(complexity, tier, expected):
    """Full cross-product of (complexity × tier) returns the expected inversion."""
    assert detect_inversion(complexity, tier) == expected


# ────────────────────────────────────────────────────────────────────────
# PII evidence guarantee — secrets MUST NOT appear in evidence
# ────────────────────────────────────────────────────────────────────────

def test_pii_evidence_never_contains_openai_key():
    signal = PiiSignal()
    secret = "sk-proj-MUSTNOTAPPEAR1234567890abcdef"
    score = signal.evaluate(f"my key: {secret}")
    assert "MUSTNOTAPPEAR" not in score.evidence
    assert "sk-proj" not in score.evidence


def test_pii_evidence_never_contains_anthropic_key():
    signal = PiiSignal()
    secret = "sk-ant-DONOTLEAKabcdefghijklmnopqrst"
    score = signal.evaluate(f"key={secret}")
    assert "DONOTLEAK" not in score.evidence


def test_pii_evidence_never_contains_aws_key():
    signal = PiiSignal()
    score = signal.evaluate("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in score.evidence


def test_pii_evidence_never_contains_jwt():
    signal = PiiSignal()
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    score = signal.evaluate(f"Authorization: Bearer {jwt}")
    assert jwt not in score.evidence
    assert "Bearer" not in score.evidence or "Bearer" in "matched pattern"


# ────────────────────────────────────────────────────────────────────────
# Lineage audit trail completeness
# ────────────────────────────────────────────────────────────────────────

def test_lineage_record_preserves_all_fields(tmp_path: Path):
    """Every field passed to make_record must round-trip through SQLite."""
    store = LineageStore(db_path=tmp_path / "lineage.db")
    rec = make_record(
        host="claude-code",
        prompt_fingerprint="abc123",
        task_type="code",
        complexity="complex",
        classifier_method="signal_engine",
        signal_scores={"pii": 0.0, "code": 0.8},
        fired_decisions=("route_code_tasks",),
        chain_attempted=("ollama/qwen3.5:latest", "openai/gpt-4o"),
        model_chosen="openai/gpt-4o",
        outcome="success",
        latency_ms=2500,
        cost_usd=0.012,
        notes="manual override",
        agent_id="code-reviewer",
        session_id="sess-uuid-1",
        step_index=4,
        parent_session_id="parent-uuid-0",
        framework="agno",
    )
    store.record(rec)
    rows = store.recent(limit=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["host"] == "claude-code"
    assert row["task_type"] == "code"
    assert row["complexity"] == "complex"
    assert row["agent_id"] == "code-reviewer"
    assert row["session_id"] == "sess-uuid-1"
    assert row["step_index"] == 4
    assert row["parent_session_id"] == "parent-uuid-0"
    assert row["framework"] == "agno"
    assert row["model_chosen"] == "openai/gpt-4o"
    assert row["latency_ms"] == 2500


def test_lineage_session_query_returns_steps_in_order(tmp_path: Path):
    """by_session must return rows ordered by step_index ASC."""
    store = LineageStore(db_path=tmp_path / "lineage.db")
    sid = "sess-1"
    # Insert out of order
    for step in [3, 1, 4, 2, 0]:
        store.record(make_record(
            host="x", prompt_fingerprint=f"fp_step{step}", task_type="query",
            complexity="simple", classifier_method="heuristic",
            signal_scores={}, fired_decisions=(), chain_attempted=("m",),
            model_chosen="ollama/qwen3.5:latest", outcome="success",
            latency_ms=1, cost_usd=0.0,
            session_id=sid, step_index=step,
        ))

    rows = store.by_session(sid)
    assert [r["step_index"] for r in rows] == [0, 1, 2, 3, 4]
