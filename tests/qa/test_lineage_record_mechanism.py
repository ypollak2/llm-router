"""LineageStore.record -- what one call does to the disk, counted, not timed.

The wall-clock budget on this path (``test_performance.py::
test_perf_lineage_record_p50_budget``) measures the runner's fsync latency, not
our code: the same ``record()`` costs p50 0.11 ms on tmpfs and 1.2 ms on a
virtualised Linux disk, and shared GitHub runners stalled it to 34.6 and 64.8 ms
(TIMING-1 follow-up, docs/bugs/LINEAGE-PERF-1.md). A regression that adds a
reconnect or an fsync per row would be invisible to a budget loose enough to
survive those stalls, so this test pins the mechanism instead. It is not marked
``timing`` and gives the same answer on any hardware.

Pinned (one ``record()`` call, measured 2026-10-09 at origin/main 33ab2cdc):

    sqlite3.connect calls ........ 1   (a fresh short-lived connection per call)
    Connection.close calls ....... 1   (closing the last connection checkpoints and
                                        deletes the WAL: that is where most of the
                                        ~5 fdatasync + 2 unlink per record, seen
                                        with strace on Linux, come from)
    transactions (BEGIN/COMMIT) .. 1 / 1
    statements ................... busy_timeout, journal_mode=WAL, INSERT, nothing else
    os.fsync / os.fdatasync ...... 0   (the JSONL append is deliberately not fsynced)
    synchronous ................. FULL (2): no PRAGMA synchronous anywhere

SQLite's own fsyncs happen in C and cannot be intercepted portably, so they are
pinned by what determines them: connection count, transaction count, the
synchronous level and the journal mode. strace is not needed, so this runs on
macOS and Linux.

A change that lowers these numbers on purpose (persistent connection,
``synchronous=NORMAL``) is a durability / concurrency decision: update the
constants below in that PR and say why.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from llm_router.lineage import LineageStore, make_record
from llm_router.lineage import lineage_store as ls_mod

EXPECTED_CONNECTS = 1
EXPECTED_CLOSES = 1
EXPECTED_BEGINS = 1
EXPECTED_COMMITS = 1
EXPECTED_PYTHON_FSYNCS = 0
EXPECTED_NON_TXN_SQL = ["PRAGMA busy_timeout = 30000", "PRAGMA journal_mode = WAL"]


class _CountingConn:
    """Delegating proxy: counts close(), records every statement SQLite runs."""

    def __init__(self, real: sqlite3.Connection, log: dict) -> None:
        self._real = real
        self._log = log
        real.set_trace_callback(lambda stmt: log["sql"].append(stmt.strip()))

    def close(self):
        self._log["closes"] += 1
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _rec(i: int):
    return make_record(
        host="claude-code", prompt_fingerprint=f"fp{i}", task_type="query",
        complexity="simple", classifier_method="heuristic",
        signal_scores={"a": 0.5}, fired_decisions=("d",),
        chain_attempted=("ollama/qwen3.5:latest",),
        model_chosen="ollama/qwen3.5:latest", outcome="success",
        latency_ms=10, cost_usd=0.0,
    )


@pytest.fixture
def counted(monkeypatch):
    log = {"connects": 0, "closes": 0, "sql": [], "fsync": 0}
    real_connect = sqlite3.connect

    def connect(*a, **k):
        log["connects"] += 1
        return _CountingConn(real_connect(*a, **k), log)

    def counting(orig):
        def wrapper(*a, **k):
            log["fsync"] += 1
            return orig(*a, **k)
        return wrapper

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(os, "fsync", counting(os.fsync))
    if hasattr(os, "fdatasync"):  # absent on macOS
        monkeypatch.setattr(os, "fdatasync", counting(os.fdatasync))
    return log


def test_record_connect_transaction_and_fsync_counts(tmp_path: Path, counted):
    store = LineageStore(db_path=tmp_path / "lineage.db")  # init opens its own connections
    counted.update(connects=0, closes=0, fsync=0)
    counted["sql"].clear()

    store.record(_rec(1))

    sql = counted["sql"]
    begins = [s for s in sql if s == "BEGIN"]
    commits = [s for s in sql if s == "COMMIT"]
    inserts = [s for s in sql if s.startswith("INSERT OR REPLACE INTO lineage")]
    other = [s for s in sql if s not in begins + commits and s not in inserts]

    assert sql, "trace callback saw nothing: the instrumentation is not attached"
    assert counted["connects"] == EXPECTED_CONNECTS, "record() opens a different number of connections"
    assert counted["closes"] == EXPECTED_CLOSES
    assert len(begins) == EXPECTED_BEGINS and len(commits) == EXPECTED_COMMITS
    assert len(inserts) == 1, "exactly one row insert per record()"
    assert other == EXPECTED_NON_TXN_SQL, f"unexpected statements: {other}"
    assert counted["fsync"] == EXPECTED_PYTHON_FSYNCS


def test_record_counts_do_not_grow_with_rows(tmp_path: Path, counted):
    """Per-call cost is constant: the 20th record does what the 1st did."""
    store = LineageStore(db_path=tmp_path / "lineage.db")
    for i in range(19):
        store.record(_rec(i))
    counted.update(connects=0, closes=0, fsync=0)
    counted["sql"].clear()
    store.record(_rec(99))
    assert (counted["connects"], counted["closes"], counted["fsync"]) == (
        EXPECTED_CONNECTS, EXPECTED_CLOSES, EXPECTED_PYTHON_FSYNCS)
    assert len([s for s in counted["sql"] if s == "COMMIT"]) == EXPECTED_COMMITS


def test_record_connection_keeps_full_synchronous_wal(tmp_path: Path):
    """The durability level that decides how many fsyncs a commit costs."""
    LineageStore(db_path=tmp_path / "lineage.db")
    conn = ls_mod._connect(tmp_path / "lineage.db")
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
