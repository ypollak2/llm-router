"""Real calls in, reconciled money out.

The accounting work was verified writer-by-writer with the writers mocked. This
drives the whole chain on real data: real calls -> savings_log.jsonl -> the
importer -> savings_stats -> the report the user actually reads. Every defect
in that area was a disagreement between two of those steps, not a bug inside
one of them.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.requires_ollama]


@pytest.mark.asyncio
async def test_jsonl_imports_into_savings_stats_and_totals_agree(
    ollama_only_env, isolated_home, read_ledgers
):
    import sqlite3

    from llm_router import cost
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    for word in ("alpha", "bravo", "charlie"):
        await route_and_call(
            TaskType.QUERY, f"Reply with exactly the word: {word}",
            complexity_hint="simple",
        )

    jsonl = read_ledgers()["savings_jsonl"]
    assert len(jsonl) == 3, f"3 calls wrote {len(jsonl)} rows"
    expected_saved = sum(r["estimated_saved"] for r in jsonl)

    imported = await cost.import_savings_log()
    assert imported == 3, f"importer took {imported} of 3 rows"

    db = isolated_home / ".llm-router" / "usage.db"
    conn = sqlite3.connect(db)
    try:
        n, saved, spent = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(estimated_claude_cost_saved),0), "
            "COALESCE(SUM(external_cost),0) FROM savings_stats WHERE cost_state='known'"
        ).fetchone()
    finally:
        conn.close()

    assert n == 3
    assert saved == pytest.approx(expected_saved, rel=1e-6), (
        "savings changed value between the JSONL and the database"
    )
    assert spent == pytest.approx(0.0), "a local model cost money?"


@pytest.mark.asyncio
async def test_lifetime_summary_matches_the_rows(ollama_only_env, isolated_home):
    from llm_router import cost
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    for word in ("delta", "echo"):
        await route_and_call(
            TaskType.QUERY, f"Reply with exactly the word: {word}",
            complexity_hint="simple",
        )
    await cost.import_savings_log()

    summary = await cost.get_lifetime_savings_summary(days=0)

    assert summary["tasks_routed"] == 2
    assert summary["unknown_cost_routes"] == 0
    assert summary["total_external_cost"] == pytest.approx(0.0)
    assert summary["total_saved"] > 0
    assert summary["net_savings"] == pytest.approx(summary["total_saved"])


@pytest.mark.asyncio
async def test_savings_report_renders_real_data(ollama_only_env, isolated_home):
    """The surface a user actually reads, on data a real call produced."""
    from llm_router import cost
    from llm_router.commands.savings_report import render_savings_report
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    await route_and_call(TaskType.QUERY, "Reply with exactly the word: foxtrot",
                         complexity_hint="simple")
    await cost.import_savings_log()

    out = render_savings_report("all")

    assert "FREE / LOCAL" in out, out
    assert "UNKNOWN COST" not in out, "a priced local model was bucketed as unknown"
    assert "1 calls" in out or "   1 calls" in out
