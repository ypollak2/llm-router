"""GH#60: the primary tool surface never wrote a routing_decisions row.

Follow-up to #55/#56, both closed "fixed" in 13.0.4 (#58) after being verified
only against existing DB rows — neither fix was ever exercised with a fresh,
controlled call through the surface people actually use. Retested with a real
call and the gap was still there: `llm`, `llm_query`, `llm_code`,
`llm_analyze`, `llm_generate`, `llm_research` (all defined in `tools/text.py`,
including the consolidated `llm()` "1.0 door") call `route_and_call()` without
ever building or passing `classification_data` — it defaults to `None`. Only
the separate `llm_route`/`llm_act` tools (`tools/routing.py`) ever build that
dict. `_finalize_successful_route` (router.py) gated the ONLY writer of
`routing_decisions` (`cost.log_routing_decision`) behind `if
classification_data:`, so the table stayed empty for the primary surface while
`usage`/`savings_log.jsonl` still recorded real activity — the reporter's
exact live repro.

This test drives `route_and_call` exactly the way the primary surface does —
`classification_data` omitted — through a stubbed dispatch layer against an
isolated temp DB, and is the single most valuable test in this fix: it
encodes the reporter's controlled repro as CI, through the real code path.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from llm_router.types import LLMResponse, TaskType


@pytest.fixture
def _patch_dispatch(mock_env, temp_db, monkeypatch):
    """Stub only the budget-check queries and codex probe; the real
    route_and_call / _finalize_successful_route / cost.log_routing_decision
    path runs unmodified against the isolated temp DB."""
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    return temp_db


def _routing_decision_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM routing_decisions").fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_route_and_call_with_no_classification_data_writes_one_row(_patch_dispatch):
    """The load-bearing repro: route_and_call(classification_data=None) — the
    exact call shape every primary tool makes — must still write exactly one
    routing_decisions row, with honest (not invented) field values."""
    db_path = _patch_dispatch
    captured: dict = {}

    async def _fake_call_text(model, *args, **kwargs):
        captured["model"] = model
        return LLMResponse(
            content="wc -l counts the number of lines in a file.",
            model=model,
            input_tokens=42,
            output_tokens=12,
            cost_usd=0.0,
            latency_ms=250.0,
            provider="ollama",
        )

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._call_text", _fake_call_text):
        from llm_router.router import route_and_call
        result = await route_and_call(
            TaskType.QUERY,
            "In one sentence, what does the Unix command `wc -l` do?",
        )

    assert result.content == "wc -l counts the number of lines in a file."
    assert "model" in captured, "the stubbed dispatch layer was never reached"

    rows = _routing_decision_rows(db_path)
    assert len(rows) == 1, f"expected exactly one routing_decisions row, got {len(rows)}"
    row = rows[0]

    assert row["final_model"] == captured["model"]
    assert row["final_provider"] == "ollama"
    assert row["success"] == 1
    assert row["task_type"] == "query"

    # classifier_type must be distinguishable from a real classifier run
    # (including a low-confidence one) — NOT the generic "unknown" label,
    # which cost.py's own history ties to a real incident: 28,536 synthetic
    # rows in production all shared classifier_type='unknown' because a test
    # guard let unisolated writes through. Reusing that label here would
    # make an honest "no classifier ran" row indistinguishable from that
    # contamination signature in future analysis.
    assert row["classifier_type"] == "unhinted"

    # No classifier ran at all for this call — these must be NULL, not an
    # invented plausible-looking default (0.0 confidence would falsely imply
    # a classifier ran and scored zero confidence).
    assert row["classifier_model"] is None
    assert row["classifier_confidence"] is None
    assert row["classifier_latency_ms"] is None
    assert row["budget_pct_used"] is None
    assert row["quality_mode"] is None

    # complexity/recommended_model/base_model are still honest values derived
    # from what the finalizer actually has in scope: the complexity resolved
    # for model selection, and the model that actually executed.
    assert row["complexity"] in {"simple", "moderate", "complex"}
    assert row["recommended_model"] == captured["model"]
    assert row["base_model"] == captured["model"]


@pytest.mark.asyncio
async def test_route_and_call_with_real_classification_data_is_unaffected(_patch_dispatch):
    """Regression guard: the existing llm_route/llm_act path (which DOES pass
    classification_data) must keep behaving exactly as before — the #60 fix
    must not alter the already-working path, only extend coverage to the
    None case."""
    db_path = _patch_dispatch
    classification_data = {
        "task_type": "code",
        "profile": "balanced",
        "classifier_type": "heuristic",
        "classifier_confidence": 0.87,
        "classifier_latency_ms": 3.2,
        "complexity": "complex",
        "recommended_model": "ollama/qwen2.5-coder:32b",
        "base_model": "ollama/qwen2.5-coder:32b",
        "was_downshifted": True,
        "budget_pct_used": 0.42,
        "quality_mode": "conserve",
    }

    async def _fake_call_text(model, *args, **kwargs):
        return LLMResponse(
            content="ok", model=model, input_tokens=10, output_tokens=5,
            cost_usd=0.0, latency_ms=100.0, provider="ollama",
        )

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._call_text", _fake_call_text):
        from llm_router.router import route_and_call
        await route_and_call(
            TaskType.QUERY, "hello", classification_data=classification_data,
        )

    rows = _routing_decision_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["classifier_type"] == "heuristic"
    assert row["classifier_confidence"] == pytest.approx(0.87)
    assert row["classifier_latency_ms"] == pytest.approx(3.2)
    assert row["budget_pct_used"] == pytest.approx(0.42)
    assert row["quality_mode"] == "conserve"
    assert row["was_downshifted"] == 1
    assert row["recommended_model"] == "ollama/qwen2.5-coder:32b"
    assert row["base_model"] == "ollama/qwen2.5-coder:32b"
