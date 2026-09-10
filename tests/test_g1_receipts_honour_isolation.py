"""receipts.db must land where the state directory says, resolved per call.

``receipt_store._DB_PATH`` was a module-level constant evaluated at import:

    _DB_PATH = Path.home() / ".llm-router" / "receipts.db"

so it froze the real user's home the moment the module was first imported.
``LLM_ROUTER_HOME`` — the supported isolation override — could not move it, and
neither could patching ``Path.home`` afterwards. The whole test suite has been
writing receipts into the developer's real database as a result: a live
``receipts.db`` on this machine contains rows for ``ollama/badmodel``,
``ollama/a``, ``ollama/b`` and ``openai/some-model``, which exist only as unit
test fixtures.

paths.py's own docstring already warned this class of bug was out there —
"usage.db alone is resolved ~23 different ways. Four modules honour an override,
and each honours a *different* variable". This is one of them, now closed.
"""

from __future__ import annotations

import pytest


def test_receipt_path_follows_the_state_dir(tmp_path, monkeypatch):
    import llm_router.receipt_store as rs

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    assert rs._db_path() == tmp_path / "state" / "receipts.db", (
        "receipts.db ignored LLM_ROUTER_HOME — the supported isolation override"
    )


def test_receipt_path_is_resolved_per_call_not_at_import(tmp_path, monkeypatch):
    """The defining property: changing the state dir after import must move it."""
    import llm_router.receipt_store as rs

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "one"))
    first = rs._db_path()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "two"))
    second = rs._db_path()

    assert first != second, "the path was frozen at import time"


@pytest.mark.asyncio
async def test_storing_a_receipt_writes_under_the_state_dir(tmp_path, monkeypatch):
    import sqlite3

    from llm_router.receipt_store import Receipt, store_receipt

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))

    await store_receipt(Receipt(
        contract_id="c1", model="ollama/test-model", task_type="query",
        complexity="simple", gates_passed=["schema"], gate_count=1, all_passed=True,
        latency_ms=1.0, input_tokens=1, output_tokens=1, cost_usd=0.0,
        opus_equivalent_cost=0.001, tokens_reclaimed=2, savings_usd=0.001,
    ))

    db = tmp_path / "state" / "receipts.db"
    assert db.exists(), "receipt did not land in the isolated state dir"
    conn = sqlite3.connect(db)
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM receipts WHERE model = 'ollama/test-model'"
        ).fetchone()
    finally:
        conn.close()
    assert n == 1
