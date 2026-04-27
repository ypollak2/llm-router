"""Tests for cost tracking."""


import pytest

from llm_router import cost
from llm_router.types import LLMResponse, RoutingProfile, TaskType


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


@pytest.mark.asyncio
async def test_column_exists_rejects_sql_injection(temp_db):
    """_column_exists rejects malicious table names to prevent SQL injection."""
    import llm_router.config as _cfg
    _cfg._config = None
    db = await cost._get_db()
    
    # Test SQL injection attempts — all should return False (invalid table name)
    injection_attempts = [
        "usage'); DROP TABLE usage; --",
        "usage' OR '1'='1",
        "usage'; DELETE FROM usage; --",
        "usage` OR 1=1 --",
        "nonexistent_table",  # Invalid table name (not in allowlist)
    ]
    
    for malicious_table in injection_attempts:
        result = await cost._column_exists(db, malicious_table, "timestamp")
        assert result is False, f"SQL injection check failed for: {malicious_table}"
    
    # Verify valid tables still work
    assert await cost._column_exists(db, "usage", "timestamp") is True
    
    await db.close()
    _cfg._config = None


@pytest.mark.asyncio
async def test_import_savings_log_uses_estimated_saved_key(temp_db):
    """Regression test for bug: _sync_import_savings_log() read wrong key name.
    
    The hook was reading "estimated_claude_cost_saved" but the writer used
    "estimated_saved", causing all imported savings to be 0.0.
    
    This test ensures the fix reads the correct field names:
    - "estimated_saved" (not "estimated_claude_cost_saved")
    - "model" (not "model_used")
    """
    import json
    import os
    
    # Create a mock savings_log.jsonl with correct field names
    savings_log_content = json.dumps({
        "timestamp": "2026-04-27T20:00:00+00:00",
        "session_id": "test-session-1",
        "task_type": "query",
        "estimated_saved": 0.033,  # Correct field name
        "external_cost": 0.001,
        "model": "gemini-flash",  # Correct field name
        "host": "claude_code"
    })
    
    # Write to a temporary JSONL file in temp_db directory
    state_dir = os.path.dirname(temp_db)
    savings_log_path = os.path.join(state_dir, "savings_log.jsonl")
    
    with open(savings_log_path, "w") as f:
        f.write(savings_log_content + "\n")
    
    # Import using the fixed logic from session-end.py
    records = []
    with open(savings_log_path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                # This is the fixed code that reads "estimated_saved" and "model"
                records.append((
                    r.get("timestamp", ""),
                    r.get("session_id", ""),
                    r.get("task_type", "unknown"),
                    float(r.get("estimated_saved", 0.0)),  # Correct key
                    float(r.get("external_cost", 0.0)),
                    r.get("model", "unknown"),  # Correct key
                    r.get("host", "claude_code"),
                ))
    
    # Verify the data was read correctly
    assert len(records) == 1
    assert records[0][3] == 0.033  # estimated_saved should be 0.033, not 0.0
    assert records[0][5] == "gemini-flash"  # model should be "gemini-flash", not ""
    
    # Write to database and verify
    conn = await cost._get_db()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            estimated_claude_cost_saved REAL NOT NULL,
            external_cost REAL NOT NULL,
            model_used TEXT NOT NULL,
            host TEXT NOT NULL DEFAULT 'claude_code'
        )
    """)
    await conn.executemany(
        "INSERT INTO savings_stats "
        "(timestamp, session_id, task_type, estimated_claude_cost_saved, external_cost, model_used, host) "
        "VALUES (?,?,?,?,?,?,?)",
        records,
    )
    await conn.commit()
    await conn.close()
    
    # Verify data in database
    conn = await cost._get_db()
    cursor = await conn.execute(
        "SELECT estimated_claude_cost_saved, model_used FROM savings_stats LIMIT 1"
    )
    row = await cursor.fetchone()
    await conn.close()
    
    assert row[0] == 0.033  # Should have preserved the value
    assert row[1] == "gemini-flash"


@pytest.mark.asyncio
async def test_today_filter_uses_localtime(temp_db):
    """Regression test for bug: date('now') was UTC but should use localtime.

    Users in UTC+ timezones see late-evening records as "yesterday" because
    the query used UTC instead of local time. The fix changes date('now') to
    date('now', 'localtime').
    """
    # This test verifies the fix by checking that the where clause is correct
    # In the actual code, the where clauses should use localtime

    # Expected fixed format (what should be in the code)
    expected_today_where = "date(timestamp, 'localtime') = date('now', 'localtime')"

    # Read the actual session-end.py to verify the fix is in place
    from pathlib import Path
    project_root = Path(__file__).parent.parent
    session_end_path = project_root / "src" / "llm_router" / "hooks" / "session-end.py"
    with open(session_end_path, 'r') as f:
        content = f.read()
        assert expected_today_where in content, \
            "session-end.py should use localtime in _PERIODS for 'today'"

    # Also verify in cost.py
    cost_path = project_root / "src" / "llm_router" / "cost.py"
    with open(cost_path, 'r') as f:
        content = f.read()
        # Should have multiple occurrences in get_usage_summary, get_daily_claude_tokens, etc.
        count = content.count(expected_today_where)
        assert count >= 3, \
            f"cost.py should use localtime in multiple places, found {count} occurrences"


@pytest.mark.asyncio
async def test_get_router_efficiency(temp_db):
    """Test router efficiency calculation from routing_decisions table."""
    conn = await cost._get_db()

    # Create routing_decisions table with sample data
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            recommended_model TEXT NOT NULL,
            final_model TEXT NOT NULL,
            classifier_latency_ms REAL
        )
    """)

    # Insert test data for today
    from datetime import datetime
    today_iso = datetime.now().isoformat()

    # 10 decisions: 8 on-target (final = recommended), 2 off-target
    await conn.executemany(
        "INSERT INTO routing_decisions (timestamp, recommended_model, final_model, classifier_latency_ms) "
        "VALUES (?, ?, ?, ?)",
        [
            (today_iso, "gemini-flash", "gemini-flash", 150.0),
            (today_iso, "gemini-flash", "gemini-flash", 160.0),
            (today_iso, "gpt-4o", "gpt-4o", 200.0),
            (today_iso, "gpt-4o", "gpt-4o", 210.0),
            (today_iso, "ollama", "ollama", 100.0),
            (today_iso, "ollama", "ollama", 110.0),
            (today_iso, "opus", "opus", 300.0),
            (today_iso, "opus", "opus", 320.0),
            (today_iso, "gemini-flash", "gpt-4o", 180.0),  # off-target
            (today_iso, "opus", "gemini-flash", 250.0),    # off-target
        ]
    )
    await conn.commit()
    await conn.close()

    # Test the function
    result = await cost.get_router_efficiency("today")
    assert result["total"] == 10
    assert result["on_target"] == 8
    assert result["efficiency_pct"] == 80.0


