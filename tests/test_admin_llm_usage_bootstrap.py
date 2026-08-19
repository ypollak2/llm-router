"""Regression test for llm_usage schema bootstrap on a fresh DB.

Background
----------
Prior to the bootstrap fix in ``tools/admin.py:llm_usage``, calling the tool
against a 0-byte or schema-less ``usage.db`` raised
``sqlite3.OperationalError: no such table: usage`` because the dashboard
queries assumed the schema already existed. The writer path
(``cost._get_db``) is the only place schemas are created, but the writer
hadn't run yet on a fresh install — so users saw a hard error instead of
the empty-state UI.

This test guarantees ``llm_usage`` renders the empty-state UI on a fresh DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from llm_router.config import get_config
from llm_router.tools.admin import llm_usage


@pytest.mark.asyncio
async def test_llm_usage_bootstraps_schema_on_empty_db(temp_db):
    """llm_usage must render empty-state UI on a fresh DB, not error.

    The fixture starts with no ``usage.db`` file at all. Without the
    bootstrap, the first dashboard query would fail. With it, the schema
    is created idempotently and queries return zero rows cleanly.
    """
    db_path = get_config().llm_router_db_path
    assert not db_path.exists() or db_path.stat().st_size == 0, (
        "Precondition: temp_db should leave us with a missing/0-byte DB"
    )

    out = await llm_usage(period="today")

    # Renders the standard frame
    assert "LLM Usage Dashboard" in out
    assert "EXTERNAL APIs" in out

    # Schema now exists — usage table is queryable
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()

    assert "usage" in tables, "Bootstrap should have created the usage table"
    assert "routing_decisions" in tables
    assert "savings_stats" in tables
