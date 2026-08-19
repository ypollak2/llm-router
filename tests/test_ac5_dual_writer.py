"""AC-5 regression: the savings_log dual-writer race must not double-insert.

`cost.import_savings_log` (async, MCP server) and
`session-end.py::_sync_import_savings_log` (sync hook) both drain the shared
`savings_log.jsonl`. The old read-then-truncate had no lock, so two concurrent
drainers could read the same rows and insert them twice into `savings_stats`.
Both now **atomically claim** the log via `os.replace`, so exactly one drainer
processes each row.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from llm_router import cost


@pytest.fixture
def savings_env(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    import llm_router.config as config_module
    config_module._config = None  # re-read env
    log = tmp_path / "savings_log.jsonl"
    monkeypatch.setattr(cost, "SAVINGS_LOG_PATH", log)
    return db, log


def _write_lines(log, n: int) -> None:
    entry = {
        "timestamp": "2026-01-01T00:00:00Z", "session_id": "s",
        "task_type": "query", "estimated_saved": 0.01, "external_cost": 0.001,
        "model": "flash", "host": "claude_code",
    }
    log.write_text("\n".join(json.dumps(entry) for _ in range(n)) + "\n")


def _count(db) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM savings_stats").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_concurrent_import_no_double_insert(savings_env):
    """Fail-before: both drainers read N rows and insert → 2N. Pass-after: the
    atomic claim gives exactly one drainer the rows → N total, N in the table."""
    db, log = savings_env
    n = 20
    _write_lines(log, n)

    r1, r2 = await asyncio.gather(
        cost.import_savings_log(), cost.import_savings_log()
    )
    assert r1 + r2 == n            # exactly N imported total, never 2N
    assert {r1, r2} == {n, 0}      # one claims all rows, the other no-ops
    assert _count(db) == n         # no double-insert into savings_stats
    assert not log.exists() or log.read_text() == ""  # log fully drained


@pytest.mark.asyncio
async def test_import_drains_and_second_call_is_noop(savings_env):
    """A single import drains everything; a subsequent import finds no log."""
    db, log = savings_env
    _write_lines(log, 5)
    first = await cost.import_savings_log()
    second = await cost.import_savings_log()
    assert first == 5
    assert second == 0
    assert _count(db) == 5
