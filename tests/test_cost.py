"""Tests for cost tracking."""


import pytest

from llm_router import cost
from llm_router.types import LLMResponse, RoutingProfile, TaskType


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Use a temporary database for testing."""
    db_path = tmp_path / "test_usage.db"
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    return db_path


@pytest.mark.asyncio
async def test_log_and_query_usage(temp_db):
    resp = LLMResponse(
        content="test",
        model="openai/gpt-4o",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        latency_ms=500.0,
        provider="openai",
    )
    await cost.log_usage(resp, TaskType.QUERY, RoutingProfile.BALANCED)

    summary = await cost.get_usage_summary("all")
    assert "1" in summary  # 1 call
    assert "gpt-4o" in summary
    assert "$0.0010" in summary


@pytest.mark.asyncio
async def test_empty_usage(temp_db):
    summary = await cost.get_usage_summary("all")
    assert "No usage data" in summary


@pytest.mark.asyncio
async def test_multiple_entries(temp_db):
    for i in range(3):
        resp = LLMResponse(
            content=f"test-{i}",
            model="gemini/gemini-2.5-flash",
            input_tokens=50,
            output_tokens=25,
            cost_usd=0.0001,
            latency_ms=200.0,
            provider="gemini",
        )
        await cost.log_usage(resp, TaskType.GENERATE, RoutingProfile.BUDGET)

    summary = await cost.get_usage_summary("all")
    assert "3" in summary  # 3 calls
    assert "gemini" in summary


@pytest.mark.asyncio
async def test_migration_idempotent(temp_db):
    """Running _get_db() twice on the same DB must not raise OperationalError."""
    import llm_router.config as _cfg
    _cfg._config = None  # force reload with temp_db env vars
    # First open — creates schema + runs migrations
    db1 = await cost._get_db()
    await db1.close()
    # Second open — migrations must skip already-existing columns without error
    db2 = await cost._get_db()
    await db2.close()
    _cfg._config = None


@pytest.mark.asyncio
async def test_column_exists_helper(temp_db):
    """_column_exists returns True for existing columns, False for missing ones."""
    import llm_router.config as _cfg
    _cfg._config = None
    db = await cost._get_db()
    assert await cost._column_exists(db, "usage", "cost_usd") is True
    assert await cost._column_exists(db, "usage", "nonexistent_col_xyz") is False
    await db.close()
    _cfg._config = None