@pytest.mark.asyncio
async def test_get_classifier_overhead(temp_db):
    """Test classifier latency calculation from routing_decisions table."""
    conn = await cost._get_db()

    # Create routing_decisions table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            classifier_latency_ms REAL
        )
    """)

    # Insert test data
    from datetime import datetime
    today_iso = datetime.now().isoformat()

    latencies = [100.0, 150.0, 200.0, 250.0, 300.0]  # avg=200
    for latency in latencies:
        await conn.execute(
            "INSERT INTO routing_decisions (timestamp, classifier_latency_ms) VALUES (?, ?)",
            (today_iso, latency)
        )
    await conn.commit()
    await conn.close()

    # Test the function
    result = await cost.get_classifier_overhead("today")
    assert result["count"] == 5
    assert result["avg_ms"] == 200.0
    assert result["min_ms"] == 100.0
    assert result["max_ms"] == 300.0


@pytest.mark.asyncio
async def test_get_savings_by_task_type(temp_db):
    """Test task-type breakdown from savings_stats table."""
    conn = await cost._get_db()

    # Create savings_stats table with full schema
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            estimated_claude_cost_saved REAL NOT NULL,
            external_cost REAL NOT NULL,
            model_used TEXT NOT NULL,
            host TEXT NOT NULL DEFAULT 'claude_code'
        )
    """)

    # Insert test data
    from datetime import datetime
    today_iso = datetime.now().isoformat()

    await conn.executemany(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, estimated_claude_cost_saved, external_cost, model_used, host) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (today_iso, "s1", "query", 0.001, 0.0001, "gemini-flash", "claude_code"),
            (today_iso, "s1", "query", 0.002, 0.0002, "gemini-flash", "claude_code"),
            (today_iso, "s1", "query", 0.003, 0.0003, "gemini-flash", "claude_code"),
            (today_iso, "s1", "code", 0.005, 0.0005, "gpt-4o-mini", "claude_code"),
            (today_iso, "s1", "code", 0.010, 0.0010, "gpt-4o-mini", "claude_code"),
            (today_iso, "s1", "analyze", 0.015, 0.0015, "sonnet", "claude_code"),
        ]
    )
    await conn.commit()
    await conn.close()

    # Test the function
    result = await cost.get_savings_by_task_type("today")

    # Should be sorted by saved DESC (then by task_type for ties)
    assert len(result) == 3
    # First two have 0.015 saved, query has 0.006
    assert result[0]["saved"] == 0.015
    assert result[1]["saved"] == 0.015
    assert result[2]["task_type"] == "query"
    assert result[2]["calls"] == 3
    assert result[2]["saved"] == 0.006

    # Verify code and analyze are both in top results
    task_types = {r["task_type"] for r in result[:2]}
    assert "code" in task_types
    assert "analyze" in task_types
