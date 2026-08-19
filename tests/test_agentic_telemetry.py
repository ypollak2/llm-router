"""P7 — delegation savings telemetry into llm_router's savings_stats ledger."""
from __future__ import annotations

import sqlite3

from llm_router.agentic.telemetry import (
    record_delegation_savings,
    savings_payload,
)

_RESULT = {
    "outcome": "complete",
    "task_type": "code",
    "savings": {"actual_usd": 0.0, "baseline_usd": 0.6, "saved_usd": 0.6, "efficiency": None},
}


def test_savings_payload_from_result_dict():
    p = savings_payload(_RESULT, model="m", session_id="s1")
    assert p["saved_usd"] == 0.6 and p["actual_usd"] == 0.0
    assert p["model"] == "m" and p["session_id"] == "s1" and p["task_type"] == "code"


async def test_record_dispatches_to_injected_recorder():
    seen = []

    async def fake_recorder(payload):
        seen.append(payload)

    p = await record_delegation_savings(_RESULT, recorder=fake_recorder)
    assert seen and seen[0]["saved_usd"] == 0.6
    assert p == seen[0]


async def test_record_is_fail_open():
    async def boom(_payload):
        raise RuntimeError("db down")

    # must NOT raise — telemetry can never break a delegation
    p = await record_delegation_savings(_RESULT, recorder=boom)
    assert p["saved_usd"] == 0.6


async def test_default_recorder_writes_savings_stats_row(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    await record_delegation_savings(_RESULT, model="llm_router-agentic-router", session_id="sess")
    rows = sqlite3.connect(str(db)).execute(
        "SELECT estimated_claude_cost_saved, external_cost, model_used, session_id "
        "FROM savings_stats"
    ).fetchall()
    assert rows == [(0.6, 0.0, "llm_router-agentic-router", "sess")]
